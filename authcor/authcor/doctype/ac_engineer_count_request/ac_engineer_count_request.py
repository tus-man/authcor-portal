# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	COUNT_CHANGE_BLOCKED_STATUSES,
	UNRESTRICTED_TICKET_ROLES,
	_ensure_can_request_count_change,
	_ensure_customer_access,
	_recompute_pool_status,
	_run_with_lock_retry,
	remove_engineer,
)


def _ensure_can_decide():
	"""Section 5: restricted to Admin L1/L2/L3 and Ops L1 -- the same role
	set as UNRESTRICTED_TICKET_ROLES, reused rather than re-listed so the
	two can't drift apart."""
	if not set(frappe.get_roles()) & UNRESTRICTED_TICKET_ROLES:
		frappe.throw(_("You are not permitted to decide engineer count requests."), frappe.PermissionError)


class ACEngineerCountRequest(Document):
	def before_insert(self):
		"""Section 2: customer and current_count are stamped from the ticket
		at creation -- current_count is a snapshot of what the customer was
		looking at when they asked, not a live fetch."""
		ticket = frappe.get_doc("AC Smart Hands Request", self.ticket)
		self.customer = ticket.customer
		self.current_count = ticket.engineers_required
		self.requested_by = frappe.session.user
		self.requested_on = now_datetime()
		self.status = "Pending"

	def validate(self):
		if self.is_new():
			self.validate_on_creation()

	def validate_on_creation(self):
		"""Section 3. Increases don't go through this doctype at all
		(section 4) -- requested_count > current_count is rejected here,
		not routed anywhere."""
		if self.requested_count < 1:
			frappe.throw(_("Requested Count must be at least 1."))
		if self.requested_count == self.current_count:
			frappe.throw(_("Requested Count is the same as the current count -- nothing to change."))
		if self.requested_count > self.current_count:
			frappe.throw(_("Increases do not go through a request -- use Increase Engineer Count directly."))

		ticket_status = frappe.db.get_value("AC Smart Hands Request", self.ticket, "status")
		if ticket_status in COUNT_CHANGE_BLOCKED_STATUSES:
			frappe.throw(
				_("Cannot request an engineer count change once the ticket is {0}.").format(ticket_status)
			)

		if frappe.db.exists("AC Engineer Count Request", {"ticket": self.ticket, "status": "Pending"}):
			frappe.throw(_("A pending engineer count request already exists for this ticket."))


@frappe.whitelist()
def create_count_request(ticket, requested_count, reason=None):
	"""Section 8: the non-portal "Change Engineer Count" button's decrease
	path (Admin/Ops L1 -- the customer-facing button is Phase 7). Client
	L1/L2 already hold Create on this doctype directly (section 2) and can
	insert a request from the Desk form as-is; staff generally don't, so
	this goes through the same role/customer gate as increase_engineer_count
	and inserts with ignore_permissions rather than depending on a Create
	grant that section 2 doesn't list for Admin/Ops L1. before_insert and
	validate_on_creation run exactly as they would for any other insert."""
	requested_count = frappe.utils.cint(requested_count)
	customer = frappe.db.get_value("AC Smart Hands Request", ticket, "customer")
	if customer is None:
		frappe.throw(_("Ticket {0} not found.").format(ticket))
	_ensure_can_request_count_change(customer)

	doc = frappe.get_doc(
		doctype="AC Engineer Count Request",
		ticket=ticket,
		requested_count=requested_count,
		reason=reason,
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


@frappe.whitelist()
def reject_count_request(request, note=None):
	"""Section 5. The ticket is untouched."""
	_ensure_can_decide()

	def _attempt():
		req = frappe.get_doc("AC Engineer Count Request", request, for_update=True)
		if req.status != "Pending":
			frappe.throw(_("This request is no longer pending."))

		req.status = "Rejected"
		req.decided_by = frappe.session.user
		req.decided_on = now_datetime()
		req.decision_note = note
		req.save(ignore_permissions=True)
		frappe.db.commit()
		return req

	return _run_with_lock_retry(_attempt)


@frappe.whitelist()
def approve_count_request(request, engineers_to_remove, note=None):
	"""Section 5. Re-validates request/ticket state (the ticket can have
	moved to In Progress while the request sat pending), then removes the
	named engineers one at a time through remove_engineer -- reusing its
	Primary-promotion rule rather than reimplementing it, and resolving
	promotion correctly at each step when several are removed at once --
	before setting engineers_required and recomputing status.

	Removals needed is max(0, assigned - requested_count): if the customer
	reduced the count before anyone (or enough people) claimed, assigned can
	already be at or below requested_count, and approval should still
	succeed with nothing to remove rather than being treated as an error."""
	_ensure_can_decide()
	if isinstance(engineers_to_remove, str):
		engineers_to_remove = frappe.parse_json(engineers_to_remove)

	def _validate_attempt():
		req = frappe.get_doc("AC Engineer Count Request", request, for_update=True)
		if req.status != "Pending":
			frappe.throw(_("This request is no longer pending."))

		ticket = frappe.get_doc("AC Smart Hands Request", req.ticket, for_update=True)
		if ticket.status in COUNT_CHANGE_BLOCKED_STATUSES:
			frappe.throw(_("Cannot approve -- the ticket is {0}.").format(ticket.status))

		expected_removals = max(0, len(ticket.assigned_engineers) - req.requested_count)
		if len(engineers_to_remove) != expected_removals:
			frappe.throw(
				_("Exactly {0} engineer(s) must be named for removal, got {1}.").format(
					expected_removals, len(engineers_to_remove)
				)
			)

		assigned_names = {row.engineer for row in ticket.assigned_engineers}
		for engineer in engineers_to_remove:
			if engineer not in assigned_names:
				frappe.throw(_("{0} is not assigned to this ticket.").format(engineer))

		return req.ticket, req.requested_count

	ticket_name, requested_count = _run_with_lock_retry(_validate_attempt)

	for engineer in engineers_to_remove:
		remove_engineer(ticket_name, engineer)

	def _finalize():
		ticket = frappe.get_doc("AC Smart Hands Request", ticket_name, for_update=True)
		ticket.engineers_required = requested_count
		_recompute_pool_status(ticket)
		ticket.save(ignore_permissions=True)

		req = frappe.get_doc("AC Engineer Count Request", request, for_update=True)
		req.status = "Approved"
		req.decided_by = frappe.session.user
		req.decided_on = now_datetime()
		req.decision_note = note
		req.save(ignore_permissions=True)

		frappe.db.commit()
		return req

	return _run_with_lock_retry(_finalize)


@frappe.whitelist()
def withdraw_count_request(request):
	"""Section 6. The requesting customer's own users only, and only while
	Pending -- lets them cancel without needing an admin to reject."""

	def _attempt():
		req = frappe.get_doc("AC Engineer Count Request", request, for_update=True)
		_ensure_customer_access(req.customer)
		if req.status != "Pending":
			frappe.throw(_("Only a pending request can be withdrawn."))

		req.status = "Withdrawn"
		req.save(ignore_permissions=True)
		frappe.db.commit()
		return req

	return _run_with_lock_retry(_attempt)
