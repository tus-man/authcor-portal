import frappe
from frappe import _
from frappe.utils import add_to_date, getdate, now_datetime

from authcor.notifications import notify
from authcor.notifications.context import (
	fin_expiring_context,
	shipment_overdue_context,
	unlogged_hours_context,
)
from authcor.notifications.escalation import evaluate_escalation_rules
from authcor.notifications.recipients import get_users_with_role


def check_sla_breaches():
	"""PHASE-3-SLA.md section 8. Registered in hooks.py under
	scheduler_events["cron"], every 15 minutes.

	The verdict is recorded at the moment a due date passes, rather than
	inferred whenever a ticket next happens to be viewed -- so a breach
	that nobody opens the ticket for still gets a timestamped record, and
	Phase 5's escalation alerts have something to hook into.

	Uses frappe.db.set_value's bulk-filter form deliberately: it skips
	Document validate()/on_update (this is a status flip, not a business
	rule to re-run) and needs no row lock (only rows currently Pending are
	touched, and a ticket only leaves Pending once).

	PHASE-5-NOTIFICATIONS.md section 5: AC Escalation Rule evaluation is
	folded into this same 15-minute cadence, after the verdicts above are
	committed -- a Response/Onsite SLA Breach rule must see this run's own
	breach, not last run's."""
	now = now_datetime()

	frappe.db.set_value(
		"AC Smart Hands Request",
		{"has_met_response_sla": "Pending", "response_due": ["<", now]},
		"has_met_response_sla",
		"No",
	)
	frappe.db.set_value(
		"AC Smart Hands Request",
		{"has_met_onsite_sla": "Pending", "onsite_due": ["<", now]},
		"has_met_onsite_sla",
		"No",
	)

	# A scheduled job owns its own transaction, and these breach verdicts are
	# its only output.
	frappe.db.commit()  # nosemgrep

	evaluate_escalation_rules()


def send_unlogged_hours_nudges():
	"""PHASE-4-TIME-EXPENSE.md section 5 / PHASE-5-NOTIFICATIONS.md section
	3 (`unlogged_hours`). Registered in hooks.py under
	scheduler_events["daily"].

	Not requested by Authcor -- taken from the reference system -- but
	these hours feed a monthly invoice, so an engineer who forgets to log
	is revenue Authcor never bills for. Tone is a reminder, not a
	reprimand.

	Routed through the common notify() path (post-demo: it used to call
	frappe.sendmail directly) so it gets the same dedupe/kill-switch/
	logging every other event gets, and so a second run within the same
	day can't email the same engineer about the same ticket twice.

	AC Time Entry.engineer is a Link to AC Engineer Profile, whose autoname
	is `field:user` -- so its name already *is* the assigned engineer's
	user id, comparable directly against AC Ticket Engineer.engineer
	(a Link to User) with no extra lookup."""
	cutoff = add_to_date(now_datetime(), hours=-24)
	tickets = frappe.get_all(
		"AC Smart Hands Request",
		filters={"status": "Completed", "completed_on": ["<", cutoff]},
		fields=["name", "subject"],
	)

	for ticket in tickets:
		assigned = frappe.get_all(
			"AC Ticket Engineer",
			filters={"parent": ticket.name, "parenttype": "AC Smart Hands Request"},
			pluck="engineer",
		)
		for engineer in assigned:
			already_logged = frappe.db.exists(
				"AC Time Entry", {"ticket": ticket.name, "engineer": engineer, "docstatus": 1}
			)
			if already_logged:
				continue

			notify(
				event_key="unlogged_hours",
				reference_doctype="AC Smart Hands Request",
				reference_name=ticket.name,
				recipients=[engineer],
				context=unlogged_hours_context(ticket.name, ticket.subject),
				subject=_("Don't forget to log your time for {0}").format(ticket.name),
			)


def send_fin_expiry_warnings():
	"""PHASE-5-NOTIFICATIONS.md section 3 (`fin_expiring`), weekly.
	Registered in hooks.py under scheduler_events["weekly"].

	60 days is our number, not Authcor's (section 10, "Open with
	Authcor"). Only FINs expire (PLAN.md section 9 / _validate_fin_not_
	expired's own reasoning) -- an engineer with a lapsed work pass can't
	be dispatched, so this gives warning before the claim path's own
	expiry check turns into a live problem."""
	cutoff = add_to_date(getdate(), days=60)
	profiles = frappe.get_all(
		"AC Engineer Profile",
		filters={"id_type": "FIN", "id_expiry": ["<=", cutoff], "is_active": 1},
		fields=["name", "user", "id_expiry"],
	)
	if not profiles:
		return

	ops_l1_users = get_users_with_role("Operations L1 Authorizer")
	for profile in profiles:
		full_name = frappe.db.get_value("User", profile.user, "full_name") or profile.user
		notify(
			event_key="fin_expiring",
			reference_doctype="AC Engineer Profile",
			reference_name=profile.name,
			recipients=[profile.user, *ops_l1_users],
			context=fin_expiring_context(full_name, profile.id_expiry),
			subject=_("FIN expiring soon -- {0}").format(full_name),
		)


def send_shipment_overdue_alerts():
	"""PHASE-5-NOTIFICATIONS.md section 3 (`shipment_overdue`), daily.
	Registered in hooks.py under scheduler_events["daily"].

	3 days is our number, not Authcor's. expected_delivery is the
	customer's own estimate, not a commitment Authcor made -- this alert
	is internal (Ops L1 only), never presented to the customer as a missed
	deadline (PHASE-6-SHIPMENT.md section 4)."""
	cutoff = add_to_date(getdate(), days=-3)
	shipments = frappe.get_all(
		"AC Shipment Request",
		filters={"status": "Open", "expected_delivery": ["<", cutoff]},
		fields=["name", "subject", "expected_delivery"],
	)
	if not shipments:
		return

	ops_l1_users = get_users_with_role("Operations L1 Authorizer")
	for shipment in shipments:
		notify(
			event_key="shipment_overdue",
			reference_doctype="AC Shipment Request",
			reference_name=shipment.name,
			recipients=ops_l1_users,
			context=shipment_overdue_context(shipment.name, shipment.subject, shipment.expected_delivery),
			subject=_("[{0}] Shipment overdue").format(shipment.name),
		)
