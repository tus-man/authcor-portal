# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate, now_datetime
from frappe.utils.password import get_decrypted_password


def resolve_owning_slot(confirmed_date, confirmed_time):
	"""Given a confirmed date and a time-of-day, return (owning_date,
	band_fieldname): the AC Preferred Slot row and band checkbox that
	moment falls within.

	band_overnight runs 18:00 on a slot's date through 08:59 the following
	morning, so a confirmed time before 09:00 belongs to the *previous*
	day's overnight row, not the confirmed date's own row.
	"""
	if datetime.time(9, 0) <= confirmed_time < datetime.time(13, 0):
		return confirmed_date, "band_morning"
	if datetime.time(13, 0) <= confirmed_time < datetime.time(18, 0):
		return confirmed_date, "band_afternoon"
	if confirmed_time >= datetime.time(18, 0):
		return confirmed_date, "band_overnight"
	return confirmed_date - datetime.timedelta(days=1), "band_overnight"


def combine_local_time_to_utc(local_date, local_time, timezone_name):
	"""Combine a local date + time-of-day in the given IANA timezone into
	the equivalent naive UTC datetime, for storing in confirmed_datetime."""
	local_dt = datetime.datetime.combine(local_date, local_time, tzinfo=ZoneInfo(timezone_name))
	return local_dt.astimezone(datetime.UTC).replace(tzinfo=None)


UNRESTRICTED_TICKET_ROLES = {"Admin L1", "Admin L2", "Admin L3", "Operations L1 Authorizer"}
SCOPED_TICKET_ROLES = {"Operations L2 Authorizer", "Operations L3 Authorizer"}


def get_visibility_condition(user):
	"""The single expression of who may see an AC Smart Hands Request.
	Written once and used, verbatim, by both get_permission_query_conditions
	(list views/reports/get_list) and has_permission (get_doc/API reads) --
	the same SQL text is reused by has_permission below, not just the rule
	it expresses, so the two mechanisms can't drift apart.

	Returns None when the user should see everything -- Admin L1/L2/L3 and
	Operations L1 get no restriction from this hook at all; every other
	role (including client roles) is left to Frappe's own User Permission
	enforcement, which this hook doesn't need to duplicate.

	Returns a SQL WHERE fragment (referencing `tabAC Smart Hands
	Request`) for Operations L2/L3: their own customer's
	`authorised_engineers`, plus any ticket they are already assigned to
	-- so losing customer authorisation mid-ticket doesn't hide a ticket
	they're still working.
	"""
	roles = set(frappe.get_roles(user))

	if roles & UNRESTRICTED_TICKET_ROLES:
		return None

	if not (roles & SCOPED_TICKET_ROLES):
		return None

	user_escaped = frappe.db.escape(user)
	return f"""(
		`tabAC Smart Hands Request`.customer in (
			select ce.parent from `tabAC Customer Engineer` ce where ce.engineer = {user_escaped}
		)
		or `tabAC Smart Hands Request`.name in (
			select te.parent from `tabAC Ticket Engineer` te where te.engineer = {user_escaped}
		)
	)"""


def get_permission_query_conditions(user, doctype=None):
	return get_visibility_condition(user) or ""


def has_permission(doc, ptype=None, user=None, debug=False):
	if not user:
		user = frappe.session.user

	if doc.is_new():
		# Nothing to scope a not-yet-saved document against -- create
		# permission is governed by the standard Role Permissions Manager.
		return True

	condition = get_visibility_condition(user)
	if condition is None:
		return True

	# Re-run the exact same SQL fragment used above, narrowed to this one
	# document, rather than re-expressing the rule in Python -- that's
	# what guarantees the two hooks can't disagree.
	return bool(
		frappe.db.sql(
			f"select name from `tabAC Smart Hands Request` where name = %s and {condition}",
			(doc.name,),
		)
	)


def _ensure_customer_access(customer):
	if not frappe.has_permission("AC Customer", doc=customer):
		frappe.throw(_("Not permitted to access this customer."), frappe.PermissionError)


@frappe.whitelist()
def get_allowed_countries(customer):
	_ensure_customer_access(customer)
	return frappe.get_all(
		"AC Customer Service Area",
		filters={"parent": customer},
		pluck="country",
		distinct=True,
	)


@frappe.whitelist()
def get_allowed_cities(customer, country):
	_ensure_customer_access(customer)
	return frappe.get_all(
		"AC Customer Service Area",
		filters={"parent": customer, "country": country},
		pluck="city",
		distinct=True,
	)


@frappe.whitelist()
def get_allowed_data_centers(customer, city):
	_ensure_customer_access(customer)
	rows = frappe.get_all(
		"AC Customer Service Area",
		filters={"parent": customer, "city": city},
		fields=["data_center"],
	)
	if any(not row.data_center for row in rows):
		# A row for this city leaves data_center blank -- that authorises
		# every data centre in the city, not just the ones listed.
		return frappe.get_all("AC Data Center", filters={"city": city}, pluck="name")
	return list({row.data_center for row in rows if row.data_center})


ASSIGNMENT_MANAGER_ROLES = {
	"Admin L1",
	"Admin L2",
	"Admin L3",
	"Operations L1 Authorizer",
	"Operations L2 Authorizer",
}


def _get_active_engineer_profile(user):
	profile_name = frappe.db.get_value("AC Engineer Profile", {"user": user}, "name")
	if not profile_name:
		frappe.throw(_("No engineer profile found for {0}.").format(user))
	profile = frappe.get_doc("AC Engineer Profile", profile_name)
	if not profile.is_active:
		frappe.throw(_("The engineer profile for {0} is not active.").format(user))
	return profile


def _is_authorised_engineer(customer, user):
	return bool(frappe.db.exists("AC Customer Engineer", {"parent": customer, "engineer": user}))


def _validate_fin_not_expired(engineer_profile, ticket):
	"""Only FINs expire, not NRICs (PLAN.md section 9) -- so this is a
	no-op for any other id_type. Compared against the confirmed slot when
	one exists yet; otherwise against today, so an already-expired FIN is
	never allowed to claim regardless of scheduling state."""
	if engineer_profile.id_type != "FIN" or not engineer_profile.id_expiry:
		return

	deadline = getdate(ticket.confirmed_datetime) if ticket.confirmed_datetime else getdate()
	if getdate(engineer_profile.id_expiry) < deadline:
		frappe.throw(
			_("Your FIN expires on {0} and cannot be dispatched for this ticket.").format(
				frappe.utils.formatdate(engineer_profile.id_expiry)
			)
		)


def _ensure_can_manage_assignments():
	if not set(frappe.get_roles()) & ASSIGNMENT_MANAGER_ROLES:
		frappe.throw(
			_("You are not permitted to manage ticket assignments."),
			frappe.PermissionError,
		)


def _assign_engineer(doc, engineer_user, added_by):
	"""Core assignment logic shared by claim_ticket and add_engineer.
	`doc` must already be the locked (for_update), freshly reloaded
	ticket -- callers acquire the lock, this function never does.
	Validates duplicates/capacity/authorisation/FIN-expiry, appends the
	row, recomputes status, and discloses the engineer's profile."""
	if any(row.engineer == engineer_user for row in doc.assigned_engineers):
		frappe.throw(_("{0} is already assigned to this ticket.").format(engineer_user))

	if len(doc.assigned_engineers) >= doc.engineers_required:
		frappe.throw(_("This ticket is already fully staffed."))

	if not _is_authorised_engineer(doc.customer, engineer_user):
		frappe.throw(_("{0} is not authorised for this customer.").format(engineer_user))

	engineer_profile = _get_active_engineer_profile(engineer_user)
	_validate_fin_not_expired(engineer_profile, doc)

	assignment_type = "Primary" if not doc.assigned_engineers else "Support"
	doc.append(
		"assigned_engineers",
		{
			"engineer": engineer_user,
			"assignment_type": assignment_type,
			"claimed_at": now_datetime(),
			"added_by": added_by,
		},
	)
	doc.status = "Claimed" if len(doc.assigned_engineers) >= doc.engineers_required else "Partially Claimed"
	doc.save(ignore_permissions=True)

	disclose_engineer_profile(doc, engineer_profile)
	return doc


LOCK_RETRY_ATTEMPTS = 5


def _run_with_lock_retry(fn):
	"""SELECT ... FOR UPDATE under REPEATABLE READ (MariaDB's default,
	and this site's) can raise QueryDeadlockError -- MariaDB error 1020,
	ER_CHECKREAD, "Record has changed since last read... try restarting
	transaction" -- when several transactions race for the same row's
	lock, even though the lock itself is behaving correctly. This isn't
	a corner case; it reproduces routinely with as few as two concurrent
	claimants (verified empirically while building this). Frappe
	deliberately classifies 1020 the same as a genuine deadlock (1213) --
	see frappe/database/mariadb/database.py: "Snapshot isolation is also
	treated as deadlock from User POV" -- and the documented remedy for
	both is to roll back and retry the whole transaction. `fn` must
	re-acquire the lock itself on every attempt, not just retry a single
	statement, since the failure invalidates the whole transaction."""
	last_error = None
	for _attempt in range(LOCK_RETRY_ATTEMPTS):
		try:
			return fn()
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as e:
			last_error = e
			frappe.db.rollback()
	raise last_error


@frappe.whitelist()
def claim_ticket(ticket):
	"""Section 3. Self-claim by the calling engineer."""
	engineer_user = frappe.session.user
	# Step 1: fail fast, before ever taking the row lock, on a caller who
	# isn't a valid engineer at all.
	_get_active_engineer_profile(engineer_user)

	def _attempt():
		# Steps 2-3: SELECT ... FOR UPDATE, and this is the first read of
		# assigned_engineers -- never read before the lock.
		doc = frappe.get_doc("AC Smart Hands Request", ticket, for_update=True)
		doc = _assign_engineer(doc, engineer_user, added_by=engineer_user)
		frappe.db.commit()
		return doc

	return _run_with_lock_retry(_attempt)


@frappe.whitelist()
def add_engineer(ticket, engineer):
	"""Section 4. Admin/Ops L1/L2 assignment, bypassing the claim queue --
	but not the authorisation/capacity/FIN checks, which still apply."""
	_ensure_can_manage_assignments()

	def _attempt():
		doc = frappe.get_doc("AC Smart Hands Request", ticket, for_update=True)
		doc = _assign_engineer(doc, engineer, added_by=frappe.session.user)
		frappe.db.commit()
		return doc

	return _run_with_lock_retry(_attempt)


@frappe.whitelist()
def remove_engineer(ticket, engineer):
	"""Section 4. If the removed engineer was Primary and Support
	engineers remain, promote the earliest-claimed Support -- otherwise a
	ticket can end up with only Support engineers and no owner. Status is
	only recomputed while the ticket is still in the pool lifecycle
	(In Pool/Partially Claimed/Claimed); a Scheduled-or-later ticket's
	status is left untouched, since removal rules past that point aren't
	specified here. The disclosure log is never touched -- the disclosure
	already happened and the record of it stands."""
	_ensure_can_manage_assignments()

	def _attempt():
		doc = frappe.get_doc("AC Smart Hands Request", ticket, for_update=True)

		remaining = [row for row in doc.assigned_engineers if row.engineer != engineer]
		if len(remaining) == len(doc.assigned_engineers):
			frappe.throw(_("{0} is not assigned to this ticket.").format(engineer))

		was_primary = any(
			row.engineer == engineer and row.assignment_type == "Primary" for row in doc.assigned_engineers
		)
		doc.set("assigned_engineers", remaining)

		if was_primary:
			remaining_support = sorted(
				(row for row in doc.assigned_engineers if row.assignment_type == "Support"),
				key=lambda row: row.claimed_at,
			)
			if remaining_support:
				remaining_support[0].assignment_type = "Primary"

		if doc.status in ("In Pool", "Partially Claimed", "Claimed"):
			if not doc.assigned_engineers:
				doc.status = "In Pool"
			elif len(doc.assigned_engineers) >= doc.engineers_required:
				doc.status = "Claimed"
			else:
				doc.status = "Partially Claimed"

		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return doc

	return _run_with_lock_retry(_attempt)


def _get_disclosure_recipients(customer):
	"""Portal users plus leads/heads, per the notification rules in
	PLAN.md section 6. Only active portal users -- a deactivated portal
	contact shouldn't keep receiving PDPA-sensitive disclosures."""
	emails = set()

	for row in customer.portal_users:
		if row.is_active and row.user:
			email = frappe.db.get_value("User", row.user, "email")
			if email:
				emails.add(email)

	for row in customer.leads_and_heads:
		if row.user:
			email = frappe.db.get_value("User", row.user, "email")
			if email:
				emails.add(email)

	return emails


def disclose_engineer_profile(ticket, engineer_profile):
	"""Section 3. The single place NRIC/FIN gets disclosed to a customer --
	called from nowhere else. Reads the decrypted ID, builds the
	notification, sends it, and writes one AC ID Disclosure Log row per
	recipient (the log's disclosed_to field holds a single address, so a
	multi-recipient disclosure is multiple rows sharing the same
	ticket/engineer/customer). The v2 switch to a portal view only ever
	needs to change what happens in this function."""
	customer = frappe.get_doc("AC Customer", ticket.customer)
	recipients = _get_disclosure_recipients(customer)
	if not recipients:
		return

	id_number = get_decrypted_password("AC Engineer Profile", engineer_profile.name, "id_number")
	engineer_name = frappe.db.get_value("User", engineer_profile.user, "full_name") or engineer_profile.user

	subject = _("Engineer assigned to your ticket {0}").format(ticket.name)
	message = frappe.render_template(
		"""
		<p>An engineer has been assigned to ticket <strong>{{ ticket_name }}</strong> ({{ subject }}).</p>
		<table>
			<tr><td>Name</td><td>{{ engineer_name }}</td></tr>
			<tr><td>{{ id_label }}</td><td>{{ id_number }}</td></tr>
			<tr><td>Phone</td><td>{{ phone }}</td></tr>
		</table>
		<p>Please use these details to file the data centre access request.</p>
		""",
		{
			"ticket_name": ticket.name,
			"subject": ticket.subject,
			"engineer_name": engineer_name,
			"id_label": engineer_profile.id_type,
			"id_number": id_number,
			"phone": engineer_profile.phone,
		},
	)

	disclosed_on = now_datetime()
	for email in recipients:
		frappe.sendmail(recipients=[email], subject=subject, message=message)
		frappe.get_doc(
			{
				"doctype": "AC ID Disclosure Log",
				"ticket": ticket.name,
				"engineer": engineer_profile.name,
				"customer": customer.name,
				"disclosed_to": email,
				"disclosed_on": disclosed_on,
				"channel": "Email",
			}
		).insert(ignore_permissions=True)


class ACSmartHandsRequest(Document):
	def before_insert(self):
		self.generate_preferred_slots()

	def validate(self):
		self.validate_engineers_required()
		self.validate_action_other_details()
		self.validate_confirmed_slot()
		self.stamp_completion()

	def generate_preferred_slots(self):
		"""(a) Seven date rows for the next 7 days, starting the day after
		creation, all three bands ticked. Skipped if slots were already
		supplied (e.g. an API-created ticket), so they don't get
		overwritten."""
		if self.preferred_slots:
			return

		start = add_days(getdate(), 1)
		for offset in range(7):
			self.append(
				"preferred_slots",
				{
					"slot_date": add_days(start, offset),
					"band_morning": 1,
					"band_afternoon": 1,
					"band_overnight": 1,
				},
			)

	def validate_engineers_required(self):
		"""(b) engineers_required >= 1. non_negative on the field already
		blocks negatives client-side and in core Document validation; this
		catches zero, which non_negative allows."""
		if self.engineers_required < 1:
			frappe.throw(_("Engineers Required must be at least 1."))

	def validate_action_other_details(self):
		"""(b) Depends On / Mandatory Depends On on action_other_details are
		client-side only and don't apply to API writes -- enforce the same
		rule here."""
		if self.action_required == "Others" and not self.action_other_details:
			frappe.throw(_("Other Details is required when Action Required is Others."))

	def validate_confirmed_slot(self):
		"""(c) Given confirmed_date and confirmed_time, check the chosen
		moment falls inside a band the customer left ticked, and compute
		confirmed_datetime (UTC) from it. Skipped until Ops has actually
		confirmed a slot."""
		if not (self.confirmed_date and self.confirmed_time):
			return

		confirmed_date = getdate(self.confirmed_date)
		confirmed_time = datetime.datetime.strptime(self.confirmed_time, "%H:%M").time()
		owning_date, band_fieldname = resolve_owning_slot(confirmed_date, confirmed_time)

		slot_row = next(
			(row for row in self.preferred_slots if getdate(row.slot_date) == owning_date),
			None,
		)
		if not slot_row or not slot_row.get(band_fieldname):
			frappe.throw(
				_("The confirmed time {0} on {1} does not fall inside a time band the customer offered.").format(
					self.confirmed_time, self.confirmed_date
				)
			)

		timezone_name = frappe.db.get_value("AC City", self.city, "timezone")
		self.confirmed_datetime = combine_local_time_to_utc(confirmed_date, confirmed_time, timezone_name)

	def stamp_completion(self):
		"""(e) Stamp completed_on/completed_by the first time status becomes
		Completed; clear both on any transition away from it. The "only
		when currently empty" guard on the Completed branch is what makes
		re-editing an already-completed ticket a no-op instead of
		overwriting the original timestamp."""
		if self.status == "Completed":
			if not self.completed_on:
				self.completed_on = now_datetime()
			if not self.completed_by:
				# Resolve explicitly via the `user` field rather than
				# assuming AC Engineer Profile's name coincides with
				# frappe.session.user -- that's true today because the
				# doctype is named `field:user`, but not guaranteed.
				engineer_profile = frappe.db.get_value(
					"AC Engineer Profile", {"user": frappe.session.user}, "name"
				)
				if engineer_profile:
					self.completed_by = engineer_profile
		else:
			self.completed_on = None
			self.completed_by = None
