# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	SCOPED_TICKET_ROLES,
	UNRESTRICTED_TICKET_ROLES,
	WEEKDAY_NAMES,
	_business_hours_by_day,
	_is_holiday,
	recompute_time_expense_rollups,
)

MAX_DURATION_MINUTES = 24 * 60


def derive_rate_category(city, start_time):
	"""PHASE-4-TIME-EXPENSE.md section 2. Derived from start_time against
	`city`'s AC Business Hours and AC Holiday Calendar -- reusing the same
	_business_hours_by_day/_is_holiday helpers add_business_minutes relies
	on (PHASE-3-SLA.md section 5) rather than reimplementing calendar
	logic. start_time is naive-UTC, same convention as every other
	datetime field this app treats this way; converted to the city's local
	time before classifying. Holiday beats everything else; a working
	day's own window decides Standard vs After Hours; a day the business
	hours table has no row for at all (never entered as a working day) is
	Weekend."""
	city_doc = frappe.get_cached_doc("AC City", city)
	tz = ZoneInfo(city_doc.timezone)
	local_dt = get_datetime(start_time).replace(tzinfo=datetime.UTC).astimezone(tz)
	local_date = local_dt.date()

	if _is_holiday(city_doc, local_date, {}):
		return "Holiday"

	window = _business_hours_by_day(city_doc).get(WEEKDAY_NAMES[local_date.weekday()])
	if not window:
		return "Weekend"

	start_of_day, end_of_day = window
	if start_of_day <= local_dt.time() < end_of_day:
		return "Standard"
	return "After Hours"


def get_visibility_condition(user):
	"""CHANGE-SET-POST-DEMO.md section 8/10.7. Authcor's narrowing: an
	engineer sees their own time entries, plus entries on any ticket they
	are themselves assigned to (the "shared ticket" clause -- a Support
	engineer sees the Primary's hours on a job they're both on, and nothing
	from a job they aren't), plus everything for an unrestricted role.
	Written once and used, verbatim, by both get_permission_query_conditions
	and has_permission, same pattern as ticket visibility
	(ac_smart_hands_request.py) and expense claim visibility
	(ac_expense_claim.py) -- so the two hooks can't drift apart.

	engineer is a Link to AC Engineer Profile, whose autoname is
	`field:user`, so it already *is* the owning engineer's user id and
	compares directly against the caller with no extra lookup -- same
	reasoning as this module's own validate_engineer_assignment.

	Returns None for an unrestricted role, and also -- same shape as
	ac_smart_hands_request.py's own get_visibility_condition -- for anyone
	outside SCOPED_TICKET_ROLES (Operations L2/L3), the only roles this
	own-entry/shared-ticket rule exists to narrow. That matters for a
	client role specifically: a Client L1/L2 user is bound to their own AC
	Customer via a User Permission record with apply_to_all_doctypes, which
	Frappe checks automatically against AC Time Entry's own `customer`
	field -- but only *after* this hook's has_permission side runs, and a
	has_permission controller hook can only deny, never grant beyond what's
	already there (frappe/permissions.py: has_controller_permissions,
	"Controllers can only deny permission"). Returning a real own/
	shared-ticket condition for a client here would hard-deny every time
	entry before that User Permission check ever gets a chance to run, since
	a client matches none of "own", "shared ticket", or "unrestricted
	role" -- which is exactly the bug this fixes: a client must still see
	their own customer's time entries, scoped by that existing User
	Permission, not by a rule written for engineers."""
	roles = set(frappe.get_roles(user))
	if roles & UNRESTRICTED_TICKET_ROLES:
		return None
	if not (roles & SCOPED_TICKET_ROLES):
		return None

	user_escaped = frappe.db.escape(user)
	return f"""(
		`tabAC Time Entry`.engineer = {user_escaped}
		or `tabAC Time Entry`.ticket in (
			select te.parent from `tabAC Ticket Engineer` te where te.engineer = {user_escaped}
		)
	)"""


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
			f"select name from `tabAC Time Entry` where name = %s and {condition}",
			(doc.name,),
		)
	)


class ACTimeEntry(Document):
	def before_insert(self):
		"""customer is stamped from the ticket, denormalised so month-end
		queries don't have to join through it (section 2)."""
		self.customer = frappe.db.get_value("AC Smart Hands Request", self.ticket, "customer")

	def validate(self):
		ticket = frappe.get_cached_doc("AC Smart Hands Request", self.ticket)
		self.validate_times()
		self.compute_duration()
		self.stamp_rate_category(ticket)
		self.validate_engineer_assignment(ticket)
		self.warn_on_overlap()

	def validate_times(self):
		if get_datetime(self.end_time) <= get_datetime(self.start_time):
			frappe.throw(_("End Time must be after Start Time."))
		if get_datetime(self.start_time) > now_datetime():
			frappe.throw(_("Start Time cannot be in the future."))

	def compute_duration(self):
		minutes = int((get_datetime(self.end_time) - get_datetime(self.start_time)).total_seconds() // 60)
		if minutes > MAX_DURATION_MINUTES:
			frappe.throw(_("Duration cannot exceed 24 hours."))
		self.duration_minutes = minutes
		hours, mins = divmod(minutes, 60)
		self.duration_display = f"{hours}h {mins}m"

	def stamp_rate_category(self, ticket):
		self.rate_category = derive_rate_category(ticket.city, self.start_time)

	def validate_engineer_assignment(self, ticket):
		"""The engineer must be assigned to the ticket, unless the caller is
		Ops L1 or an Admin logging on someone's behalf (section 2) -- the
		same "unrestricted" role set that already bypasses ticket-visibility
		scoping, reused rather than re-listed.

		Also enforces that a non-privileged caller can only log -- and only
		ever edit -- their own time: self.engineer being assigned to the
		ticket isn't sufficient on its own, since a Support engineer's name
		is just as pickable from that same ticket's assignment list as the
		caller's own. Covers editing an existing entry too: the stored
		(pre-save) engineer must already be them, and they may not repoint
		it at someone else."""
		if set(frappe.get_roles()) & UNRESTRICTED_TICKET_ROLES:
			return

		if self.is_new():
			if self.engineer != frappe.session.user:
				frappe.throw(_("You may only log time for yourself."))
		else:
			# get_doc_before_save() is populated for every non-new save
			# (Document._save() -> check_if_latest() -> load_doc_before_save
			# (raise_exception=True)), which either loads the pre-save row or
			# aborts before validate() ever runs -- so `previous` can't
			# actually be None on this path today. Checked anyway: a missing
			# before-image should deny, not crash on previous.engineer.
			previous = self.get_doc_before_save()
			if (
				previous is None
				or previous.engineer != frappe.session.user
				or self.engineer != frappe.session.user
			):
				frappe.throw(
					_("You may only edit your own time entries, and may not reassign them to someone else.")
				)

		assigned = {row.engineer for row in ticket.assigned_engineers}
		if self.engineer not in assigned:
			frappe.throw(_("{0} is not assigned to ticket {1}.").format(self.engineer, self.ticket))

	def warn_on_overlap(self):
		"""Warn rather than block -- an engineer could legitimately be
		logging two tickets worked in parallel at the same site."""
		overlapping = frappe.get_all(
			"AC Time Entry",
			filters={
				"engineer": self.engineer,
				"name": ["!=", self.name or ""],
				"docstatus": ["!=", 2],
				"start_time": ["<", self.end_time],
				"end_time": [">", self.start_time],
			},
			pluck="name",
		)
		if overlapping:
			frappe.msgprint(
				_("This overlaps with {0}'s existing time entry: {1}").format(
					self.engineer, ", ".join(overlapping)
				),
				indicator="orange",
				alert=True,
			)

	def on_submit(self):
		recompute_time_expense_rollups(self.ticket)

	def on_cancel(self):
		recompute_time_expense_rollups(self.ticket)
