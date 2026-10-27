"""PHASE-5-NOTIFICATIONS.md section 3: per-event Jinja context builders.

`ticket_closed` is the one the spec calls out by name: it must render
"from a function that only reads permlevel 0 fields ... rather than
passing the whole document to Jinja where a future edit could expose a
rate." build_ticket_closed_context takes a ticket *name*, not a doc, and
does its own explicit-field-list frappe.db reads -- there is no way to
accidentally widen it to sla_policy/response_due/etc. by handing it a doc
that later grows a field.

The other builders take the already-validated in-memory document from the
calling on_update/after_insert hook and pick specific fields into a plain
dict -- never the document object itself. Same discipline, just without a
fresh DB read, since none of these other events touch anything
permlevel-1 in the first place.
"""

import frappe


def ticket_created_context(ticket):
	return {
		"ticket_name": ticket.name,
		"subject": ticket.subject,
		"severity": ticket.severity,
		"city": ticket.city,
	}


def ticket_claimed_context(ticket):
	return {"ticket_name": ticket.name, "subject": ticket.subject}


def slot_confirmed_context(ticket):
	return {
		"ticket_name": ticket.name,
		"subject": ticket.subject,
		"confirmed_date": ticket.confirmed_date,
		"confirmed_time": ticket.confirmed_time,
	}


def ticket_in_progress_context(ticket):
	return {"ticket_name": ticket.name, "subject": ticket.subject}


def ticket_completed_context(ticket):
	return {"ticket_name": ticket.name, "subject": ticket.subject}


def build_ticket_closed_context(ticket_name):
	"""The one function ticket_closed's render path is allowed to build its
	Jinja context from -- explicit field lists only, never frappe.get_doc,
	so a future permlevel-1 field (a rate, a billable amount) can't reach
	this email just by existing on the doctype. No rates, no billable
	amounts: only total_hours/total_expenses (both permlevel 0) and the
	per-engineer/per-claim breakdowns behind them."""
	ticket = frappe.db.get_value(
		"AC Smart Hands Request",
		ticket_name,
		["name", "subject", "total_hours", "total_expenses"],
		as_dict=True,
	)

	time_rows = frappe.db.sql(
		"""
		select te.engineer, sum(te.duration_minutes) as minutes
		from `tabAC Time Entry` te
		where te.ticket = %s and te.docstatus = 1
		group by te.engineer
		""",
		(ticket_name,),
		as_dict=True,
	)
	engineer_hours = []
	for row in time_rows:
		user = frappe.db.get_value("AC Engineer Profile", row.engineer, "user")
		full_name = (frappe.db.get_value("User", user, "full_name") if user else None) or row.engineer
		engineer_hours.append({"engineer": full_name, "hours": round((row.minutes or 0) / 60, 2)})

	expenses = frappe.get_all(
		"AC Expense Claim",
		filters={"ticket": ticket_name, "status": ["in", ("Submitted", "Reimbursed")]},
		# `receipt` is here for the portal's detail page (PHASE-7-PORTAL.md
		# section 4: "Expense claims with amounts and receipts"), which
		# renders from this same helper rather than growing a second
		# permlevel-0 field list of its own. The closure email ignores it.
		fields=["category", "description", "amount", "currency", "receipt"],
	)

	return {
		"ticket_name": ticket.name,
		"subject": ticket.subject,
		"engineer_hours": engineer_hours,
		"total_hours": ticket.total_hours,
		"expenses": expenses,
		"total_expenses": ticket.total_expenses,
	}


def severity_escalated_context(ticket):
	last_change = ticket.severity_history[-1] if ticket.severity_history else None
	return {
		"ticket_name": ticket.name,
		"subject": ticket.subject,
		"old_severity": last_change.old_severity if last_change else None,
		"new_severity": ticket.severity,
	}


def sla_breached_context(ticket, rule, minutes_over):
	return {
		"ticket_name": ticket.name,
		"subject": ticket.subject,
		"trigger": rule.trigger,
		"rule_name": rule.rule_name,
		"minutes_over": int(minutes_over),
	}


def engineer_removed_context(ticket_name, subject):
	return {"ticket_name": ticket_name, "subject": subject}


def count_request_context(request):
	return {
		"ticket_name": request.ticket,
		"requested_count": request.requested_count,
		"current_count": request.current_count,
		"reason": request.reason,
	}


def count_request_decided_context(request):
	context = count_request_context(request)
	context["status"] = request.status
	return context


# -- Shipment events ------------------------------------------------------


def shipment_created_context(shipment):
	return {"shipment_name": shipment.name, "subject": shipment.subject, "carrier": shipment.carrier}


def shipment_received_context(shipment):
	image_url = shipment.receipt_proof[0].image if shipment.receipt_proof else None
	return {
		"shipment_name": shipment.name,
		"subject": shipment.subject,
		"actual_delivery": shipment.actual_delivery,
		"receipt_image_url": image_url,
	}


def shipment_closed_context(shipment):
	return {
		"shipment_name": shipment.name,
		"subject": shipment.subject,
		"store_in_warehouse": shipment.store_in_warehouse,
		"storage_location": shipment.storage_location,
	}


# -- Scheduled events ------------------------------------------------------


def unlogged_hours_context(ticket_name, subject):
	return {"ticket_name": ticket_name, "subject": subject}


def fin_expiring_context(engineer_name, id_expiry):
	return {"engineer_name": engineer_name, "id_expiry": id_expiry}


def shipment_overdue_context(shipment_name, subject, expected_delivery):
	return {"shipment_name": shipment_name, "subject": subject, "expected_delivery": expected_delivery}
