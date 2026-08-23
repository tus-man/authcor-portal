"""PHASE-5-NOTIFICATIONS.md section 1: the channel abstraction.

notify() answers only "how" -- "when" is decided by document event hooks
and scheduled jobs elsewhere in the app, "who" by recipients.py. Keeping
these apart is what makes a later channel (Telegram in the deferred
session, WhatsApp in v2) an addition here rather than a rewrite at every
call site.

    notify(event_key, ...)
        -> for each recipient (already resolved, already excluding the
           acting user)
            -> for each channel this event uses
                -> enqueue delivery
                -> (in the background job) dedupe, send, log the attempt

Two rules are enforced here, in the one place every event funnels
through, rather than trusted to every recipient resolver or escalation
rule getting it right:

- No `sla_*` event may reach a user holding a Client role, whatever an
  escalation rule's notify_roles says.
- A failed send logs `Failed` and never raises into the caller -- it runs
  inside frappe.enqueue's own job, with its own try/except besides.
"""

import frappe
from frappe.utils import now_datetime

CLIENT_ROLES = {"Client L1 Authorizer", "Client L2 Authorizer"}

# Populated by channels.py (imported at the bottom of this module) via
# register_channel(). Keeps this module ignorant of *how* any given
# channel actually sends -- it only knows a channel name maps to a
# callable.
CHANNEL_SENDERS = {}


def register_channel(channel, sender):
	CHANNEL_SENDERS[channel] = sender


def _notifications_enabled():
	"""PHASE-5-NOTIFICATIONS.md section 7 configuration table (the kill
	switch itself is general-purpose, not Telegram-specific -- section 10's
	build order lists creating this Single as step 2, before templates or
	event wiring). Note the field is `notification_enabled` (singular) --
	that's the name it was actually given in Desk, not the `notifications_
	enabled` the spec doc uses; matching Desk's name here rather than
	touching DocType JSON.

	Deliberately frappe.get_cached_doc, not frappe.db.get_single_value:
	the field defaults to 1 (fail open, per its own "default": "1" in the
	DocType), but get_single_value reads straight from `tabSingles` and
	has no concept of schema defaults -- a Single nobody has ever opened
	and saved in Desk has no row there at all, and get_single_value
	returns None/0 for that, not the field's default. get_doc/
	get_cached_doc go through frappe.new_doc for an unsaved Single
	instead, which *does* fill in defaults -- the difference between
	"notifications are on until someone turns them off" and "notifications
	are silently off until someone opens Settings and hits Save once".
	The DocType-existence guard stays as a cheap safety net for a site
	that hasn't run bench migrate yet, not because the Single is expected
	to be routinely absent."""
	if not frappe.db.exists("DocType", "AC Notification Settings"):
		return True
	return bool(frappe.get_cached_doc("AC Notification Settings").notification_enabled)


def _is_client(user):
	return bool(set(frappe.get_roles(user)) & CLIENT_ROLES)


def _already_sent(event_key, reference_name, recipient, channel):
	"""PHASE-5-NOTIFICATIONS.md section 2 dedupe rule: an existing Sent row
	for this exact (event_key, reference_name, recipient, channel) means a
	double-save already sent this once -- skip, don't resend."""
	return bool(
		frappe.db.exists(
			"AC Notification Log",
			{
				"event_key": event_key,
				"reference_name": reference_name,
				"recipient": recipient,
				"channel": channel,
				"status": "Sent",
			},
		)
	)


def notify(
	event_key,
	reference_doctype,
	reference_name,
	recipients,
	context,
	subject,
	channels=("Email",),
	template=None,
):
	"""Resolve nothing itself -- `recipients` is whatever the caller's
	per-event resolver (recipients.py) already produced. This function's
	only jobs are the two hard rules above, excluding the acting user, and
	handing each (recipient, channel) pair to a background job.

	`channels` defaults to ("Email",) -- the only channel implemented
	until the deferred Telegram session lands; call sites that document a
	richer channel list (e.g. "Email, Telegram") still only need to pass
	what's actually deliverable today.

	`template` defaults to `event_key` -- the two are the same name for
	every plain event. They diverge only for escalation rules
	(authcor/notifications/escalation.py), whose event_key is
	`sla_breached:<rule name>` so that two different rules don't dedupe
	against each other, while every rule still renders the one
	`sla_breached` template."""
	if not _notifications_enabled():
		return

	template = template or event_key
	actor = frappe.session.user
	seen = set()
	for recipient in recipients:
		if not recipient or recipient == actor or recipient in seen:
			continue
		seen.add(recipient)

		if event_key.startswith("sla_") and _is_client(recipient):
			# Hard rule: no sla_* event may reach a Client-role user,
			# whatever an escalation rule's notify_roles says -- Desk lets
			# any Role be picked there, so this can't be left to
			# configuration alone.
			continue

		for channel in channels:
			if channel not in CHANNEL_SENDERS:
				continue
			frappe.enqueue(
				_deliver,
				queue="short",
				event_key=event_key,
				template=template,
				reference_doctype=reference_doctype,
				reference_name=reference_name,
				channel=channel,
				recipient=recipient,
				context=context,
				subject=subject,
			)


def _deliver(event_key, template, reference_doctype, reference_name, channel, recipient, context, subject):
	"""The enqueued job. Runs in its own worker transaction (frappe.enqueue
	is what makes "must not raise into the calling transaction" true by
	construction), and the try/except besides is what turns a failed send
	into an ordinary Failed log row instead of a crashed job."""
	if _already_sent(event_key, reference_name, recipient, channel):
		_write_log(event_key, reference_doctype, reference_name, channel, recipient, status="Skipped")
		return

	send_fn = CHANNEL_SENDERS.get(channel)
	try:
		if not send_fn:
			raise ValueError(f"No sender registered for channel {channel!r}.")
		address = send_fn(recipient, template, context, subject)
		_write_log(
			event_key, reference_doctype, reference_name, channel, recipient, status="Sent", address=address
		)
	except Exception as e:
		_write_log(event_key, reference_doctype, reference_name, channel, recipient, status="Failed", error=str(e))


def _write_log(event_key, reference_doctype, reference_name, channel, recipient, status, address=None, error=None):
	"""ignore_links=True: an audit log's whole job is to say what happened,
	including after the referenced record is gone -- a ticket deleted
	later (test cleanup, a genuine retention policy) must not make this
	insert start throwing LinkValidationError and silently break dedupe
	(_already_sent reads exactly these rows back)."""
	frappe.get_doc(
		doctype="AC Notification Log",
		event_key=event_key,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		channel=channel,
		recipient=recipient,
		recipient_address=address,
		status=status,
		error=error,
		sent_on=now_datetime() if status == "Sent" else None,
	).insert(ignore_permissions=True, ignore_links=True)
	frappe.db.commit()


# Registers "Email" into CHANNEL_SENDERS. Imported at the bottom, not the
# top: channels.py imports register_channel back from this module, and by
# this point notify()/register_channel are already defined on the
# (partially-initialised) authcor.notifications module object. Aliased
# on import (rather than plain `import channels`) purely so this
# module-level name can't be confused with notify()'s own `channels`
# parameter -- the import's only job is its side effect.
from authcor.notifications import channels as _channels  # noqa: E402,F401
