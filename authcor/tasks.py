import frappe
from frappe.utils import now_datetime


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
