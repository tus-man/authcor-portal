# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, get_datetime, get_time, getdate, now_datetime
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


WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Bound on how many calendar days _next_working_period_start will scan before
# giving up. A sane calendar resolves in well under a week; this is just a
# backstop against an all-non-working-day / all-holiday misconfiguration
# spinning forever.
MAX_WORKING_DAY_SEARCH = 370


def _business_hours_by_day(city_doc):
	"""{day_name: (start_time, end_time)} from AC City.business_hours.
	get_time normalises the field regardless of whether it comes back as a
	timedelta (Frappe's native Time representation), a datetime.time, or a
	string -- callers don't need to know which."""
	return {row.day: (get_time(row.start_time), get_time(row.end_time)) for row in city_doc.business_hours}


def _get_holiday_dates(city_doc, year, holiday_cache):
	"""Holiday dates for `year`, from the AC Holiday Calendar linked on
	`city_doc` -- cached per call in `holiday_cache` since a single
	add_business_minutes call can consult the same year repeatedly.

	No calendar linked at all, or a linked calendar for the wrong year, is
	a configuration error and raises -- same treatment as a city with no
	business hours, since a missing 2027 calendar would otherwise produce
	quietly wrong deadlines all year. A calendar that exists for the right
	year with zero rows in its holidays table is a deliberate "no
	holidays" answer, not a missing one, and is accepted as-is."""
	if year in holiday_cache:
		return holiday_cache[year]

	if not city_doc.holiday_calendar:
		frappe.throw(
			_("AC City {0} has no holiday_calendar configured for {1}.").format(city_doc.name, year)
		)

	calendar = frappe.get_cached_doc("AC Holiday Calendar", city_doc.holiday_calendar)
	if calendar.year != year:
		frappe.throw(
			_("AC Holiday Calendar {0} covers {1}, not {2} -- AC City {3} needs a calendar for {2}.").format(
				calendar.name, calendar.year, year, city_doc.name
			)
		)

	dates = {getdate(row.holiday_date) for row in calendar.holidays}
	holiday_cache[year] = dates
	return dates


def _is_holiday(city_doc, local_date, holiday_cache):
	return local_date in _get_holiday_dates(city_doc, local_date.year, holiday_cache)


def _next_working_period_start(local_date, business_hours, city_doc, tz, holiday_cache):
	"""The first working day at or after `local_date` -- a plain date, not a
	datetime, so this always lands on that day's opening time regardless of
	what time of day the search started from. Used both to roll over to
	"tomorrow" after a day's capacity is used up, and to answer
	next_business_day_end directly."""
	d = local_date
	for _ in range(MAX_WORKING_DAY_SEARCH):
		window = business_hours.get(WEEKDAY_NAMES[d.weekday()])
		if window and not _is_holiday(city_doc, d, holiday_cache):
			return datetime.datetime.combine(d, window[0], tzinfo=tz)
		d += datetime.timedelta(days=1)
	frappe.throw(
		_("No working day found for AC City {0} within {1} days of {2} -- check its business hours.").format(
			city_doc.name, MAX_WORKING_DAY_SEARCH, local_date
		)
	)


def _advance_to_working_instant(local_dt, business_hours, city_doc, tz, holiday_cache):
	"""Where the clock actually starts counting from: unchanged if already
	inside today's working window, moved to today's opening if today is a
	working day but it's too early, otherwise rolled to the next working
	day's opening (weekend, holiday, or past closing all take this branch)."""
	d = local_dt.date()
	window = business_hours.get(WEEKDAY_NAMES[d.weekday()])
	if window and not _is_holiday(city_doc, d, holiday_cache):
		start_time, end_time = window
		if local_dt.time() < start_time:
			return datetime.datetime.combine(d, start_time, tzinfo=tz)
		if local_dt.time() < end_time:
			return local_dt
	return _next_working_period_start(d + datetime.timedelta(days=1), business_hours, city_doc, tz, holiday_cache)


def _get_business_hours_or_throw(city_doc):
	business_hours = _business_hours_by_day(city_doc)
	if not business_hours:
		frappe.throw(_("AC City {0} has no business hours configured.").format(city_doc.name))
	return business_hours


def add_business_minutes(start_utc, minutes, city):
	"""Section 5. Advance a naive-UTC timestamp by `minutes` *working*
	minutes, per `city`'s AC Business Hours and AC Holiday Calendar --
	skipping non-working hours, non-working days, and holidays. A start
	outside working hours begins counting from the next working period, not
	immediately (PHASE-3-SLA.md section 5)."""
	city_doc = frappe.get_cached_doc("AC City", city)
	business_hours = _get_business_hours_or_throw(city_doc)
	tz = ZoneInfo(city_doc.timezone)
	holiday_cache = {}

	local_start = start_utc.replace(tzinfo=datetime.UTC).astimezone(tz)
	current = _advance_to_working_instant(local_start, business_hours, city_doc, tz, holiday_cache)

	remaining = minutes
	while remaining > 0:
		d = current.date()
		_start_time, end_time = business_hours[WEEKDAY_NAMES[d.weekday()]]
		day_end = datetime.datetime.combine(d, end_time, tzinfo=tz)
		available = (day_end - current).total_seconds() / 60

		if remaining <= available:
			current += datetime.timedelta(minutes=remaining)
			remaining = 0
		else:
			remaining -= available
			current = _next_working_period_start(
				d + datetime.timedelta(days=1), business_hours, city_doc, tz, holiday_cache
			)

	return current.astimezone(datetime.UTC).replace(tzinfo=None)


def next_business_day_end(start_utc, city):
	"""Section 5. End-of-day of the next working day after `start_utc`'s
	local date -- always the day *after*, regardless of what time of day
	start_utc falls at, since this answers "when does the next working day
	close," not "how much of today is left" (PHASE-3-SLA.md section 5)."""
	city_doc = frappe.get_cached_doc("AC City", city)
	business_hours = _get_business_hours_or_throw(city_doc)
	tz = ZoneInfo(city_doc.timezone)
	holiday_cache = {}

	local_date = start_utc.replace(tzinfo=datetime.UTC).astimezone(tz).date()
	next_start = _next_working_period_start(
		local_date + datetime.timedelta(days=1), business_hours, city_doc, tz, holiday_cache
	)
	_start_time, end_time = business_hours[WEEKDAY_NAMES[next_start.weekday()]]
	day_end = datetime.datetime.combine(next_start.date(), end_time, tzinfo=tz)

	return day_end.astimezone(datetime.UTC).replace(tzinfo=None)


def _minutes_between(start, end):
	"""Same normalise-on-entry reasoning as _compute_due_datetime: `start`
	is frequently self.creation, which is a string rather than a datetime
	until the document has been through a DB round-trip. stamp_confirmation
	can call this from validate() during before_insert's own save (a
	ticket confirmed at creation, not just via a later edit), which hits
	the exact same unparsed value."""
	return int((get_datetime(end) - get_datetime(start)).total_seconds() // 60)


def _get_active_sla_policy(service_level, severity):
	"""Section 6. The unique active AC SLA Policy for (service_level,
	severity) -- queried by filters rather than the {service_level}-
	{severity} autoname, since service_level values can contain characters
	(the P4-Scheduled tier) that make reconstructing the name error-prone."""
	policy_name = frappe.db.get_value(
		"AC SLA Policy",
		{"service_level": service_level, "severity": severity, "is_active": 1},
		"name",
	)
	if not policy_name:
		frappe.throw(
			_("No active AC SLA Policy for service level {0}, severity {1}.").format(service_level, severity)
		)
	return frappe.get_cached_doc("AC SLA Policy", policy_name)


def _compute_due_datetime(start_utc, minutes, next_business_day, business_hours_only, city):
	"""Section 6. One due-date rule, shared by response and onsite targets:
	next_business_day wins when ticked (P4-Scheduled, and the Standard/Basic
	P2 rows); otherwise a plain duration from `start_utc`, in business
	minutes when business_hours_only is ticked (Basic tier) or wall-clock
	minutes otherwise. No target at all (neither minutes nor the flag)
	returns None -- the caller decides what that means.

	Normalises `start_utc` on entry via frappe.utils.get_datetime -- the
	only boundary this whole SLA pathway has, since a caller mid-insert
	may still be holding self.creation as the unparsed DB-format string
	Document.set_user_and_timestamp assigns it (frappe.utils.now()
	returns a string, not a datetime), not yet cast back by a DB
	round-trip. get_datetime is a no-op on a value that's already a
	datetime, so this is free for every other caller."""
	start_utc = get_datetime(start_utc)
	if next_business_day:
		return next_business_day_end(start_utc, city)
	if not minutes:
		return None
	if business_hours_only:
		return add_business_minutes(start_utc, minutes, city)
	return start_utc + datetime.timedelta(minutes=minutes)


def _apply_sla_policy(doc, policy, start_utc):
	"""Sections 6-7. Stamp sla_policy/response_due/onsite_due on `doc` from
	`policy`, computed from `start_utc` -- the single routine shared by
	ticket creation and severity escalation, since both are just "pick a
	policy, compute due dates from a given instant" with a different
	instant. Returns whether the policy carries an onsite target at all,
	so callers don't re-derive it from the same two fields a second time.
	Never touches the verdict fields -- those depend on what has already
	happened on the ticket, which this function has no view of."""
	doc.sla_policy = policy.name
	doc.response_due = _compute_due_datetime(
		start_utc,
		policy.response_minutes,
		policy.response_next_business_day,
		policy.business_hours_only,
		doc.city,
	)

	has_onsite_target = bool(policy.onsite_minutes) or bool(policy.onsite_next_business_day)
	doc.onsite_due = (
		_compute_due_datetime(
			start_utc, policy.onsite_minutes, policy.onsite_next_business_day, policy.business_hours_only, doc.city
		)
		if has_onsite_target
		else None
	)
	return has_onsite_target


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

	if assignment_type == "Primary":
		# Section 1/6: first response is the first row landing in
		# assigned_engineers, full stop -- an admin's add_engineer counts
		# the same as a self-claim, since both produce exactly that event.
		doc.first_response_at = now_datetime()
		doc.first_response_minutes = _minutes_between(doc.creation, doc.first_response_at)
		doc.has_met_response_sla = (
			"Yes" if not doc.response_due or doc.first_response_at <= doc.response_due else "No"
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


COUNT_CHANGE_BLOCKED_STATUSES = {"In Progress", "Completed", "Cancelled"}


def _recompute_pool_status(doc):
	"""PHASE-3B-ENGINEER-COUNT.md sections 4-5. Same "only while still in
	the pool lifecycle" convention as remove_engineer's inline recompute --
	left untouched at Scheduled or later. Shared by increase_engineer_count
	and approve_count_request, both of which change engineers_required or
	assigned_engineers outside the claim/remove paths that already carry
	their own recompute."""
	if doc.status not in ("In Pool", "Partially Claimed", "Claimed"):
		return
	if not doc.assigned_engineers:
		doc.status = "In Pool"
	elif len(doc.assigned_engineers) >= doc.engineers_required:
		doc.status = "Claimed"
	else:
		doc.status = "Partially Claimed"


def _ensure_can_request_count_change(customer):
	"""Sections 4/8: who may *ask* for an engineer count change is one rule
	regardless of direction -- the customer's own portal users, plus Ops L1
	and Admins, the same "unrestricted" role set that already sees every
	ticket regardless of customer scoping. Shared by increase_engineer_count
	and AC Engineer Count Request's create_count_request."""
	if set(frappe.get_roles()) & UNRESTRICTED_TICKET_ROLES:
		return
	_ensure_customer_access(customer)


@frappe.whitelist()
def increase_engineer_count(ticket, new_count):
	"""Section 4. Increases need no approval -- direct edit to
	engineers_required, under the ticket lock same as claiming."""
	new_count = frappe.utils.cint(new_count)
	customer = frappe.db.get_value("AC Smart Hands Request", ticket, "customer")
	if customer is None:
		frappe.throw(_("Ticket {0} not found.").format(ticket))
	_ensure_can_request_count_change(customer)

	def _attempt():
		doc = frappe.get_doc("AC Smart Hands Request", ticket, for_update=True)

		if new_count <= doc.engineers_required:
			frappe.throw(_("New Count must be greater than the current Engineers Required."))
		if doc.status in COUNT_CHANGE_BLOCKED_STATUSES:
			frappe.throw(_("Cannot change the engineer count once the ticket is {0}.").format(doc.status))

		doc.engineers_required = new_count
		_recompute_pool_status(doc)
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
		self.apply_initial_sla_policy()

	def validate(self):
		self.validate_engineers_required()
		self.validate_action_other_details()
		self.validate_confirmed_slot()
		self.stamp_completion()
		# Escalation before onsite-arrival stamping: if severity and status
		# both change in the same save (rare, but possible), onsite_due
		# must already reflect the new policy before stamp_onsite_arrival
		# judges arrival against it.
		self.handle_severity_escalation()
		self.stamp_onsite_arrival()

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
		self.stamp_confirmation()

	def stamp_confirmation(self):
		"""Section 6. Stamp confirmed_at/confirmation_minutes the first time
		confirmed_datetime is populated -- same "only when currently empty"
		guard as stamp_completion, so re-editing an already-confirmed
		slot doesn't move the confirmation clock. idle_minutes (claim ->
		confirmation, PHASE-3-SLA.md section 1) is left unset if the ticket
		was confirmed before any engineer claimed it -- there's no claim
		instant yet to measure from."""
		if self.confirmed_at:
			return
		self.confirmed_at = now_datetime()
		self.confirmation_minutes = _minutes_between(self.creation, self.confirmed_at)
		if self.first_response_at:
			self.idle_minutes = _minutes_between(self.first_response_at, self.confirmed_at)

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

	def apply_initial_sla_policy(self):
		"""Section 6. sla_policy/response_due/onsite_due are captured once,
		from self.creation, in before_insert -- never recomputed in
		validate -- so a later policy revision doesn't retroactively change
		what a ticket was judged against. has_met_onsite_sla starts N/A
		rather than Pending when the resolved policy carries no onsite
		target at all (every P4 severity row, across every service level,
		per PHASE-3-SLA.md section 4) -- it will never move away from N/A,
		since there's nothing to judge it against."""
		policy = _get_active_sla_policy(self.service_level, self.severity)
		has_onsite_target = _apply_sla_policy(self, policy, self.creation)

		self.has_met_response_sla = "Pending"
		self.has_met_onsite_sla = "Pending" if has_onsite_target else "N/A"

	def stamp_onsite_arrival(self):
		"""Section 6. actual_onsite_at has no independent observation in the
		system -- by decision, it's derived from the transition to In
		Progress (the field is built read_only, which rules out plain
		manual entry). Same "only when currently empty" guard as
		stamp_completion, so re-saving an already-in-progress ticket
		doesn't move the timestamp. Only flips the verdict when there's an
		onsite_due to compare against -- a policy with no onsite target
		stays N/A regardless of status."""
		if self.status != "In Progress" or self.actual_onsite_at:
			return
		self.actual_onsite_at = now_datetime()
		if self.onsite_due:
			self.has_met_onsite_sla = "Yes" if self.actual_onsite_at <= self.onsite_due else "No"

	def handle_severity_escalation(self):
		"""Section 7. Only fires on an actual severity change to an
		existing ticket. has_value_changed treats a brand-new document as
		"changed" (there's no previous state to compare against yet), so
		is_new() is checked first -- otherwise every ticket creation would
		misread as escalating from nothing to its initial severity."""
		if self.is_new() or not self.has_value_changed("severity"):
			return

		_ensure_can_manage_assignments()
		self.escalate_severity()

	def escalate_severity(self):
		"""Section 7. Authcor's decision: the clock restarts from the
		moment of escalation. The history row captures the old severity's
		response_due and verdict *before* they get overwritten below --
		that's what stops escalation being used to erase a breach; without
		it, a ticket that blew its P4 target could be bumped to P1 and
		appear clean. has_met_response_sla only resets to Pending if the
		ticket hasn't been responded to yet -- an already-recorded first
		response isn't rewritten by a later escalation. has_met_onsite_sla
		gets the same treatment, extended to cover the onsite side (not
		spelled out in PHASE-3-SLA.md section 7, but the same reasoning:
		don't rewrite a verdict for something that already happened)."""
		old_severity = self.get_doc_before_save().severity
		self.append(
			"severity_history",
			{
				"changed_at": now_datetime(),
				"changed_by": frappe.session.user,
				"old_severity": old_severity,
				"new_severity": self.severity,
				"old_response_due": self.response_due,
				"old_verdict": self.has_met_response_sla,
			},
		)

		policy = _get_active_sla_policy(self.service_level, self.severity)
		has_onsite_target = _apply_sla_policy(self, policy, now_datetime())

		if not self.first_response_at:
			self.has_met_response_sla = "Pending"

		if not has_onsite_target:
			self.has_met_onsite_sla = "N/A"
		elif not self.actual_onsite_at:
			self.has_met_onsite_sla = "Pending"
