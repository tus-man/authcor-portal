# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	UNRESTRICTED_TICKET_ROLES,
	recompute_time_expense_rollups,
)
from authcor.permissions import readable_response

APPROVAL_ROLES = {"Operations L1 Authorizer", "Admin L1", "Admin L2"}
CLIENT_ROLES = {"Client L1 Authorizer", "Client L2 Authorizer"}
CLIENT_VISIBLE_STATUSES = ("Submitted", "Reimbursed")


def _ensure_can_mark_reimbursed():
	if not set(frappe.get_roles()) & APPROVAL_ROLES:
		frappe.throw(_("You are not permitted to mark expense claims as reimbursed."), frappe.PermissionError)


def _ensure_can_submit_claim(doc):
	"""Engineers create and submit their own (section 3) -- doc.engineer is
	a Link to AC Engineer Profile, whose autoname is `field:user`, so it
	*is* the owning engineer's user id already and compares directly
	against frappe.session.user with no extra lookup. Ops L1/Admin L1/L2
	may also submit on someone's behalf, same role set as approval."""
	if doc.engineer == frappe.session.user or set(frappe.get_roles()) & APPROVAL_ROLES:
		return
	frappe.throw(_("You are not permitted to submit this expense claim."), frappe.PermissionError)


def get_visibility_condition(user):
	"""PHASE-4-TIME-EXPENSE.md section 3. The customer's own scoping is
	already handled by Frappe's standard User Permission enforcement on the
	`customer` link field -- a Client L1/L2 user is bound to their own AC
	Customer via a User Permission record, same as ticket visibility -- so
	this hook only ever narrows client roles further, by status: Draft is
	internal, the customer sees Submitted and Reimbursed (post-demo: approval
	was removed, so Submitted is now the gate a claim becomes customer-visible
	at). Written once and used,
	verbatim, by both get_permission_query_conditions (list views/reports)
	and has_permission (get_doc/API reads) so the two hooks can't drift
	apart, same pattern as ticket visibility's get_visibility_condition.

	Returns None when the caller isn't a client role at all -- every other
	role sees every status, left to Frappe's own permission enforcement."""
	roles = set(frappe.get_roles(user))
	if not (roles & CLIENT_ROLES):
		return None

	statuses = ", ".join(frappe.db.escape(status) for status in CLIENT_VISIBLE_STATUSES)
	return f"`tabAC Expense Claim`.status in ({statuses})"


def get_permission_query_conditions(user, doctype=None):
	return get_visibility_condition(user) or ""


def has_permission(doc, ptype=None, user=None, debug=False):
	if not user:
		user = frappe.session.user

	if doc.is_new():
		return True

	condition = get_visibility_condition(user)
	if condition is None:
		return True

	return bool(
		frappe.db.sql(
			f"select name from `tabAC Expense Claim` where name = %s and {condition}",
			(doc.name,),
		)
	)


class ACExpenseClaim(Document):
	def before_insert(self):
		self.customer = frappe.db.get_value("AC Smart Hands Request", self.ticket, "customer")
		if not self.status:
			self.status = "Draft"

	def validate(self):
		self.validate_receipt()
		self.validate_engineer_ownership()

	def validate_receipt(self):
		"""Enforced here, not just via the Reqd flag -- an API write with
		ignore_mandatory could otherwise bypass it (section 3)."""
		if not self.receipt:
			frappe.throw(_("Receipt is mandatory for an expense claim."))

	def validate_engineer_ownership(self):
		"""Section 3: engineers create and submit their own claims. Covers
		both creation and editing of an existing claim -- a non-privileged
		caller must own the record both before and after this save: the
		stored (pre-save) engineer must already be them, and they may not
		repoint engineer at someone else. Ops L1/Admin L1/L2/L3 may act on
		anyone's behalf throughout -- the same "unrestricted" role set AC
		Time Entry's validate_engineer_assignment bypasses on, since the
		doc describes claim creation as "the same shape as time entry" and
		calls out only approval as narrower. In validate() rather than just
		the client dialog so an API write can't bypass it."""
		if set(frappe.get_roles()) & UNRESTRICTED_TICKET_ROLES:
			return

		if self.is_new():
			if self.engineer != frappe.session.user:
				frappe.throw(_("You may only create an expense claim for yourself."))
			return

		# get_doc_before_save() is populated for every non-new save (Document
		# ._save() -> check_if_latest() -> load_doc_before_save(raise_exception
		# =True), which either loads the pre-save row or aborts before
		# validate() ever runs -- so `previous` can't actually be None on this
		# path today. Checked anyway: a missing before-image should deny, not
		# crash on previous.engineer.
		previous = self.get_doc_before_save()
		if (
			previous is None
			or previous.engineer != frappe.session.user
			or self.engineer != frappe.session.user
		):
			frappe.throw(
				_("You may only edit your own expense claims, and may not reassign them to someone else.")
			)


@frappe.whitelist(methods=["POST"])
def submit_claim(claim):
	"""Section 3. Engineer creates Draft, then submits it themselves."""
	doc = frappe.get_doc("AC Expense Claim", claim)
	_ensure_can_submit_claim(doc)
	if doc.status != "Draft":
		frappe.throw(_("Only a Draft claim can be submitted."))
	doc.status = "Submitted"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return readable_response(doc)


@frappe.whitelist(methods=["POST"])
def mark_reimbursed(claim):
	"""Section 3, post-demo: approval is gone -- a claim reaches the
	customer as soon as it's Submitted with a receipt, so this is the one
	remaining decision, tracking that Authcor has actually paid it out."""
	_ensure_can_mark_reimbursed()
	doc = frappe.get_doc("AC Expense Claim", claim)
	if doc.status != "Submitted":
		frappe.throw(_("Only a Submitted claim can be marked Reimbursed."))

	doc.status = "Reimbursed"
	doc.save(ignore_permissions=True)

	recompute_time_expense_rollups(doc.ticket)
	frappe.db.commit()
	return readable_response(doc)
