import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime


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
	touched, and a ticket only leaves Pending once)."""
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

	frappe.db.commit()


def send_unlogged_hours_nudges():
	"""PHASE-4-TIME-EXPENSE.md section 5. Registered in hooks.py under
	scheduler_events["daily"].

	Not requested by Authcor -- taken from the reference system -- but
	these hours feed a monthly invoice, so an engineer who forgets to log
	is revenue Authcor never bills for. Tone is a reminder, not a
	reprimand.

	AC Time Entry.engineer is a Link to AC Engineer Profile, whose autoname
	is `field:user` -- so its name already *is* the assigned engineer's
	user id, comparable directly against AC Ticket Engineer.engineer
	(a Link to User) with no extra lookup."""
	cutoff = add_to_date(now_datetime(), hours=-24)
	tickets = frappe.get_all(
		"AC Smart Hands Request",
		filters={"status": "Completed", "completed_on": ["<", cutoff]},
		pluck="name",
	)

	for ticket_name in tickets:
		ticket = frappe.get_doc("AC Smart Hands Request", ticket_name)
		for row in ticket.assigned_engineers:
			already_logged = frappe.db.exists(
				"AC Time Entry", {"ticket": ticket_name, "engineer": row.engineer, "docstatus": 1}
			)
			if already_logged:
				continue

			email = frappe.db.get_value("User", row.engineer, "email")
			if not email:
				continue

			frappe.sendmail(
				recipients=[email],
				subject=_("Don't forget to log your time for {0}").format(ticket_name),
				message=_(
					"Just a friendly reminder -- ticket {0} ({1}) was completed more than a day ago and "
					"we don't have a time entry from you yet. When you get a chance, please log your hours."
				).format(ticket_name, ticket.subject),
			)
