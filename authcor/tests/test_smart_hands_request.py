from datetime import date, time, timedelta

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, get_datetime, getdate, now_datetime

from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	_compute_due_datetime,
	_minutes_between,
	add_business_minutes,
	combine_local_time_to_utc,
	get_allowed_cities,
	get_allowed_countries,
	get_allowed_data_centers,
	next_business_day_end,
	resolve_owning_slot,
)
from authcor.tasks import check_sla_breaches
from authcor.tests.sla_fixtures import ensure_sla_policy, ensure_sla_policy_committed

SGT = "Asia/Singapore"


def _make_city(city_name, country, timezone):
	return frappe.get_doc(
		doctype="AC City",
		city_name=city_name,
		country=country,
		timezone=timezone,
	).insert(ignore_permissions=True)


def _make_business_city(city_name, country, timezone, business_hours=None, holiday_calendar=None):
	return frappe.get_doc(
		doctype="AC City",
		city_name=city_name,
		country=country,
		timezone=timezone,
		business_hours=business_hours or [],
		holiday_calendar=holiday_calendar,
	).insert(ignore_permissions=True)


def _make_holiday_calendar(calendar_name, year, holiday_dates):
	return frappe.get_doc(
		doctype="AC Holiday Calendar",
		calendar_name=calendar_name,
		year=year,
		holidays=[{"holiday_date": d} for d in holiday_dates],
	).insert(ignore_permissions=True)


def _make_data_center(data_center_name, city):
	return frappe.get_doc(
		doctype="AC Data Center",
		data_center_name=data_center_name,
		city=city,
	).insert(ignore_permissions=True)


def _make_customer(customer_name, service_areas, service_level="Standard"):
	return frappe.get_doc(
		doctype="AC Customer",
		customer_name=customer_name,
		service_level=service_level,
		service_areas=service_areas,
	).insert(ignore_permissions=True)


def _make_plain_user(email):
	return frappe.get_doc(
		doctype="User",
		email=email,
		first_name=email.split("@", 1)[0],
		send_welcome_email=0,
	).insert(ignore_permissions=True)


class TestResolveOwningSlot(IntegrationTestCase):
	"""The standalone resolver, tested independently of the doctype -- pure
	function, no DB. Boundaries called out in PHASE-2A-TICKET.md section 6:
	08:59/09:00, 12:59/13:00, 17:59/18:00, 23:59/00:00."""

	DAY = date(2026, 6, 15)
	PREV_DAY = date(2026, 6, 14)

	def test_morning_band_start_boundary(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(9, 0)), (self.DAY, "band_morning"))

	def test_before_morning_band_is_previous_day_overnight(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(8, 59)), (self.PREV_DAY, "band_overnight"))

	def test_morning_band_end_boundary(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(12, 59)), (self.DAY, "band_morning"))

	def test_afternoon_band_start_boundary(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(13, 0)), (self.DAY, "band_afternoon"))

	def test_afternoon_band_end_boundary(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(17, 59)), (self.DAY, "band_afternoon"))

	def test_overnight_band_start_boundary(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(18, 0)), (self.DAY, "band_overnight"))

	def test_overnight_band_end_of_day_boundary(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(23, 59)), (self.DAY, "band_overnight"))

	def test_midnight_belongs_to_previous_day_overnight(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(0, 0)), (self.PREV_DAY, "band_overnight"))

	def test_mid_morning(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(10, 30)), (self.DAY, "band_morning"))

	def test_mid_afternoon(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(15, 0)), (self.DAY, "band_afternoon"))

	def test_mid_overnight_same_day(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(20, 0)), (self.DAY, "band_overnight"))

	def test_mid_overnight_previous_day(self):
		self.assertEqual(resolve_owning_slot(self.DAY, time(3, 0)), (self.PREV_DAY, "band_overnight"))


class TestCombineLocalTimeToUtc(IntegrationTestCase):
	def test_singapore_09_00_is_01_00_utc(self):
		result = combine_local_time_to_utc(date(2026, 6, 15), time(9, 0), SGT)
		self.assertEqual(result.isoformat(), "2026-06-15T01:00:00")

	def test_singapore_early_morning_rolls_back_a_utc_day(self):
		result = combine_local_time_to_utc(date(2026, 6, 15), time(3, 0), SGT)
		self.assertEqual(result.isoformat(), "2026-06-14T19:00:00")

	# US DST begins 2026-03-08 at 02:00 local (clocks spring forward to
	# 03:00) -- confirmed against zoneinfo directly rather than assumed,
	# since "second Sunday in March" shifts year to year.

	def test_new_york_before_dst_transition_is_est(self):
		result = combine_local_time_to_utc(date(2026, 3, 7), time(9, 0), "America/New_York")
		self.assertEqual(result.isoformat(), "2026-03-07T14:00:00")

	def test_new_york_after_dst_transition_is_edt(self):
		result = combine_local_time_to_utc(date(2026, 3, 8), time(9, 0), "America/New_York")
		self.assertEqual(result.isoformat(), "2026-03-08T13:00:00")

	def test_new_york_dst_transition_changes_utc_offset(self):
		# Same local wall-clock time, one day apart, must convert to a
		# different UTC hour -- proof the conversion actually consults
		# the zone's transition table rather than a fixed offset.
		before = combine_local_time_to_utc(date(2026, 3, 7), time(9, 0), "America/New_York")
		after = combine_local_time_to_utc(date(2026, 3, 8), time(9, 0), "America/New_York")
		self.assertNotEqual(before.hour, after.hour)

	# EU DST begins 2026-03-29 at 01:00 UTC (02:00 CET -> 03:00 CEST) --
	# last Sunday in March. Authcor has data centres in the Netherlands.

	def test_amsterdam_before_dst_transition_is_cet(self):
		result = combine_local_time_to_utc(date(2026, 3, 28), time(9, 0), "Europe/Amsterdam")
		self.assertEqual(result.isoformat(), "2026-03-28T08:00:00")

	def test_amsterdam_after_dst_transition_is_cest(self):
		result = combine_local_time_to_utc(date(2026, 3, 29), time(9, 0), "Europe/Amsterdam")
		self.assertEqual(result.isoformat(), "2026-03-29T07:00:00")

	def test_amsterdam_dst_transition_changes_utc_offset(self):
		before = combine_local_time_to_utc(date(2026, 3, 28), time(9, 0), "Europe/Amsterdam")
		after = combine_local_time_to_utc(date(2026, 3, 29), time(9, 0), "Europe/Amsterdam")
		self.assertNotEqual(before.hour, after.hour)


MON_FRI_HOURS = [
	{"day": day, "start_time": "09:00:00", "end_time": "18:00:00"}
	for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
]


class TestBusinessHoursArithmetic(IntegrationTestCase):
	"""PHASE-3-SLA.md section 5 -- add_business_minutes and
	next_business_day_end, tested standalone before anything wires them into
	the ticket. All Singapore fixtures share Mon-Fri 09:00-18:00 (UTC+8, no
	DST), so expected results are exact minute arithmetic; Amsterdam is used
	only for the DST case.

	Reference calendar: in June 2026, Mon/Fri fall on 1&5, 8&12, 15&19,
	22&26, 29; Sat/Sun are 6-7, 13-14, 20-21, 27-28.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.no_holidays_calendar = _make_holiday_calendar("Test SLA No Holidays Calendar 2026", 2026, [])
		cls.city = _make_business_city(
			"Test SLA City",
			"Singapore",
			SGT,
			business_hours=MON_FRI_HOURS,
			holiday_calendar=cls.no_holidays_calendar.name,
		)

		cls.holiday_calendar = _make_holiday_calendar(
			"Test SLA Holiday Calendar 2026",
			2026,
			[date(2026, 6, 15), date(2026, 6, 22)],
		)
		cls.holiday_city = _make_business_city(
			"Test SLA Holiday City",
			"Singapore",
			SGT,
			business_hours=MON_FRI_HOURS,
			holiday_calendar=cls.holiday_calendar.name,
		)

		cls.no_hours_city = _make_business_city("Test SLA No Hours City", "Singapore", SGT)

		# Business hours but no holiday_calendar link at all -- the
		# "missing calendar" configuration-error case, distinct from
		# no_hours_city's missing-business-hours case.
		cls.no_calendar_city = _make_business_city(
			"Test SLA No Calendar City", "Singapore", SGT, business_hours=MON_FRI_HOURS
		)

		cls.amsterdam_city = _make_business_city(
			"Test SLA Amsterdam City",
			"Netherlands",
			"Europe/Amsterdam",
			business_hours=MON_FRI_HOURS,
			holiday_calendar=cls.no_holidays_calendar.name,
		)

	# -- add_business_minutes --------------------------------------------

	def test_mid_day_add_less_than_remaining_stays_same_day(self):
		start = get_datetime("2026-06-15 02:00:00")  # Monday 10:00 SGT
		result = add_business_minutes(start, 60, self.city.name)
		self.assertEqual(result, get_datetime("2026-06-15 03:00:00"))  # 11:00 SGT

	def test_mid_day_add_more_than_remaining_rolls_to_next_working_day(self):
		start = get_datetime("2026-06-15 09:00:00")  # Monday 17:00 SGT
		result = add_business_minutes(start, 120, self.city.name)
		self.assertEqual(result, get_datetime("2026-06-16 02:00:00"))  # Tuesday 10:00 SGT

	def test_start_on_saturday_begins_monday_morning(self):
		start = get_datetime("2026-06-20 02:00:00")  # Saturday 10:00 SGT
		result = add_business_minutes(start, 30, self.city.name)
		self.assertEqual(result, get_datetime("2026-06-22 01:30:00"))  # Monday 09:30 SGT

	def test_start_on_holiday_skips_it(self):
		start = get_datetime("2026-06-15 02:00:00")  # Monday 10:00 SGT, but a holiday
		result = add_business_minutes(start, 30, self.holiday_city.name)
		self.assertEqual(result, get_datetime("2026-06-16 01:30:00"))  # Tuesday 09:30 SGT

	def test_spanning_weekend_and_holiday_together(self):
		start = get_datetime("2026-06-19 09:00:00")  # Friday 17:00 SGT
		# 60 min left in Friday, 60 min remaining rolls over Sat/Sun, then
		# Monday 22nd is a holiday too -- lands on Tuesday.
		result = add_business_minutes(start, 120, self.holiday_city.name)
		self.assertEqual(result, get_datetime("2026-06-23 02:00:00"))  # Tuesday 10:00 SGT

	def test_start_before_opening_begins_at_opening(self):
		start = get_datetime("2026-06-15 23:00:00")  # Tuesday 07:00 SGT
		result = add_business_minutes(start, 15, self.city.name)
		self.assertEqual(result, get_datetime("2026-06-16 01:15:00"))  # Tuesday 09:15 SGT

	def test_start_after_closing_begins_next_working_day(self):
		start = get_datetime("2026-06-16 11:00:00")  # Tuesday 19:00 SGT
		result = add_business_minutes(start, 20, self.city.name)
		self.assertEqual(result, get_datetime("2026-06-17 01:20:00"))  # Wednesday 09:20 SGT

	def test_dst_transition_amsterdam_uses_correct_local_wall_clock(self):
		# Friday 27th is CET (UTC+1); by Monday 30th, Amsterdam has sprung
		# forward to CEST (UTC+2) -- the transition itself falls on Sunday
		# the 29th, a non-working day. 60 min finishes Friday, the
		# remaining 540 min is exactly Monday's full 09:00-18:00 window.
		start = get_datetime("2026-03-27 16:00:00")  # Friday 17:00 CET
		result = add_business_minutes(start, 600, self.amsterdam_city.name)
		self.assertEqual(result, get_datetime("2026-03-30 16:00:00"))  # Monday 18:00 CEST

	def test_no_business_hours_configured_raises(self):
		start = get_datetime("2026-06-15 02:00:00")
		with self.assertRaises(frappe.ValidationError):
			add_business_minutes(start, 30, self.no_hours_city.name)

	def test_missing_holiday_calendar_raises(self):
		start = get_datetime("2026-06-15 02:00:00")  # Monday 10:00 SGT, otherwise unremarkable
		with self.assertRaises(frappe.ValidationError):
			add_business_minutes(start, 30, self.no_calendar_city.name)

	def test_holiday_calendar_with_empty_holidays_table_is_valid(self):
		# self.city's calendar exists for 2026 but lists zero holidays --
		# a deliberate "no holidays" answer, not a missing one, so this
		# must behave exactly like test_mid_day_add_less_than_remaining.
		start = get_datetime("2026-06-15 02:00:00")  # Monday 10:00 SGT
		result = add_business_minutes(start, 60, self.city.name)
		self.assertEqual(result, get_datetime("2026-06-15 03:00:00"))  # 11:00 SGT

	# -- next_business_day_end -------------------------------------------

	def test_next_business_day_end_mid_week(self):
		start = get_datetime("2026-06-15 05:00:00")  # Monday 13:00 SGT
		result = next_business_day_end(start, self.city.name)
		self.assertEqual(result, get_datetime("2026-06-16 10:00:00"))  # Tuesday 18:00 SGT

	def test_next_business_day_end_rolls_over_weekend(self):
		start = get_datetime("2026-06-19 05:00:00")  # Friday 13:00 SGT
		result = next_business_day_end(start, self.city.name)
		self.assertEqual(result, get_datetime("2026-06-22 10:00:00"))  # Monday 18:00 SGT

	def test_next_business_day_end_skips_a_holiday(self):
		start = get_datetime("2026-06-19 05:00:00")  # Friday 13:00 SGT, Monday 22nd is a holiday
		result = next_business_day_end(start, self.holiday_city.name)
		self.assertEqual(result, get_datetime("2026-06-23 10:00:00"))  # Tuesday 18:00 SGT

	def test_next_business_day_end_no_business_hours_raises(self):
		start = get_datetime("2026-06-15 02:00:00")
		with self.assertRaises(frappe.ValidationError):
			next_business_day_end(start, self.no_hours_city.name)


class TestDatetimeBoundaryNormalisation(IntegrationTestCase):
	"""Regression coverage for the bug where apply_initial_sla_policy
	passed self.creation into _compute_due_datetime while it was still
	the string frappe.utils.now() assigns during before_insert (a DB
	round-trip is what casts creation/modified back to a real datetime;
	nothing in the insert() flow does that on the same in-memory object).
	Section 5's add_business_minutes/next_business_day_end tests never
	exercised this because they always pass get_datetime(...)-parsed
	values by construction -- the gap was specifically at the two places
	a raw pre-round-trip field can enter this module's arithmetic:
	_compute_due_datetime (via self.creation) and _minutes_between (same,
	via stamp_confirmation). Both are tested here directly, with plain
	ISO strings standing in for that unparsed field, rather than only via
	a full ticket insert -- so this fails fast and without DB-dependent
	scaffolding for the one case (the wall-clock branch) that doesn't
	need any.

	No other function in this module takes a datetime that might still be
	a pre-round-trip string. add_business_minutes/next_business_day_end
	are only ever called from _compute_due_datetime, which normalises
	before calling them -- they never see a raw field directly. Every
	other Datetime field this module sets (response_due, confirmed_at,
	first_response_at, actual_onsite_at, claimed_at) is assigned
	in-process via now_datetime() or a value this module itself computed
	-- never frappe.utils.now()'s string form, which is unique to
	creation/modified because Document.set_user_and_timestamp assigns
	those two from it directly."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.no_holidays_calendar = _make_holiday_calendar(
			"Test Boundary No Holidays Calendar 2026", 2026, []
		)
		cls.city = _make_business_city(
			"Test Boundary City",
			"Singapore",
			SGT,
			business_hours=MON_FRI_HOURS,
			holiday_calendar=cls.no_holidays_calendar.name,
		)

	# -- _compute_due_datetime, given an ISO string instead of a datetime -

	def test_wall_clock_branch_accepts_iso_string(self):
		result = _compute_due_datetime(
			"2026-06-15 02:00:00", 60, next_business_day=0, business_hours_only=0, city=self.city.name
		)
		self.assertEqual(result, get_datetime("2026-06-15 03:00:00"))

	def test_business_hours_only_branch_accepts_iso_string(self):
		result = _compute_due_datetime(
			"2026-06-15 02:00:00", 60, next_business_day=0, business_hours_only=1, city=self.city.name
		)
		self.assertEqual(result, add_business_minutes(get_datetime("2026-06-15 02:00:00"), 60, self.city.name))

	def test_next_business_day_branch_accepts_iso_string(self):
		result = _compute_due_datetime(
			"2026-06-15 02:00:00", 0, next_business_day=1, business_hours_only=0, city=self.city.name
		)
		self.assertEqual(result, next_business_day_end(get_datetime("2026-06-15 02:00:00"), self.city.name))

	def test_no_target_returns_none_regardless_of_string_input(self):
		result = _compute_due_datetime(
			"2026-06-15 02:00:00", 0, next_business_day=0, business_hours_only=0, city=self.city.name
		)
		self.assertIsNone(result)

	# -- _minutes_between, given ISO strings instead of datetimes ---------

	def test_minutes_between_accepts_iso_strings(self):
		result = _minutes_between("2026-06-15 02:00:00", "2026-06-15 03:30:00")
		self.assertEqual(result, 90)

	def test_minutes_between_accepts_mixed_string_and_datetime(self):
		# The actual shape stamp_confirmation calls this with: self.creation
		# (string, pre-round-trip) against self.confirmed_at (already a
		# real datetime -- it's set via now_datetime(), not frappe's
		# string-returning now()).
		result = _minutes_between("2026-06-15 02:00:00", get_datetime("2026-06-15 04:00:00"))
		self.assertEqual(result, 120)


class TestSmartHandsRequestController(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Every ticket here defaults to severity P4 against a Standard
		# customer -- before_insert now requires an active AC SLA Policy
		# for that pair to exist (PHASE-3-SLA.md section 6).
		ensure_sla_policy(
			"Standard",
			"P4",
			response_minutes=480,
			onsite_minutes=0,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		cls.city = _make_city("Test SHR City", "Singapore", SGT)
		cls.data_center = _make_data_center("Test SHR DC", cls.city.name)
		cls.customer = _make_customer(
			"Test SHR Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.data_center.name}],
		)
		cls.engineer_user = _make_plain_user("shr_engineer@authcor.test")
		cls.engineer_profile = frappe.get_doc(
			doctype="AC Engineer Profile",
			user=cls.engineer_user.name,
			phone="+65 8000 0000",
			id_type="NRIC",
			id_number="S1234567A",
		).insert(ignore_permissions=True)
		cls.non_engineer_user = _make_plain_user("shr_non_engineer@authcor.test")

	def _make_ticket(self, **overrides):
		values = {
			"doctype": "AC Smart Hands Request",
			"customer": self.customer.name,
			"country": "Singapore",
			"city": self.city.name,
			"data_center": self.data_center.name,
			"subject": "Test ticket",
			"description": "Test description",
			"action_required": "Remote Hands",
		}
		values.update(overrides)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	# -- (a) slot auto-generation --------------------------------------

	def test_slot_auto_generation_starts_following_day(self):
		ticket = self._make_ticket()
		self.assertEqual(len(ticket.preferred_slots), 7)
		start = add_days(getdate(), 1)
		for offset, row in enumerate(ticket.preferred_slots):
			self.assertEqual(getdate(row.slot_date), add_days(start, offset))
			self.assertEqual((row.band_morning, row.band_afternoon, row.band_overnight), (1, 1, 1))

	def test_slot_auto_generation_skipped_when_slots_supplied(self):
		supplied_date = add_days(getdate(), 3)
		ticket = self._make_ticket(
			preferred_slots=[{"slot_date": supplied_date, "band_morning": 1}],
		)
		self.assertEqual(len(ticket.preferred_slots), 1)
		self.assertEqual(getdate(ticket.preferred_slots[0].slot_date), supplied_date)

	# -- (b) engineers_required / action_other_details -------------------

	def test_engineers_required_zero_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._make_ticket(engineers_required=0)

	def test_engineers_required_one_is_accepted(self):
		ticket = self._make_ticket(engineers_required=1)
		self.assertEqual(ticket.engineers_required, 1)

	def test_others_action_requires_details(self):
		with self.assertRaises(frappe.ValidationError):
			self._make_ticket(action_required="Others", action_other_details="")

	def test_others_action_with_details_accepted(self):
		ticket = self._make_ticket(action_required="Others", action_other_details="Custom work")
		self.assertEqual(ticket.action_other_details, "Custom work")

	def test_non_others_action_does_not_require_details(self):
		ticket = self._make_ticket(action_required="Remote Hands")
		self.assertFalse(ticket.action_other_details)

	# -- (c) confirmed slot validation + UTC conversion -------------------

	def test_confirmed_time_inside_ticked_band_accepted_and_converted_to_utc(self):
		confirmed_date = add_days(getdate(), 5)
		ticket = self._make_ticket(
			preferred_slots=[{"slot_date": confirmed_date, "band_morning": 1}],
			confirmed_date=confirmed_date,
			confirmed_time="09:00",
		)
		self.assertEqual(
			get_datetime(ticket.confirmed_datetime),
			get_datetime(f"{confirmed_date.isoformat()} 01:00:00"),
		)

	def test_confirmed_time_outside_ticked_band_rejected(self):
		confirmed_date = add_days(getdate(), 5)
		with self.assertRaises(frappe.ValidationError):
			self._make_ticket(
				preferred_slots=[{"slot_date": confirmed_date, "band_afternoon": 1}],
				confirmed_date=confirmed_date,
				confirmed_time="09:00",
			)

	def test_confirmed_time_before_9am_matches_previous_day_overnight_band(self):
		confirmed_date = add_days(getdate(), 5)
		previous_day = add_days(confirmed_date, -1)
		ticket = self._make_ticket(
			preferred_slots=[{"slot_date": previous_day, "band_overnight": 1}],
			confirmed_date=confirmed_date,
			confirmed_time="07:00",
		)
		self.assertEqual(
			get_datetime(ticket.confirmed_datetime),
			get_datetime(f"{previous_day.isoformat()} 23:00:00"),
		)

	def test_confirmed_time_before_9am_rejected_if_only_same_day_overnight_ticked(self):
		"""The band belongs to the previous day's row -- ticking overnight
		on the confirmed date itself does not satisfy it."""
		confirmed_date = add_days(getdate(), 5)
		with self.assertRaises(frappe.ValidationError):
			self._make_ticket(
				preferred_slots=[{"slot_date": confirmed_date, "band_overnight": 1}],
				confirmed_date=confirmed_date,
				confirmed_time="07:00",
			)

	def test_unconfirmed_ticket_skips_slot_validation(self):
		# No confirmed_date/confirmed_time -- must not raise regardless of
		# what preferred_slots contains.
		ticket = self._make_ticket()
		self.assertIsNone(ticket.confirmed_datetime)

	# -- (e) completion stamping ------------------------------------------

	def test_completing_stamps_completed_on_and_engineer_profile(self):
		ticket = self._make_ticket()
		frappe.set_user(self.engineer_user.name)
		try:
			ticket.status = "Completed"
			ticket.save(ignore_permissions=True)
		finally:
			frappe.set_user("Administrator")

		self.assertIsNotNone(ticket.completed_on)
		self.assertEqual(ticket.completed_by, self.engineer_profile.name)

	def test_completing_leaves_completed_by_blank_without_engineer_profile(self):
		ticket = self._make_ticket()
		frappe.set_user(self.non_engineer_user.name)
		try:
			ticket.status = "Completed"
			ticket.save(ignore_permissions=True)
		finally:
			frappe.set_user("Administrator")

		self.assertIsNotNone(ticket.completed_on)
		self.assertFalse(ticket.completed_by)

	def test_reopening_clears_completion_fields(self):
		ticket = self._make_ticket()
		ticket.status = "Completed"
		ticket.save(ignore_permissions=True)
		self.assertIsNotNone(ticket.completed_on)

		ticket.status = "In Progress"
		ticket.save(ignore_permissions=True)

		self.assertIsNone(ticket.completed_on)
		self.assertFalse(ticket.completed_by)

	def test_reediting_completed_ticket_does_not_overwrite_timestamp(self):
		ticket = self._make_ticket()
		ticket.status = "Completed"
		ticket.save(ignore_permissions=True)
		original_completed_on = ticket.completed_on

		ticket.target_equipment = "Switch in rack 4"
		ticket.save(ignore_permissions=True)

		self.assertEqual(ticket.completed_on, original_completed_on)


class TestCascadingLocationOptions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.city = _make_city("Test Cascade City", "Singapore", SGT)
		cls.other_city = _make_city("Test Cascade City Two", "Singapore", SGT)
		cls.dc1 = _make_data_center("Test Cascade DC1", cls.city.name)
		cls.dc2 = _make_data_center("Test Cascade DC2", cls.city.name)
		cls.customer = _make_customer(
			"Test Cascade Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc1.name}],
		)
		cls.unrestricted_customer = _make_customer(
			"Test Cascade Customer Unrestricted",
			service_areas=[{"country": "Singapore", "city": cls.city.name}],
		)

	def test_get_allowed_countries(self):
		self.assertEqual(get_allowed_countries(customer=self.customer.name), ["Singapore"])

	def test_get_allowed_cities_scoped_to_country(self):
		self.assertEqual(
			get_allowed_cities(customer=self.customer.name, country="Singapore"),
			[self.city.name],
		)

	def test_get_allowed_cities_empty_for_unrelated_country(self):
		self.assertEqual(get_allowed_cities(customer=self.customer.name, country="Malaysia"), [])

	def test_get_allowed_data_centers_restricted_to_specific_dc(self):
		self.assertEqual(
			get_allowed_data_centers(customer=self.customer.name, city=self.city.name),
			[self.dc1.name],
		)

	def test_get_allowed_data_centers_unrestricted_when_row_leaves_it_blank(self):
		result = get_allowed_data_centers(customer=self.unrestricted_customer.name, city=self.city.name)
		self.assertCountEqual(result, [self.dc1.name, self.dc2.name])

	def test_denies_access_to_customer_outside_permission(self):
		user = _make_plain_user("gate_cascade_user@authcor.test")
		frappe.set_user(user.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				get_allowed_countries(customer=self.customer.name)
		finally:
			frappe.set_user("Administrator")


class TestSlaPolicyResolution(IntegrationTestCase):
	"""PHASE-3-SLA.md section 6 -- sla_policy/response_due/onsite_due
	captured at creation, and the has_met_onsite_sla N/A case.

	Uses sla_fixtures.ensure_sla_policy (get-or-mutate, no commit) since
	the 16 real policy rows are Desk-managed data this test can't assume
	exists on a fresh site, or safely insert alongside on a shared one --
	autoname is service_level-severity, so there's no free combination to
	insert under. None of these tests call claim_ticket/add_engineer/
	remove_engineer, so nothing here forces a commit and the normal
	per-test rollback is enough cleanup."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.no_holidays_calendar = _make_holiday_calendar(
			"Test SLA Resolution No Holidays Calendar 2026", 2026, []
		)
		cls.city = _make_business_city(
			"Test SLA Resolution City",
			"Singapore",
			SGT,
			business_hours=MON_FRI_HOURS,
			holiday_calendar=cls.no_holidays_calendar.name,
		)
		cls.dc = _make_data_center("Test SLA Resolution DC", cls.city.name)

		cls.premium_customer = _make_customer(
			"Test SLA Resolution Premium Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
			service_level="Premium",
		)
		cls.standard_customer = _make_customer(
			"Test SLA Resolution Standard Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
			service_level="Standard",
		)
		cls.basic_customer = _make_customer(
			"Test SLA Resolution Basic Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
			service_level="Basic",
		)

	def _make_ticket(self, customer, severity="P4", **overrides):
		values = {
			"doctype": "AC Smart Hands Request",
			"customer": customer,
			"country": "Singapore",
			"city": self.city.name,
			"data_center": self.dc.name,
			"subject": "SLA resolution test ticket",
			"description": "Test",
			"action_required": "Remote Hands",
			"severity": severity,
		}
		values.update(overrides)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	def test_wall_clock_response_and_onsite_due(self):
		ensure_sla_policy(
			"Premium",
			"P1",
			response_minutes=60,
			onsite_minutes=180,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		ticket = self._make_ticket(self.premium_customer.name, severity="P1")

		# ticket.creation is still frappe.utils.now()'s string form on this
		# freshly-inserted in-memory object (no DB round-trip has cast it
		# back to a datetime) -- normalise before doing arithmetic on it
		# ourselves, same reasoning as _compute_due_datetime's fix.
		creation = get_datetime(ticket.creation)
		self.assertEqual(ticket.sla_policy, "Premium-P1")
		self.assertEqual(ticket.response_due, creation + timedelta(minutes=60))
		self.assertEqual(ticket.onsite_due, creation + timedelta(minutes=180))
		self.assertEqual(ticket.has_met_response_sla, "Pending")
		self.assertEqual(ticket.has_met_onsite_sla, "Pending")

	def test_no_onsite_target_resolves_to_na(self):
		ensure_sla_policy(
			"Basic",
			"P4",
			response_minutes=960,
			onsite_minutes=0,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		ticket = self._make_ticket(self.basic_customer.name, severity="P4")

		self.assertIsNone(ticket.onsite_due)
		self.assertEqual(ticket.has_met_onsite_sla, "N/A")

	def test_business_hours_only_due_dates_match_add_business_minutes(self):
		ensure_sla_policy(
			"Basic",
			"P3",
			response_minutes=480,
			onsite_minutes=2880,
			onsite_next_business_day=0,
			business_hours_only=1,
			response_next_business_day=0,
		)
		ticket = self._make_ticket(self.basic_customer.name, severity="P3")

		# ticket.creation is the string frappe.utils.now() assigns during
		# insert, not yet a datetime -- get_datetime here mirrors what
		# _compute_due_datetime now does internally, so this test's
		# "expected" computation isn't hit by the same bug it's guarding.
		creation = get_datetime(ticket.creation)
		self.assertEqual(ticket.response_due, add_business_minutes(creation, 480, self.city.name))
		self.assertEqual(ticket.onsite_due, add_business_minutes(creation, 2880, self.city.name))

	def test_response_next_business_day(self):
		# Temporarily configuring Premium-P2 this way for isolated wiring
		# verification -- not its real spec shape.
		ensure_sla_policy(
			"Premium",
			"P2",
			response_minutes=0,
			onsite_minutes=360,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=1,
		)
		ticket = self._make_ticket(self.premium_customer.name, severity="P2")

		self.assertEqual(
			ticket.response_due, next_business_day_end(get_datetime(ticket.creation), self.city.name)
		)

	def test_onsite_next_business_day(self):
		ensure_sla_policy(
			"Standard",
			"P2",
			response_minutes=120,
			onsite_minutes=0,
			onsite_next_business_day=1,
			business_hours_only=0,
			response_next_business_day=0,
		)
		ticket = self._make_ticket(self.standard_customer.name, severity="P2")

		self.assertEqual(
			ticket.onsite_due, next_business_day_end(get_datetime(ticket.creation), self.city.name)
		)
		self.assertEqual(ticket.has_met_onsite_sla, "Pending")

	def test_missing_active_policy_raises(self):
		ensure_sla_policy(
			"Basic",
			"P1",
			response_minutes=120,
			onsite_minutes=480,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
			is_active=0,
		)
		with self.assertRaises(frappe.ValidationError):
			self._make_ticket(self.basic_customer.name, severity="P1")


class TestSeverityEscalation(IntegrationTestCase):
	"""PHASE-3-SLA.md section 7."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.city = _make_city("Test Escalation City", "Singapore", SGT)
		cls.dc = _make_data_center("Test Escalation DC", cls.city.name)
		cls.customer = _make_customer(
			"Test Escalation Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
			service_level="Premium",
		)
		cls.admin_user = _make_plain_user("escalation_admin@authcor.test")
		cls.admin_user.add_roles("Admin L1")
		cls.client_user = _make_plain_user("escalation_client@authcor.test")
		cls.client_user.add_roles("Client L1 Authorizer")

		ensure_sla_policy(
			"Premium",
			"P4",
			response_minutes=360,
			onsite_minutes=0,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		ensure_sla_policy(
			"Premium",
			"P1",
			response_minutes=60,
			onsite_minutes=180,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)

	def _make_ticket(self, severity="P4", **overrides):
		values = {
			"doctype": "AC Smart Hands Request",
			"customer": self.customer.name,
			"country": "Singapore",
			"city": self.city.name,
			"data_center": self.dc.name,
			"subject": "Escalation test ticket",
			"description": "Test",
			"action_required": "Remote Hands",
			"severity": severity,
		}
		values.update(overrides)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	def test_escalation_records_history_and_recomputes_due_dates(self):
		ticket = self._make_ticket(severity="P4")
		old_response_due = ticket.response_due
		self.assertEqual(ticket.has_met_onsite_sla, "N/A")  # P4: no onsite target

		before = now_datetime()
		frappe.set_user(self.admin_user.name)
		try:
			ticket.severity = "P1"
			ticket.save(ignore_permissions=True)
		finally:
			frappe.set_user("Administrator")
		after = now_datetime()

		self.assertEqual(len(ticket.severity_history), 1)
		history_row = ticket.severity_history[0]
		self.assertEqual(history_row.old_severity, "P4")
		self.assertEqual(history_row.new_severity, "P1")
		self.assertEqual(history_row.old_response_due, old_response_due)
		self.assertEqual(history_row.old_verdict, "Pending")

		self.assertEqual(ticket.sla_policy, "Premium-P1")
		self.assertGreaterEqual(ticket.response_due, before + timedelta(minutes=60))
		self.assertLessEqual(ticket.response_due, after + timedelta(minutes=60))
		self.assertEqual(ticket.has_met_response_sla, "Pending")
		self.assertEqual(ticket.has_met_onsite_sla, "Pending")  # P1 now has an onsite target

	def test_escalation_does_not_overwrite_already_recorded_response_verdict(self):
		ticket = self._make_ticket(severity="P4")
		ticket.db_set({"first_response_at": now_datetime(), "has_met_response_sla": "Yes"})

		frappe.set_user(self.admin_user.name)
		try:
			ticket.severity = "P1"
			ticket.save(ignore_permissions=True)
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(ticket.has_met_response_sla, "Yes")
		self.assertEqual(ticket.severity_history[0].old_verdict, "Yes")

	def test_escalation_by_unauthorised_role_is_rejected(self):
		ticket = self._make_ticket(severity="P4")

		frappe.set_user(self.client_user.name)
		try:
			ticket.severity = "P1"
			with self.assertRaises(frappe.PermissionError):
				ticket.save(ignore_permissions=True)
		finally:
			frappe.set_user("Administrator")

	def test_saving_without_a_severity_change_does_not_touch_history(self):
		ticket = self._make_ticket(severity="P4")

		ticket.target_equipment = "Rack 2"
		ticket.save(ignore_permissions=True)

		self.assertEqual(len(ticket.severity_history), 0)


class TestOnsiteArrival(IntegrationTestCase):
	"""PHASE-3-SLA.md section 6 -- actual_onsite_at derived from the
	transition to In Progress (the chosen trigger, since the field is
	built read_only and so can't be plain manual Desk entry)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.city = _make_city("Test Onsite City", "Singapore", SGT)
		cls.dc = _make_data_center("Test Onsite DC", cls.city.name)
		cls.customer = _make_customer(
			"Test Onsite Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
			service_level="Premium",
		)
		ensure_sla_policy(
			"Premium",
			"P1",
			response_minutes=60,
			onsite_minutes=180,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		ensure_sla_policy(
			"Premium",
			"P4",
			response_minutes=360,
			onsite_minutes=0,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)

	def _make_ticket(self, severity="P1", **overrides):
		values = {
			"doctype": "AC Smart Hands Request",
			"customer": self.customer.name,
			"country": "Singapore",
			"city": self.city.name,
			"data_center": self.dc.name,
			"subject": "Onsite arrival test ticket",
			"description": "Test",
			"action_required": "Remote Hands",
			"severity": severity,
		}
		values.update(overrides)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	def test_transition_to_in_progress_stamps_actual_onsite_at_and_meets_sla(self):
		ticket = self._make_ticket(severity="P1")

		ticket.status = "In Progress"
		ticket.save(ignore_permissions=True)

		self.assertIsNotNone(ticket.actual_onsite_at)
		self.assertEqual(ticket.has_met_onsite_sla, "Yes")

	def test_transition_to_in_progress_past_onsite_due_is_a_miss(self):
		ticket = self._make_ticket(severity="P1")
		ticket.db_set("onsite_due", now_datetime() - timedelta(minutes=5))

		ticket.status = "In Progress"
		ticket.save(ignore_permissions=True)

		self.assertEqual(ticket.has_met_onsite_sla, "No")

	def test_resaving_in_progress_does_not_move_the_timestamp(self):
		ticket = self._make_ticket(severity="P1")
		ticket.status = "In Progress"
		ticket.save(ignore_permissions=True)
		original = ticket.actual_onsite_at

		ticket.target_equipment = "Rack 9"
		ticket.save(ignore_permissions=True)

		self.assertEqual(ticket.actual_onsite_at, original)

	def test_no_onsite_target_stays_na_even_in_progress(self):
		ticket = self._make_ticket(severity="P4")

		ticket.status = "In Progress"
		ticket.save(ignore_permissions=True)

		self.assertIsNotNone(ticket.actual_onsite_at)
		self.assertEqual(ticket.has_met_onsite_sla, "N/A")


class TestSlaBreachDetection(IntegrationTestCase):
	"""PHASE-3-SLA.md section 8 -- the scheduled sweep. check_sla_breaches
	commits internally (it's meant to run as a background job against its
	own connection), which breaks IntegrationTestCase's rollback for
	everything already pending -- same reason TestClaimTicket manages its
	own teardown. Fixtures here are committed and explicitly cleaned up."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.city = _make_city("Test Breach City", "Singapore", SGT)
		cls.dc = _make_data_center("Test Breach DC", cls.city.name)
		cls.customer = _make_customer(
			"Test Breach Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
			service_level="Premium",
		)
		ensure_sla_policy_committed(
			cls.addClassCleanup,
			"Premium",
			"P1",
			response_minutes=60,
			onsite_minutes=180,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		frappe.db.commit()
		# addClassCleanup runs in LIFO order -- registered here so execution
		# order comes out dependency-safe: customer, then dc, then city,
		# then a final commit to persist all three deletes.
		cls.addClassCleanup(frappe.db.commit)
		cls.addClassCleanup(frappe.delete_doc, "AC City", cls.city.name, force=True, ignore_permissions=True)
		cls.addClassCleanup(frappe.delete_doc, "AC Data Center", cls.dc.name, force=True, ignore_permissions=True)
		cls.addClassCleanup(frappe.delete_doc, "AC Customer", cls.customer.name, force=True, ignore_permissions=True)

	def _make_ticket(self, **overrides):
		values = {
			"doctype": "AC Smart Hands Request",
			"customer": self.customer.name,
			"country": "Singapore",
			"city": self.city.name,
			"data_center": self.dc.name,
			"subject": "Breach detection test ticket",
			"description": "Test",
			"action_required": "Remote Hands",
			"severity": "P1",
		}
		values.update(overrides)
		ticket = frappe.get_doc(values).insert(ignore_permissions=True)
		frappe.db.commit()
		# LIFO again: commit registered first so it executes after the delete.
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.delete_doc, "AC Smart Hands Request", ticket.name, force=True, ignore_permissions=True)
		return ticket

	def test_overdue_pending_response_is_marked_missed(self):
		ticket = self._make_ticket()
		ticket.db_set("response_due", now_datetime() - timedelta(minutes=1))
		frappe.db.commit()

		check_sla_breaches()

		self.assertEqual(frappe.db.get_value("AC Smart Hands Request", ticket.name, "has_met_response_sla"), "No")

	def test_overdue_pending_onsite_is_marked_missed(self):
		ticket = self._make_ticket()
		ticket.db_set("onsite_due", now_datetime() - timedelta(minutes=1))
		frappe.db.commit()

		check_sla_breaches()

		self.assertEqual(frappe.db.get_value("AC Smart Hands Request", ticket.name, "has_met_onsite_sla"), "No")

	def test_not_yet_due_is_left_pending(self):
		ticket = self._make_ticket()
		frappe.db.commit()

		check_sla_breaches()

		self.assertEqual(frappe.db.get_value("AC Smart Hands Request", ticket.name, "has_met_response_sla"), "Pending")

	def test_already_resolved_verdict_is_left_alone(self):
		ticket = self._make_ticket()
		ticket.db_set("response_due", now_datetime() - timedelta(minutes=1))
		ticket.db_set("has_met_response_sla", "Yes")
		frappe.db.commit()

		check_sla_breaches()

		self.assertEqual(frappe.db.get_value("AC Smart Hands Request", ticket.name, "has_met_response_sla"), "Yes")
