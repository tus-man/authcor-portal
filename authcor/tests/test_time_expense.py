from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from authcor.authcor.doctype.ac_expense_claim.ac_expense_claim import (
	approve_claim,
	mark_reimbursed,
	reject_claim,
	submit_claim,
)
from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import add_engineer
from authcor.authcor.doctype.ac_time_entry.ac_time_entry import derive_rate_category
from authcor.tasks import send_unlogged_hours_nudges
from authcor.tests.sla_fixtures import ensure_sla_policy_committed

SGT = "Asia/Singapore"

# All fixtures share the reference calendar from test_smart_hands_request.py's
# TestBusinessHoursArithmetic: in June 2026, Mon/Fri fall on 1&5, 8&12, 15&19,
# 22&26, 29; Sat/Sun are 6-7, 13-14, 20-21, 27-28. Singapore is UTC+8, no DST,
# so local-time expectations below are exact minute arithmetic on the stored
# naive-UTC datetimes.
MON_FRI_HOURS = [
	{"day": day, "start_time": "09:00:00", "end_time": "18:00:00"}
	for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
]
HOLIDAY_DATE = "2026-06-16"  # Tuesday -- inside business hours, but a holiday


def _make_city(name, business_hours, holiday_calendar):
	return frappe.get_doc(
		doctype="AC City",
		city_name=name,
		country="Singapore",
		timezone=SGT,
		business_hours=business_hours,
		holiday_calendar=holiday_calendar,
	).insert(ignore_permissions=True)


def _make_holiday_calendar(name, year, holiday_dates):
	return frappe.get_doc(
		doctype="AC Holiday Calendar",
		calendar_name=name,
		year=year,
		holidays=[{"holiday_date": d} for d in holiday_dates],
	).insert(ignore_permissions=True)


def _make_data_center(name, city):
	return frappe.get_doc(doctype="AC Data Center", data_center_name=name, city=city).insert(
		ignore_permissions=True
	)


def _make_engineer(email):
	user = frappe.get_doc(
		doctype="User", email=email, first_name=email.split("@", 1)[0], send_welcome_email=0
	).insert(ignore_permissions=True)
	user.add_roles("Operations L3 Authorizer")
	profile = frappe.get_doc(
		doctype="AC Engineer Profile",
		user=user.name,
		phone="+65 8000 0000",
		id_type="NRIC",
		id_number="S1234567A",
		is_active=1,
	).insert(ignore_permissions=True)
	return user, profile


def _make_user(email, roles):
	user = frappe.get_doc(
		doctype="User", email=email, first_name=email.split("@", 1)[0], send_welcome_email=0
	).insert(ignore_permissions=True)
	for role in roles:
		user.add_roles(role)
	return user


def _make_customer(name, authorised_engineers, portal_users=None):
	return frappe.get_doc(
		doctype="AC Customer",
		customer_name=name,
		service_level="Standard",
		authorised_engineers=[{"engineer": u} for u in authorised_engineers],
		portal_users=[
			{"user": u, "authorizer_level": "L1 Authorizer", "is_active": 1} for u in (portal_users or [])
		],
	).insert(ignore_permissions=True)


def _grant_customer_access(user, customer):
	frappe.get_doc(
		doctype="User Permission",
		user=user,
		allow="AC Customer",
		for_value=customer,
		apply_to_all_doctypes=1,
	).insert(ignore_permissions=True)


def _ensure_currency(code):
	if not frappe.db.exists("Currency", code):
		frappe.get_doc(doctype="Currency", currency_name=code, enabled=1).insert(ignore_permissions=True)
	return code


def _make_ticket(customer, city, data_center, engineers_required=2):
	return frappe.get_doc(
		doctype="AC Smart Hands Request",
		customer=customer,
		country="Singapore",
		city=city,
		data_center=data_center,
		subject="Time/expense test ticket",
		description="Test",
		action_required="Remote Hands",
		engineers_required=engineers_required,
		status="In Pool",
	).insert(ignore_permissions=True)


def _delete_ticket(ticket_name):
	for te in frappe.get_all("AC Time Entry", filters={"ticket": ticket_name}, pluck="name"):
		doc = frappe.get_doc("AC Time Entry", te)
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("AC Time Entry", te, force=True, ignore_permissions=True)
	for exp in frappe.get_all("AC Expense Claim", filters={"ticket": ticket_name}, pluck="name"):
		frappe.delete_doc("AC Expense Claim", exp, force=True, ignore_permissions=True)
	for log in frappe.get_all("AC ID Disclosure Log", filters={"ticket": ticket_name}, pluck="name"):
		frappe.delete_doc("AC ID Disclosure Log", log, force=True, ignore_permissions=True)
	if frappe.db.exists("AC Smart Hands Request", ticket_name):
		frappe.delete_doc("AC Smart Hands Request", ticket_name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _cleanup_fixtures(user_emails, customer_names, dc_name, city_name, calendar_name):
	for name in customer_names:
		if frappe.db.exists("AC Customer", name):
			frappe.delete_doc("AC Customer", name, force=True, ignore_permissions=True)
	for email in user_emails:
		profile_name = frappe.db.get_value("AC Engineer Profile", {"user": email}, "name")
		if profile_name:
			frappe.delete_doc("AC Engineer Profile", profile_name, force=True, ignore_permissions=True)
		for perm in frappe.get_all("User Permission", filters={"user": email}, pluck="name"):
			frappe.delete_doc("User Permission", perm, force=True, ignore_permissions=True)
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	if frappe.db.exists("AC Data Center", dc_name):
		frappe.delete_doc("AC Data Center", dc_name, force=True, ignore_permissions=True)
	if frappe.db.exists("AC City", city_name):
		frappe.delete_doc("AC City", city_name, force=True, ignore_permissions=True)
	if frappe.db.exists("AC Holiday Calendar", calendar_name):
		frappe.delete_doc("AC Holiday Calendar", calendar_name, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestTimeExpenseCapture(IntegrationTestCase):
	"""Every whitelisted method here ends with an explicit frappe.db.commit()
	(AC Time Entry submit/cancel, AC Expense Claim's decision methods) --
	same reasoning as TestEngineerCountChange in test_engineer_count.py, so
	this class manages its own fixture commit/teardown instead of relying
	on IntegrationTestCase's usual rollback."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		ensure_sla_policy_committed(
			cls.addClassCleanup,
			"Standard",
			"P4",
			response_minutes=480,
			onsite_minutes=0,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)

		cls.calendar = _make_holiday_calendar("Test TE Holiday Calendar 2026", 2026, [HOLIDAY_DATE])
		cls.city = _make_city("Test TE City", MON_FRI_HOURS, cls.calendar.name)
		cls.dc = _make_data_center("Test TE DC", cls.city.name)
		_ensure_currency("SGD")

		cls.engineer_a, cls.profile_a = _make_engineer("te_eng_a@authcor.test")
		cls.engineer_b, cls.profile_b = _make_engineer("te_eng_b@authcor.test")

		cls.admin_user = _make_user("te_admin@authcor.test", roles=["Admin L1"])
		cls.ops_l1_user = _make_user("te_ops_l1@authcor.test", roles=["Operations L1 Authorizer"])
		cls.portal_user = _make_user("te_portal@authcor.test", roles=["Client L1 Authorizer"])

		cls.customer = _make_customer(
			"Test TE Customer",
			authorised_engineers=[cls.engineer_a.name, cls.engineer_b.name],
			portal_users=[cls.portal_user.name],
		)
		frappe.db.commit()

		_grant_customer_access(cls.portal_user.name, cls.customer.name)
		frappe.db.commit()

		cls.addClassCleanup(
			_cleanup_fixtures,
			[cls.engineer_a.name, cls.engineer_b.name, cls.admin_user.name, cls.ops_l1_user.name, cls.portal_user.name],
			[cls.customer.name],
			cls.dc.name,
			cls.city.name,
			cls.calendar.name,
		)

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.set_user, "Administrator")

	def _new_ticket(self, engineers_required=2):
		ticket = _make_ticket(self.customer.name, self.city.name, self.dc.name, engineers_required)
		frappe.db.commit()
		self.addCleanup(_delete_ticket, ticket.name)
		return ticket

	def _reload(self, ticket_name):
		return frappe.get_doc("AC Smart Hands Request", ticket_name)

	def _assign(self, ticket_name, engineer):
		frappe.set_user(self.admin_user.name)
		add_engineer(ticket_name, engineer)
		frappe.set_user("Administrator")

	def _make_time_entry(self, ticket_name, engineer, start_time, end_time, assign=True):
		if assign and engineer not in {row.engineer for row in self._reload(ticket_name).assigned_engineers}:
			self._assign(ticket_name, engineer)
		return frappe.get_doc(
			doctype="AC Time Entry",
			ticket=ticket_name,
			engineer=engineer,
			work_date=start_time.split(" ")[0],
			start_time=start_time,
			end_time=end_time,
		).insert(ignore_permissions=True)

	def _make_expense_claim(self, ticket_name, engineer, **overrides):
		values = {
			"doctype": "AC Expense Claim",
			"ticket": ticket_name,
			"engineer": engineer,
			"expense_date": "2026-06-15",
			"category": "Transport",
			"description": "Taxi to data centre",
			"amount": 50,
			"currency": "SGD",
			"receipt": "/files/receipt.pdf",
		}
		values.update(overrides)
		return frappe.get_doc(values).insert(ignore_permissions=True)

	# -- section 2: duration and time validation -----------------------------

	def test_duration_computed_correctly(self):
		ticket = self._new_ticket()
		entry = self._make_time_entry(
			ticket.name, self.engineer_a.name, "2026-06-15 06:00:00", "2026-06-15 07:30:00"
		)
		self.assertEqual(entry.duration_minutes, 90)
		self.assertEqual(entry.duration_display, "1h 30m")

	def test_end_time_before_start_time_rejected(self):
		ticket = self._new_ticket()
		with self.assertRaises(frappe.ValidationError):
			self._make_time_entry(
				ticket.name, self.engineer_a.name, "2026-06-15 08:00:00", "2026-06-15 07:00:00"
			)

	def test_duration_over_24_hours_rejected(self):
		ticket = self._new_ticket()
		with self.assertRaises(frappe.ValidationError):
			self._make_time_entry(
				ticket.name, self.engineer_a.name, "2026-06-15 06:00:00", "2026-06-16 07:01:00"
			)

	def test_future_start_time_rejected(self):
		ticket = self._new_ticket()
		future_start = add_to_date(now_datetime(), days=1)
		with self.assertRaises(frappe.ValidationError):
			self._make_time_entry(
				ticket.name,
				self.engineer_a.name,
				future_start.strftime("%Y-%m-%d %H:%M:%S"),
				add_to_date(future_start, hours=1).strftime("%Y-%m-%d %H:%M:%S"),
			)

	def test_overlap_warns_not_blocks(self):
		ticket = self._new_ticket()
		self._make_time_entry(ticket.name, self.engineer_a.name, "2026-06-15 07:00:00", "2026-06-15 08:00:00")

		frappe.clear_messages()
		entry = self._make_time_entry(
			ticket.name, self.engineer_a.name, "2026-06-15 07:30:00", "2026-06-15 08:30:00"
		)
		self.assertTrue(entry.name)

		messages = frappe.get_message_log()
		self.assertTrue(any("overlap" in m.get("message", "").lower() for m in messages))

	def test_submitted_entry_cannot_be_edited(self):
		ticket = self._new_ticket()
		entry = self._make_time_entry(
			ticket.name, self.engineer_a.name, "2026-06-15 06:00:00", "2026-06-15 07:00:00"
		)
		entry.submit()

		reloaded = frappe.get_doc("AC Time Entry", entry.name)
		reloaded.notes = "Trying to edit after submission"
		with self.assertRaises(frappe.ValidationError):
			reloaded.save()

	# -- section 2: rate category derivation ----------------------------------

	def test_rate_category_weekday_afternoon_is_standard(self):
		self.assertEqual(derive_rate_category(self.city.name, "2026-06-15 06:00:00"), "Standard")

	def test_rate_category_early_morning_is_after_hours(self):
		# 2026-06-14 19:00 UTC == 2026-06-15 (Monday) 03:00 SGT
		self.assertEqual(derive_rate_category(self.city.name, "2026-06-14 19:00:00"), "After Hours")

	def test_rate_category_sunday_is_weekend(self):
		self.assertEqual(derive_rate_category(self.city.name, "2026-06-14 06:00:00"), "Weekend")

	def test_rate_category_holiday_beats_business_hours(self):
		self.assertEqual(derive_rate_category(self.city.name, "2026-06-16 06:00:00"), "Holiday")

	def test_rate_category_stamped_on_time_entry(self):
		ticket = self._new_ticket()
		entry = self._make_time_entry(
			ticket.name, self.engineer_a.name, "2026-06-15 06:00:00", "2026-06-15 07:00:00"
		)
		self.assertEqual(entry.rate_category, "Standard")

	# -- section 2: engineer must be assigned ---------------------------------

	def test_engineer_not_assigned_rejected_but_ops_l1_succeeds(self):
		ticket = self._new_ticket(engineers_required=2)
		self._assign(ticket.name, self.engineer_a.name)  # engineer_b left unassigned

		frappe.set_user(self.engineer_b.name)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				doctype="AC Time Entry",
				ticket=ticket.name,
				engineer=self.engineer_b.name,
				work_date="2026-06-15",
				start_time="2026-06-15 06:00:00",
				end_time="2026-06-15 07:00:00",
			).insert(ignore_permissions=True)
		frappe.set_user("Administrator")

		frappe.set_user(self.ops_l1_user.name)
		entry = frappe.get_doc(
			doctype="AC Time Entry",
			ticket=ticket.name,
			engineer=self.engineer_b.name,
			work_date="2026-06-15",
			start_time="2026-06-15 06:00:00",
			end_time="2026-06-15 07:00:00",
		).insert(ignore_permissions=True)
		frappe.set_user("Administrator")

		self.assertTrue(entry.name)

	def test_non_privileged_engineer_cannot_log_time_for_another_assigned_engineer(self):
		ticket = self._new_ticket(engineers_required=2)
		self._assign(ticket.name, self.engineer_a.name)
		self._assign(ticket.name, self.engineer_b.name)  # both legitimately assigned

		frappe.set_user(self.engineer_a.name)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				doctype="AC Time Entry",
				ticket=ticket.name,
				engineer=self.engineer_b.name,  # assigned, but not the caller
				work_date="2026-06-15",
				start_time="2026-06-15 06:00:00",
				end_time="2026-06-15 07:00:00",
			).insert(ignore_permissions=True)

		entry = frappe.get_doc(
			doctype="AC Time Entry",
			ticket=ticket.name,
			engineer=self.engineer_a.name,  # logging their own time still works
			work_date="2026-06-15",
			start_time="2026-06-15 06:00:00",
			end_time="2026-06-15 07:00:00",
		).insert(ignore_permissions=True)
		frappe.set_user("Administrator")

		self.assertTrue(entry.name)

	def test_non_privileged_caller_cannot_edit_engineer_on_existing_time_entry(self):
		ticket = self._new_ticket(engineers_required=2)
		self._assign(ticket.name, self.engineer_a.name)
		self._assign(ticket.name, self.engineer_b.name)

		entry = frappe.get_doc(
			doctype="AC Time Entry",
			ticket=ticket.name,
			engineer=self.engineer_a.name,
			work_date="2026-06-15",
			start_time="2026-06-15 06:00:00",
			end_time="2026-06-15 07:00:00",
		).insert(ignore_permissions=True)

		# engineer_b isn't the owner -- touching the record at all is rejected,
		# even without changing engineer.
		frappe.set_user(self.engineer_b.name)
		other_view = frappe.get_doc("AC Time Entry", entry.name)
		other_view.notes = "Sneaking an edit in"
		with self.assertRaises(frappe.ValidationError):
			other_view.save(ignore_permissions=True)
		frappe.set_user("Administrator")

		# engineer_a owns it, but reassigning engineer away from themselves
		# is rejected.
		frappe.set_user(self.engineer_a.name)
		reassign = frappe.get_doc("AC Time Entry", entry.name)
		reassign.engineer = self.engineer_b.name
		with self.assertRaises(frappe.ValidationError):
			reassign.save(ignore_permissions=True)

		# engineer_a editing their own entry without reassigning still works.
		own_edit = frappe.get_doc("AC Time Entry", entry.name)
		own_edit.notes = "Legitimate edit"
		own_edit.save(ignore_permissions=True)
		frappe.set_user("Administrator")

		self.assertEqual(frappe.get_doc("AC Time Entry", entry.name).notes, "Legitimate edit")

	# -- section 4: rollups ----------------------------------------------------

	def test_ticket_rollup_updates_on_submit_and_reverts_on_cancel(self):
		ticket = self._new_ticket()
		entry = self._make_time_entry(
			ticket.name, self.engineer_a.name, "2026-06-15 06:00:00", "2026-06-15 08:00:00"
		)
		self.assertEqual(self._reload(ticket.name).total_hours, 0)

		entry.submit()
		self.assertEqual(self._reload(ticket.name).total_hours, 2)

		entry.cancel()
		self.assertEqual(self._reload(ticket.name).total_hours, 0)

	def test_rollup_counts_approved_expenses_only(self):
		ticket = self._new_ticket()
		self._make_expense_claim(ticket.name, self.engineer_a.name, amount=10)  # stays Draft

		submitted = self._make_expense_claim(ticket.name, self.engineer_a.name, amount=20)
		submitted.status = "Submitted"
		submitted.save(ignore_permissions=True)  # stays Submitted, never decided

		to_approve = self._make_expense_claim(ticket.name, self.engineer_a.name, amount=30)
		to_approve.status = "Submitted"
		to_approve.save(ignore_permissions=True)
		to_reject = self._make_expense_claim(ticket.name, self.engineer_a.name, amount=40)
		to_reject.status = "Submitted"
		to_reject.save(ignore_permissions=True)

		frappe.set_user(self.ops_l1_user.name)
		approve_claim(to_approve.name)
		reject_claim(to_reject.name, reason="Not a valid expense")
		frappe.set_user("Administrator")

		self.assertEqual(self._reload(ticket.name).total_expenses, 30)

	# -- section 3: expense claims ----------------------------------------------

	def test_non_privileged_user_cannot_create_claim_for_another_engineer(self):
		ticket = self._new_ticket()

		frappe.set_user(self.engineer_a.name)
		with self.assertRaises(frappe.ValidationError):
			self._make_expense_claim(ticket.name, self.engineer_b.name)  # naming a colleague

		own_claim = self._make_expense_claim(ticket.name, self.engineer_a.name)  # naming self still works
		frappe.set_user("Administrator")

		self.assertTrue(own_claim.name)

	def test_ops_l1_can_create_claim_on_engineers_behalf(self):
		ticket = self._new_ticket()

		frappe.set_user(self.ops_l1_user.name)
		claim = self._make_expense_claim(ticket.name, self.engineer_b.name)
		frappe.set_user("Administrator")

		self.assertEqual(claim.engineer, self.engineer_b.name)

	def test_non_privileged_caller_cannot_edit_engineer_on_existing_claim(self):
		ticket = self._new_ticket()
		claim = self._make_expense_claim(ticket.name, self.engineer_a.name)

		# engineer_b isn't the owner -- touching the record at all is
		# rejected, even without changing engineer.
		frappe.set_user(self.engineer_b.name)
		other_view = frappe.get_doc("AC Expense Claim", claim.name)
		other_view.description = "Sneaking an edit in"
		with self.assertRaises(frappe.ValidationError):
			other_view.save(ignore_permissions=True)
		frappe.set_user("Administrator")

		# engineer_a owns it, but reassigning engineer away from themselves
		# is rejected.
		frappe.set_user(self.engineer_a.name)
		reassign = frappe.get_doc("AC Expense Claim", claim.name)
		reassign.engineer = self.engineer_b.name
		with self.assertRaises(frappe.ValidationError):
			reassign.save(ignore_permissions=True)

		# engineer_a editing their own claim without reassigning still works.
		own_edit = frappe.get_doc("AC Expense Claim", claim.name)
		own_edit.description = "Legitimate edit"
		own_edit.save(ignore_permissions=True)
		frappe.set_user("Administrator")

		self.assertEqual(frappe.get_doc("AC Expense Claim", claim.name).description, "Legitimate edit")

	def test_expense_without_receipt_rejected(self):
		ticket = self._new_ticket()
		with self.assertRaises(frappe.ValidationError):
			self._make_expense_claim(ticket.name, self.engineer_a.name, receipt=None)

	def test_reject_without_reason_rejected(self):
		ticket = self._new_ticket()
		claim = self._make_expense_claim(ticket.name, self.engineer_a.name)
		claim.status = "Submitted"
		claim.save(ignore_permissions=True)

		frappe.set_user(self.ops_l1_user.name)
		with self.assertRaises(frappe.ValidationError):
			reject_claim(claim.name)
		frappe.set_user("Administrator")

	def test_submit_approve_reimburse_flow(self):
		ticket = self._new_ticket()
		claim = self._make_expense_claim(ticket.name, self.engineer_a.name, amount=75)

		frappe.set_user(self.engineer_a.name)
		submitted = submit_claim(claim.name)
		self.assertEqual(submitted.status, "Submitted")
		frappe.set_user("Administrator")

		frappe.set_user(self.ops_l1_user.name)
		approved = approve_claim(claim.name)
		self.assertEqual(approved.status, "Approved")
		self.assertEqual(approved.approved_by, self.ops_l1_user.name)
		self.assertIsNotNone(approved.approved_on)

		reimbursed = mark_reimbursed(claim.name)
		self.assertEqual(reimbursed.status, "Reimbursed")
		frappe.set_user("Administrator")

		self.assertEqual(self._reload(ticket.name).total_expenses, 75)

	# -- section 3: client visibility (Approved/Reimbursed only) ---------------

	def test_client_sees_approved_expense_but_not_draft_submitted_rejected(self):
		ticket = self._new_ticket()
		draft = self._make_expense_claim(ticket.name, self.engineer_a.name, amount=11)

		submitted = self._make_expense_claim(ticket.name, self.engineer_a.name, amount=12)
		submitted.status = "Submitted"
		submitted.save(ignore_permissions=True)

		rejected = self._make_expense_claim(ticket.name, self.engineer_a.name, amount=13)
		rejected.status = "Submitted"
		rejected.save(ignore_permissions=True)
		frappe.set_user(self.ops_l1_user.name)
		reject_claim(rejected.name, reason="Nope")
		frappe.set_user("Administrator")

		approved = self._make_expense_claim(ticket.name, self.engineer_a.name, amount=14)
		approved.status = "Submitted"
		approved.save(ignore_permissions=True)
		frappe.set_user(self.ops_l1_user.name)
		approve_claim(approved.name)
		frappe.set_user("Administrator")

		frappe.set_user(self.portal_user.name)
		visible = {
			row.name
			for row in frappe.get_list("AC Expense Claim", fields=["name"], limit_page_length=0)
		}
		self.assertIn(approved.name, visible)
		self.assertNotIn(draft.name, visible)
		self.assertNotIn(submitted.name, visible)
		self.assertNotIn(rejected.name, visible)

		self.assertTrue(
			frappe.has_permission("AC Expense Claim", doc=approved.name, user=self.portal_user.name)
		)
		self.assertFalse(
			frappe.has_permission("AC Expense Claim", doc=draft.name, user=self.portal_user.name)
		)
		frappe.set_user("Administrator")

	# -- section 2: permlevel (rate_category is internal only) -----------------

	def test_client_role_cannot_see_rate_category_on_time_entry(self):
		ticket = self._new_ticket()
		entry = self._make_time_entry(
			ticket.name, self.engineer_a.name, "2026-06-15 06:00:00", "2026-06-15 07:00:00"
		)
		self.assertEqual(entry.rate_category, "Standard")

		frappe.set_user(self.portal_user.name)
		doc = frappe.get_doc("AC Time Entry", entry.name)
		doc.check_permission("read")
		doc.apply_fieldlevel_read_permissions()
		frappe.set_user("Administrator")

		self.assertIsNone(doc.get("rate_category"))
		self.assertEqual(doc.get("engineer"), self.engineer_a.name)

	# -- section 5: unlogged hours nudge ----------------------------------------

	def test_unlogged_hours_nudge_emails_only_engineers_without_a_logged_entry(self):
		ticket = self._new_ticket(engineers_required=2)
		self._assign(ticket.name, self.engineer_a.name)
		self._assign(ticket.name, self.engineer_b.name)

		self._make_time_entry(
			ticket.name, self.engineer_b.name, "2026-06-15 06:00:00", "2026-06-15 07:00:00", assign=False
		).submit()

		self._reload(ticket.name).db_set("status", "Completed")
		self._reload(ticket.name).db_set("completed_on", add_to_date(now_datetime(), hours=-48))
		frappe.db.commit()

		with patch("authcor.tasks.frappe.sendmail") as mock_sendmail:
			send_unlogged_hours_nudges()

		recipients = [call.kwargs["recipients"][0] for call in mock_sendmail.call_args_list]
		self.assertIn(self.engineer_a.name, recipients)
		self.assertNotIn(self.engineer_b.name, recipients)
