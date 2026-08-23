"""PHASE-5-NOTIFICATIONS.md section 5: AC Escalation Rule evaluation,
folded into the existing 15-minute breach job (authcor.tasks.
check_sla_breaches) rather than a new scheduler entry -- reusing the
cadence that's already checking SLA state that often.

Each fired rule dispatches through notify() under its own event_key
(`sla_breached:<rule name>`), so two different rules matching the same
ticket don't dedupe against each other, and so the *same* rule firing
again 15 minutes later finds its own prior Sent row and is skipped --
"which is also what stops it firing every 15 minutes forever"
(PHASE-5-NOTIFICATIONS.md section 5).
"""

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from authcor.notifications import notify
from authcor.notifications.context import sla_breached_context
from authcor.notifications.recipients import get_leads_and_heads, get_users_with_role

CHANNELS_BY_RULE_SETTING = {
	"Email": ("Email",),
	# Telegram lands in the deferred section 7 session -- a rule already
	# configured for "Email + Telegram" only actually delivers Email
	# until then, same as every other event in this build.
	"Email + Telegram": ("Email",),
}


def _matches(rule, severity, service_level):
	if rule.severity not in ("Any", severity):
		return False
	if rule.service_level not in ("Any", service_level):
		return False
	return True


def _is_understaffed(ticket_name, engineers_required):
	"""Section 5: "Unclaimed" means not fully staffed -- assigned count
	against engineers_required, not against zero, so a ticket needing
	three engineers with one claimed still counts as unmet."""
	assigned = frappe.db.count("AC Ticket Engineer", {"parent": ticket_name, "parenttype": "AC Smart Hands Request"})
	return assigned < engineers_required


def _unclaimed_candidates(threshold_minutes):
	cutoff = add_to_date(now_datetime(), minutes=-threshold_minutes)
	rows = frappe.get_all(
		"AC Smart Hands Request",
		filters={"status": ["in", ("Open", "Partially Claimed")], "creation": ["<", cutoff]},
		fields=["name", "severity", "service_level", "engineers_required", "creation"],
	)
	result = []
	for row in rows:
		if _is_understaffed(row.name, row.engineers_required):
			row["minutes_over"] = (now_datetime() - get_datetime(row.creation)).total_seconds() / 60
			result.append(row)
	return result


def _response_breach_candidates(threshold_minutes):
	cutoff = add_to_date(now_datetime(), minutes=-threshold_minutes)
	rows = frappe.get_all(
		"AC Smart Hands Request",
		filters={"has_met_response_sla": "No", "response_due": ["<", cutoff]},
		fields=["name", "severity", "service_level", "response_due"],
	)
	for row in rows:
		row["minutes_over"] = (now_datetime() - get_datetime(row.response_due)).total_seconds() / 60
	return rows


def _onsite_breach_candidates(threshold_minutes):
	cutoff = add_to_date(now_datetime(), minutes=-threshold_minutes)
	rows = frappe.get_all(
		"AC Smart Hands Request",
		filters={"has_met_onsite_sla": "No", "onsite_due": ["<", cutoff]},
		fields=["name", "severity", "service_level", "onsite_due"],
	)
	for row in rows:
		row["minutes_over"] = (now_datetime() - get_datetime(row.onsite_due)).total_seconds() / 60
	return rows


def _unconfirmed_candidates(threshold_minutes):
	"""A ticket that's been claimed (first_response_at is set) but never
	confirmed -- age measured from the claim, since that's the moment
	confirming a slot became something to do."""
	cutoff = add_to_date(now_datetime(), minutes=-threshold_minutes)
	rows = frappe.get_all(
		"AC Smart Hands Request",
		filters=[
			["confirmed_datetime", "is", "not set"],
			["first_response_at", "is", "set"],
			["first_response_at", "<", cutoff],
		],
		fields=["name", "severity", "service_level", "first_response_at"],
	)
	for row in rows:
		row["minutes_over"] = (now_datetime() - get_datetime(row.first_response_at)).total_seconds() / 60
	return rows


TRIGGER_CANDIDATES = {
	"Unclaimed": _unclaimed_candidates,
	"Response SLA Breach": _response_breach_candidates,
	"Onsite SLA Breach": _onsite_breach_candidates,
	"Unconfirmed": _unconfirmed_candidates,
}


def _fire(rule, ticket, minutes_over):
	recipients = []
	for row in rule.notify_roles:
		recipients.extend(get_users_with_role(row.role))
	if rule.notify_leads_heads:
		recipients.extend(get_leads_and_heads(ticket.customer))

	notify(
		event_key=f"sla_breached:{rule.name}",
		template="sla_breached",
		reference_doctype="AC Smart Hands Request",
		reference_name=ticket.name,
		recipients=recipients,
		context=sla_breached_context(ticket, rule, minutes_over),
		subject=f"[{ticket.name}] {rule.trigger} -- {ticket.subject}",
		channels=CHANNELS_BY_RULE_SETTING.get(rule.channels, ("Email",)),
	)


def evaluate_escalation_rules():
	"""Called from authcor.tasks.check_sla_breaches, after the response/
	onsite verdicts for this run are already recorded -- Response/Onsite
	SLA Breach rules read has_met_*_sla, so they must see this run's own
	verdicts, not last run's."""
	rule_names = frappe.get_all("AC Escalation Rule", filters={"is_active": 1}, pluck="name")
	for rule_name in rule_names:
		rule = frappe.get_cached_doc("AC Escalation Rule", rule_name)
		finder = TRIGGER_CANDIDATES.get(rule.trigger)
		if not finder:
			continue
		for row in finder(rule.threshold_minutes or 0):
			if not _matches(rule, row.severity, row.service_level):
				continue
			ticket = frappe.get_doc("AC Smart Hands Request", row.name)
			_fire(rule, ticket, row.minutes_over)
