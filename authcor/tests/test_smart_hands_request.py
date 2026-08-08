from datetime import date, time

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, get_datetime, getdate

from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	combine_local_time_to_utc,
	get_allowed_cities,
	get_allowed_countries,
	get_allowed_data_centers,
	resolve_owning_slot,
)

SGT = "Asia/Singapore"


def _make_city(city_name, country, timezone):
	return frappe.get_doc(
		doctype="AC City",
		city_name=city_name,
		country=country,
		timezone=timezone,
	).insert(ignore_permissions=True)


def _make_data_center(data_center_name, city):
	return frappe.get_doc(
		doctype="AC Data Center",
		data_center_name=data_center_name,
		city=city,
	).insert(ignore_permissions=True)


def _make_customer(customer_name, service_areas):
	return frappe.get_doc(
		doctype="AC Customer",
		customer_name=customer_name,
		service_level="Standard",
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


class TestSmartHandsRequestController(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
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
