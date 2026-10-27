import ast
import os
import re
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	ENGINEER_DISCLOSURE_TEMPLATE,
)
from authcor.notifications import notify
from authcor.notifications.context import build_ticket_closed_context
from authcor.notifications.escalation import evaluate_escalation_rules
from authcor.notifications.recipients import (
	get_customer_contacts,
	get_leads_and_heads,
	get_pool_engineers,
	resolve_count_request_decided,
	resolve_count_request_raised,
	resolve_engineer_removed,
	resolve_severity_escalated,
	resolve_shipment_closed,
	resolve_shipment_created,
	resolve_shipment_received,
	resolve_ticket_claimed,
	resolve_ticket_created,
)
from authcor.tasks import send_unlogged_hours_nudges
from authcor.tests.notification_fixtures import synchronous_delivery
from authcor.tests.sla_fixtures import ensure_sla_policy, ensure_sla_policy_committed

SGT = "Asia/Singapore"


def _get_or_create(doctype, name, values):
	if frappe.db.exists(doctype, name):
		doc = frappe.get_doc(doctype, name)
		doc.update(values)
		doc.save(ignore_permissions=True)
		return doc
	return frappe.get_doc(doctype=doctype, **values).insert(ignore_permissions=True)


MON_FRI_HOURS = [
	{"day": day, "start_time": "09:00:00", "end_time": "18:00:00"}
	for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
]


def _make_city(name):
	return _get_or_create("AC City", name, {"city_name": name, "country": "Singapore", "timezone": SGT})


def _make_holiday_calendar(name, year=2026, holiday_dates=()):
	return _get_or_create(
		"AC Holiday Calendar",
		name,
		{"calendar_name": name, "year": year, "holidays": [{"holiday_date": d} for d in holiday_dates]},
	)


def _make_business_city(name):
	"""A city with business hours + an empty holiday calendar -- needed
	wherever a test creates an AC Time Entry (derive_rate_category
	requires both)."""
	calendar = _make_holiday_calendar(f"{name} Holiday Calendar")
	return _get_or_create(
		"AC City",
		name,
		{
			"city_name": name,
			"country": "Singapore",
			"timezone": SGT,
			"business_hours": MON_FRI_HOURS,
			"holiday_calendar": calendar.name,
		},
	)


def _make_data_center(name, city):
	return _get_or_create("AC Data Center", name, {"data_center_name": name, "city": city})


def _get_or_create_user(email):
	if frappe.db.exists("User", email):
		return frappe.get_doc("User", email)
	return frappe.get_doc(
		doctype="User", email=email, first_name=email.split("@", 1)[0], send_welcome_email=0
	).insert(ignore_permissions=True)


def _make_user(email, roles=None):
	user = _get_or_create_user(email)
	for role in roles or []:
		user.add_roles(role)
	return user


def _make_engineer(email):
	user = _get_or_create_user(email)
	user.add_roles("Operations L3 Authorizer")
	profile = _get_or_create(
		"AC Engineer Profile",
		user.name,
		{
			"user": user.name,
			"phone": "+65 8000 0000",
			"id_type": "NRIC",
			"id_number": "S1234567A",
			"is_active": 1,
		},
	)
	return user, profile


def _make_customer(name, authorised_engineers=None, portal_users=None, leads_and_heads=None):
	return _get_or_create(
		"AC Customer",
		name,
		{
			"customer_name": name,
			"service_level": "Standard",
			"authorised_engineers": [{"engineer": u} for u in (authorised_engineers or [])],
			"portal_users": [
				{"user": u, "authorizer_level": "L1 Authorizer", "is_active": 1} for u in (portal_users or [])
			],
			"leads_and_heads": [{"user": u, "designation": "Lead"} for u in (leads_and_heads or [])],
		},
	)


def _make_ticket(customer, city, data_center, **overrides):
	values = {
		"doctype": "AC Smart Hands Request",
		"customer": customer,
		"country": "Singapore",
		"city": city,
		"data_center": data_center,
		"subject": "Notification test ticket",
		"description": "Test",
	}
	values.update(overrides)
	return frappe.get_doc(values).insert(ignore_permissions=True)


def _cleanup_master_data(user_emails, customer_names, dc_names, city_names, calendar_names=()):
	"""Explicit teardown for classes whose tests force a frappe.db.commit()
	(notify() -> _write_log ends with one, same as claim_ticket/
	add_engineer elsewhere in this app). That forced commit sweeps up
	whatever setUpClass created via ensure_sla_policy/get-or-create-style
	helpers -- both deliberately non-committing on their own -- so without
	a matching cleanup, this class's master data is left permanently on a
	reused dev site. addClassCleanup runs this after all of a class's
	tests, in LIFO order relative to other registered cleanups."""
	for name in customer_names:
		if frappe.db.exists("AC Customer", name):
			frappe.delete_doc("AC Customer", name, force=True, ignore_permissions=True)
	for email in user_emails:
		profile_name = frappe.db.get_value("AC Engineer Profile", {"user": email}, "name")
		if profile_name:
			frappe.delete_doc("AC Engineer Profile", profile_name, force=True, ignore_permissions=True)
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	for name in dc_names:
		if frappe.db.exists("AC Data Center", name):
			frappe.delete_doc("AC Data Center", name, force=True, ignore_permissions=True)
	for name in city_names:
		if frappe.db.exists("AC City", name):
			frappe.delete_doc("AC City", name, force=True, ignore_permissions=True)
	for name in calendar_names:
		if frappe.db.exists("AC Holiday Calendar", name):
			frappe.delete_doc("AC Holiday Calendar", name, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestNotifyDispatch(IntegrationTestCase):
	"""PHASE-5-NOTIFICATIONS.md section 9: core notify() mechanics --
	actor exclusion, dedupe, and failure handling. Each test fires against
	a real ticket (reference_name is a Dynamic Link -- AC Notification Log
	inserts do go through _validate_links even with ignore_permissions,
	and a fake reference string would only ever have worked by accident of
	ignore_links masking it) so these exercise the real path, not a
	stand-in that happens to avoid the validation entirely."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Every test in this class forces a commit (see tearDown below), so
		# the master data below must be committed with a matching cleanup
		# too -- ensure_sla_policy alone is documented as relying on a
		# rollback that never actually happens once anything here commits.
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
		cls.city = _make_city("Test Notif Dispatch City")
		cls.dc = _make_data_center("Test Notif Dispatch DC", cls.city.name)
		cls.customer = _make_customer("Test Notif Dispatch Customer")
		cls.user_a = _make_user("notif_user_a@authcor.test")
		cls.user_b = _make_user("notif_user_b@authcor.test")
		frappe.db.commit()
		cls.addClassCleanup(
			_cleanup_master_data,
			[cls.user_a.name, cls.user_b.name],
			[cls.customer.name],
			[cls.dc.name],
			[cls.city.name],
		)

	def tearDown(self):
		"""notify() -> _write_log ends with an explicit frappe.db.commit()
		(a background job gets no automatic end-of-request commit), which
		breaks this class's normal per-test rollback for the tickets and
		AC Notification Log rows each test creates -- deleted and
		committed explicitly here rather than left to accumulate on a
		reused dev site. This customer has no authorised_engineers/leads_
		and_heads, so each ticket's own automatic ticket_created dispatch
		(after_insert) resolves to zero recipients and never writes a log
		row of its own to confuse these counts."""
		frappe.set_user("Administrator")
		for ticket_name in frappe.get_all(
			"AC Smart Hands Request", filters={"customer": self.customer.name}, pluck="name"
		):
			for log_name in frappe.get_all(
				"AC Notification Log", filters={"reference_name": ticket_name}, pluck="name"
			):
				frappe.delete_doc("AC Notification Log", log_name, force=True, ignore_permissions=True)
			frappe.delete_doc("AC Smart Hands Request", ticket_name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def _new_ticket(self):
		return _make_ticket(self.customer.name, self.city.name, self.dc.name)

	def _context(self, ticket):
		return {
			"ticket_name": ticket.name,
			"subject": ticket.subject,
			"severity": ticket.severity,
			"city": ticket.city,
		}

	def _logs(self, event_key, reference_name, recipient=None):
		filters = {"event_key": event_key, "reference_name": reference_name}
		if recipient:
			filters["recipient"] = recipient
		return frappe.get_all(
			"AC Notification Log", filters=filters, fields=["recipient", "channel", "status"]
		)

	def test_actor_is_excluded_from_recipients(self):
		ticket = self._new_ticket()
		frappe.set_user(self.user_a.name)
		with synchronous_delivery():
			notify(
				event_key="ticket_created",
				reference_doctype="AC Smart Hands Request",
				reference_name=ticket.name,
				recipients=[self.user_a.name, self.user_b.name],
				context=self._context(ticket),
				subject="Test",
			)

		self.assertEqual(len(self._logs("ticket_created", ticket.name, self.user_a.name)), 0)
		self.assertEqual(len(self._logs("ticket_created", ticket.name, self.user_b.name)), 1)

	def test_dedupe_skips_second_send_and_logs_skipped(self):
		ticket = self._new_ticket()
		with patch("authcor.notifications.channels.frappe.sendmail") as mock_sendmail:
			with synchronous_delivery():
				for _ in range(2):
					notify(
						event_key="ticket_created",
						reference_doctype="AC Smart Hands Request",
						reference_name=ticket.name,
						recipients=[self.user_b.name],
						context=self._context(ticket),
						subject="Test",
					)

		self.assertEqual(mock_sendmail.call_count, 1)
		logs = self._logs("ticket_created", ticket.name, self.user_b.name)
		statuses = sorted(row.status for row in logs)
		self.assertEqual(statuses, ["Sent", "Skipped"])

	def test_failed_send_logs_failed_and_does_not_raise(self):
		ticket = self._new_ticket()
		with patch("authcor.notifications.channels.frappe.sendmail", side_effect=RuntimeError("smtp down")):
			with synchronous_delivery():
				notify(
					event_key="ticket_created",
					reference_doctype="AC Smart Hands Request",
					reference_name=ticket.name,
					recipients=[self.user_b.name],
					context=self._context(ticket),
					subject="Test",
				)

		logs = self._logs("ticket_created", ticket.name, self.user_b.name)
		self.assertEqual(len(logs), 1)
		log = frappe.get_doc(
			"AC Notification Log",
			{"event_key": "ticket_created", "reference_name": ticket.name, "recipient": self.user_b.name},
		)
		self.assertEqual(log.status, "Failed")
		self.assertIn("smtp down", log.error)

	def test_notifications_enabled_false_suppresses_everything(self):
		"""The field is `notification_enabled` (singular) -- that's what it
		was actually named in Desk, not the `notifications_enabled` the
		spec doc uses."""
		ticket = self._new_ticket()
		frappe.db.set_single_value("AC Notification Settings", "notification_enabled", 0)
		try:
			with patch("authcor.notifications.channels.frappe.sendmail") as mock_sendmail:
				with synchronous_delivery():
					notify(
						event_key="ticket_created",
						reference_doctype="AC Smart Hands Request",
						reference_name=ticket.name,
						recipients=[self.user_b.name],
						context=self._context(ticket),
						subject="Test",
					)
			mock_sendmail.assert_not_called()
			self.assertEqual(len(self._logs("ticket_created", ticket.name)), 0)
		finally:
			frappe.db.set_single_value("AC Notification Settings", "notification_enabled", 1)


class TestClientRoleSlaExclusion(IntegrationTestCase):
	"""The hard rule: no sla_* event may reach a Client-role user, whatever
	an escalation rule's notify_roles says -- enforced in notify() itself,
	not trusted to recipient lists. Fires against a real ticket for the
	same reason as TestNotifyDispatch -- reference_name is a Dynamic Link
	that AC Notification Log's insert validates."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Same reasoning as TestNotifyDispatch.setUpClass -- every test
		# here forces a commit too, so the master data needs a matching
		# commit + addClassCleanup rather than ensure_sla_policy's
		# rely-on-rollback default.
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
		cls.city = _make_city("Test Notif Sla Exclusion City")
		cls.dc = _make_data_center("Test Notif Sla Exclusion DC", cls.city.name)
		cls.customer = _make_customer("Test Notif Sla Exclusion Customer")
		cls.client_user = _make_user("notif_client@authcor.test", roles=["Client L1 Authorizer"])
		cls.ops_user = _make_user("notif_ops@authcor.test", roles=["Operations L1 Authorizer"])
		frappe.db.commit()
		cls.addClassCleanup(
			_cleanup_master_data,
			[cls.client_user.name, cls.ops_user.name],
			[cls.customer.name],
			[cls.dc.name],
			[cls.city.name],
		)

	def tearDown(self):
		"""Same reasoning as TestNotifyDispatch.tearDown -- notify()'s
		forced commit means the tickets/log rows this class creates need
		explicit cleanup, not automatic rollback."""
		frappe.set_user("Administrator")
		for ticket_name in frappe.get_all(
			"AC Smart Hands Request", filters={"customer": self.customer.name}, pluck="name"
		):
			for log_name in frappe.get_all(
				"AC Notification Log", filters={"reference_name": ticket_name}, pluck="name"
			):
				frappe.delete_doc("AC Notification Log", log_name, force=True, ignore_permissions=True)
			frappe.delete_doc("AC Smart Hands Request", ticket_name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def _new_ticket(self):
		return _make_ticket(self.customer.name, self.city.name, self.dc.name)

	def test_client_role_never_receives_sla_event(self):
		ticket = self._new_ticket()
		with synchronous_delivery():
			notify(
				event_key="sla_breached:some-rule",
				template="sla_breached",
				reference_doctype="AC Smart Hands Request",
				reference_name=ticket.name,
				recipients=[self.client_user.name, self.ops_user.name],
				context={
					"ticket_name": ticket.name,
					"subject": ticket.subject,
					"trigger": "Unclaimed",
					"rule_name": "r",
					"minutes_over": 20,
				},
				subject="Test",
			)

		client_logs = frappe.get_all(
			"AC Notification Log",
			filters={"reference_name": ticket.name, "recipient": self.client_user.name},
		)
		ops_logs = frappe.get_all(
			"AC Notification Log", filters={"reference_name": ticket.name, "recipient": self.ops_user.name}
		)
		self.assertEqual(len(client_logs), 0)
		self.assertEqual(len(ops_logs), 1)

	def test_client_role_can_receive_a_non_sla_event(self):
		"""Proves the filter is scoped to sla_* specifically, not a blanket
		exclusion of Client roles from every notification."""
		ticket = self._new_ticket()
		with synchronous_delivery():
			notify(
				event_key="ticket_claimed",
				reference_doctype="AC Smart Hands Request",
				reference_name=ticket.name,
				recipients=[self.client_user.name],
				context={"ticket_name": ticket.name, "subject": ticket.subject},
				subject="Test",
			)

		logs = frappe.get_all(
			"AC Notification Log",
			filters={"reference_name": ticket.name, "recipient": self.client_user.name},
		)
		self.assertEqual(len(logs), 1)


class TestRecipientResolvers(IntegrationTestCase):
	"""PHASE-5-NOTIFICATIONS.md section 9: "Each event resolves the
	documented recipient set." """

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.engineer_a, cls.profile_a = _make_engineer("notif_recip_eng_a@authcor.test")
		cls.engineer_b, cls.profile_b = _make_engineer("notif_recip_eng_b@authcor.test")
		cls.portal_user = _make_user("notif_recip_portal@authcor.test", roles=["Client L1 Authorizer"])
		cls.lead_user = _make_user("notif_recip_lead@authcor.test", roles=["Client L1 Authorizer"])

		cls.customer = _make_customer(
			"Test Notif Recipients Customer",
			authorised_engineers=[cls.engineer_a.name, cls.engineer_b.name],
			portal_users=[cls.portal_user.name],
			leads_and_heads=[cls.lead_user.name],
		)

	def test_get_customer_contacts(self):
		self.assertEqual(get_customer_contacts(self.customer.name), [self.portal_user.name])

	def test_get_leads_and_heads(self):
		self.assertEqual(get_leads_and_heads(self.customer.name), [self.lead_user.name])

	def test_get_pool_engineers(self):
		engineers = get_pool_engineers(self.customer.name)
		self.assertCountEqual(engineers, [self.engineer_a.name, self.engineer_b.name])

	def test_get_pool_engineers_excludes_inactive_profile(self):
		self.profile_b.is_active = 0
		self.profile_b.save(ignore_permissions=True)
		try:
			engineers = get_pool_engineers(self.customer.name)
			self.assertEqual(engineers, [self.engineer_a.name])
		finally:
			self.profile_b.is_active = 1
			self.profile_b.save(ignore_permissions=True)

	def test_resolve_ticket_created(self):
		ticket = frappe._dict(customer=self.customer.name)
		self.assertCountEqual(
			resolve_ticket_created(ticket), [self.engineer_a.name, self.engineer_b.name, self.lead_user.name]
		)

	def test_resolve_ticket_claimed(self):
		ticket = frappe._dict(customer=self.customer.name)
		self.assertCountEqual(resolve_ticket_claimed(ticket), [self.portal_user.name, self.lead_user.name])

	def test_resolve_severity_escalated(self):
		ticket = frappe._dict(
			customer=self.customer.name,
			assigned_engineers=[frappe._dict(engineer=self.engineer_a.name)],
		)
		self.assertCountEqual(resolve_severity_escalated(ticket), [self.engineer_a.name, self.lead_user.name])

	def test_resolve_engineer_removed(self):
		self.assertEqual(resolve_engineer_removed(self.engineer_a.name), [self.engineer_a.name])

	def test_resolve_shipment_created(self):
		shipment = frappe._dict(customer=self.customer.name)
		self.assertCountEqual(
			resolve_shipment_created(shipment),
			[self.engineer_a.name, self.engineer_b.name, self.lead_user.name],
		)

	def test_resolve_shipment_received(self):
		shipment = frappe._dict(customer=self.customer.name)
		self.assertCountEqual(
			resolve_shipment_received(shipment), [self.portal_user.name, self.lead_user.name]
		)

	def test_resolve_shipment_closed(self):
		shipment = frappe._dict(customer=self.customer.name)
		self.assertEqual(resolve_shipment_closed(shipment), [self.portal_user.name])

	def test_resolve_count_request_raised_includes_leads_and_heads(self):
		request = frappe._dict(customer=self.customer.name)
		self.assertIn(self.lead_user.name, resolve_count_request_raised(request))

	def test_resolve_count_request_decided_includes_portal_users_and_affected_engineers(self):
		request = frappe._dict(customer=self.customer.name)
		recipients = resolve_count_request_decided(request, affected_engineers=[self.engineer_a.name])
		self.assertCountEqual(recipients, [self.portal_user.name, self.engineer_a.name])


class TestTicketClosedContext(IntegrationTestCase):
	"""PHASE-5-NOTIFICATIONS.md section 9: "ticket_closed renders hours and
	expenses but no rate or billable amount -- the permlevel boundary, in
	template form." This is the rate-leak test: build_ticket_closed_context
	is the one function this render path is allowed to build its Jinja
	context from, and it never reads a whole document -- explicit
	field-list frappe.db reads only -- so there's no sla_policy/
	response_due/first_response_at/etc. for a future edit to accidentally
	widen into the email."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_sla_policy(
			"Standard",
			"P4",
			response_minutes=480,
			onsite_minutes=0,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		cls.city = _make_business_city("Test Notif Closed City")
		cls.dc = _make_data_center("Test Notif Closed DC", cls.city.name)
		cls.customer = _make_customer("Test Notif Closed Customer")
		cls.engineer, cls.profile = _make_engineer("notif_closed_eng@authcor.test")

	def test_context_contains_only_permlevel_0_derived_keys(self):
		ticket = _make_ticket(self.customer.name, self.city.name, self.dc.name, engineers_required=1)
		context = build_ticket_closed_context(ticket.name)
		self.assertEqual(
			set(context.keys()),
			{"ticket_name", "subject", "engineer_hours", "total_hours", "expenses", "total_expenses"},
		)

	def test_context_reflects_submitted_time_and_expenses(self):
		ticket = _make_ticket(self.customer.name, self.city.name, self.dc.name, engineers_required=1)
		ticket.append(
			"assigned_engineers",
			{"engineer": self.engineer.name, "assignment_type": "Primary", "added_by": "Administrator"},
		)
		ticket.save(ignore_permissions=True)

		entry = frappe.get_doc(
			doctype="AC Time Entry",
			ticket=ticket.name,
			engineer=self.engineer.name,
			work_date="2026-06-15",
			start_time="2026-06-15 06:00:00",
			end_time="2026-06-15 08:00:00",
		).insert(ignore_permissions=True)
		entry.submit()

		frappe.get_doc(
			doctype="AC Expense Claim",
			ticket=ticket.name,
			engineer=self.engineer.name,
			expense_date="2026-06-15",
			category="Transport",
			description="Taxi",
			amount=25,
			currency="SGD",
			receipt="/files/r.pdf",
			status="Submitted",
		).insert(ignore_permissions=True)

		context = build_ticket_closed_context(ticket.name)
		full_name = frappe.db.get_value("User", self.engineer.name, "full_name")
		self.assertEqual(context["engineer_hours"], [{"engineer": full_name, "hours": 2.0}])
		self.assertEqual(len(context["expenses"]), 1)
		self.assertEqual(context["expenses"][0]["amount"], 25)


class TestEscalationRuleMatching(IntegrationTestCase):
	"""PHASE-5-NOTIFICATIONS.md section 9: severity/service level filters
	and Any wildcards; "Unclaimed" counts partially-staffed tickets as
	unmet; an escalation rule fires once, not every 15 minutes."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Same reasoning as TestNotifyDispatch.setUpClass -- every test
		# here forces a commit too (via evaluate_escalation_rules() ->
		# notify() -> _write_log), so the master data needs a matching
		# commit + addClassCleanup rather than ensure_sla_policy's
		# rely-on-rollback default.
		ensure_sla_policy_committed(
			cls.addClassCleanup,
			"Standard",
			"P1",
			response_minutes=15,
			onsite_minutes=0,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		cls.city = _make_city("Test Notif Escalation City")
		cls.dc = _make_data_center("Test Notif Escalation DC", cls.city.name)
		cls.customer = _make_customer("Test Notif Escalation Customer")
		cls.ops_l1_user = _make_user("notif_esc_ops_l1@authcor.test", roles=["Operations L1 Authorizer"])
		frappe.db.commit()
		cls.addClassCleanup(
			_cleanup_master_data,
			[cls.ops_l1_user.name],
			[cls.customer.name],
			[cls.dc.name],
			[cls.city.name],
		)

	def tearDown(self):
		"""notify() -> _write_log ends with an explicit frappe.db.commit()
		(the same reasoning as every other background-job-style action in
		this app: a worker process gets no automatic end-of-request commit),
		which breaks this class's normal per-test rollback for anything
		already pending -- including the escalation rules created here,
		which carry a unique rule_name and would DuplicateEntryError on a
		second run if left committed and uncleaned. So this explicitly
		deletes and commits, rather than trusting the automatic rollback.
		The tickets created per test are naming-series autonamed (no
		collision risk either way) but are cleaned up here too, along with
		their notification logs, for the same tidiness reasons."""
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			"AC Escalation Rule", filters={"rule_name": ["like", "Test Notif Rule %"]}, pluck="name"
		):
			frappe.delete_doc("AC Escalation Rule", name, force=True, ignore_permissions=True)
		for ticket_name in frappe.get_all(
			"AC Smart Hands Request", filters={"customer": self.customer.name}, pluck="name"
		):
			for log_name in frappe.get_all(
				"AC Notification Log", filters={"reference_name": ticket_name}, pluck="name"
			):
				frappe.delete_doc("AC Notification Log", log_name, force=True, ignore_permissions=True)
			frappe.delete_doc("AC Smart Hands Request", ticket_name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def _make_old_ticket(self, severity="P1", minutes_old=30, engineers_required=2):
		ticket = _make_ticket(
			self.customer.name,
			self.city.name,
			self.dc.name,
			severity=severity,
			engineers_required=engineers_required,
		)
		ticket.db_set("creation", add_to_date(now_datetime(), minutes=-minutes_old))
		return ticket

	def _make_rule(
		self, name, trigger="Unclaimed", severity="P1", service_level="Any", threshold=15, roles=None
	):
		rule_name = f"Test Notif Rule {name}"
		return _get_or_create(
			"AC Escalation Rule",
			rule_name,
			dict(
				rule_name=rule_name,
				trigger=trigger,
				severity=severity,
				service_level=service_level,
				threshold_minutes=threshold,
				notify_roles=[{"role": r} for r in (roles or ["Operations L1 Authorizer"])],
				notify_leads_heads=0,
				channels="Email",
				is_active=1,
			),
		)

	def test_unclaimed_rule_fires_for_partially_staffed_ticket(self):
		ticket = self._make_old_ticket(engineers_required=2)
		ticket.append(
			"assigned_engineers",
			{"engineer": self.ops_l1_user.name, "assignment_type": "Primary", "added_by": "Administrator"},
		)
		ticket.save(ignore_permissions=True)
		rule = self._make_rule("Partial")

		with synchronous_delivery():
			evaluate_escalation_rules()

		logs = frappe.get_all(
			"AC Notification Log",
			filters={"event_key": f"sla_breached:{rule.name}", "reference_name": ticket.name},
		)
		self.assertTrue(logs)

	def test_severity_mismatch_does_not_fire(self):
		ticket = self._make_old_ticket(severity="P4")
		rule = self._make_rule("SeverityMismatch", severity="P1")

		with synchronous_delivery():
			evaluate_escalation_rules()

		logs = frappe.get_all(
			"AC Notification Log",
			filters={"event_key": f"sla_breached:{rule.name}", "reference_name": ticket.name},
		)
		self.assertFalse(logs)

	def test_any_severity_wildcard_matches(self):
		ticket = self._make_old_ticket(severity="P4")
		rule = self._make_rule("AnySeverity", severity="Any")

		with synchronous_delivery():
			evaluate_escalation_rules()

		logs = frappe.get_all(
			"AC Notification Log",
			filters={"event_key": f"sla_breached:{rule.name}", "reference_name": ticket.name},
		)
		self.assertTrue(logs)

	def test_service_level_mismatch_does_not_fire(self):
		ticket = self._make_old_ticket()
		rule = self._make_rule("ServiceLevelMismatch", service_level="Premium")

		with synchronous_delivery():
			evaluate_escalation_rules()

		logs = frappe.get_all(
			"AC Notification Log",
			filters={"event_key": f"sla_breached:{rule.name}", "reference_name": ticket.name},
		)
		self.assertFalse(logs)

	def test_rule_fires_once_not_every_run(self):
		"""Not a raw sendmail call count -- evaluate_escalation_rules()
		scans every active rule against every matching ticket site-wide,
		so a bare mock call count would be hostage to whatever else is
		committed in the shared test database at the time this runs. The
		thing that actually defines "fires once" is the dedupe key: this
		rule's own Sent row for this ticket, per recipient, must never
		appear twice -- the second evaluate_escalation_rules() call has to
		find the first run's Sent row and skip, exactly like
		test_dedupe_skips_second_send_and_logs_skipped in
		TestNotifyDispatch, just reached through the escalation path
		instead of calling notify() directly."""
		ticket = self._make_old_ticket()
		rule = self._make_rule("FiresOnce")

		with synchronous_delivery():
			evaluate_escalation_rules()
			evaluate_escalation_rules()

		sent_logs = frappe.get_all(
			"AC Notification Log",
			filters={
				"event_key": f"sla_breached:{rule.name}",
				"reference_name": ticket.name,
				"status": "Sent",
			},
			pluck="recipient",
		)
		self.assertTrue(sent_logs, "expected at least one Sent notification")
		self.assertEqual(len(sent_logs), len(set(sent_logs)), "same recipient got more than one Sent row")


class TestUnloggedHoursNudge(IntegrationTestCase):
	"""PHASE-5-NOTIFICATIONS.md section 9: unlogged_hours finds the right
	engineers and skips those who logged."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Same reasoning as TestNotifyDispatch.setUpClass -- every test
		# here forces a commit too, so the master data needs a matching
		# commit + addClassCleanup rather than ensure_sla_policy's
		# rely-on-rollback default.
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
		cls.city = _make_business_city("Test Notif Unlogged City")
		cls.dc = _make_data_center("Test Notif Unlogged DC", cls.city.name)
		cls.customer = _make_customer("Test Notif Unlogged Customer")
		cls.engineer_a, cls.profile_a = _make_engineer("notif_unlogged_a@authcor.test")
		cls.engineer_b, cls.profile_b = _make_engineer("notif_unlogged_b@authcor.test")
		frappe.db.commit()
		cls.addClassCleanup(
			_cleanup_master_data,
			[cls.engineer_a.name, cls.engineer_b.name],
			[cls.customer.name],
			[cls.dc.name],
			[cls.city.name],
			calendar_names=[f"{cls.city.name} Holiday Calendar"],
		)

	def tearDown(self):
		"""notify() -> _write_log's forced commit (see TestNotifyDispatch)
		means the ticket/time entry/log rows this test creates need
		explicit cleanup, not automatic rollback."""
		frappe.set_user("Administrator")
		for ticket_name in frappe.get_all(
			"AC Smart Hands Request", filters={"customer": self.customer.name}, pluck="name"
		):
			for log_name in frappe.get_all(
				"AC Notification Log", filters={"reference_name": ticket_name}, pluck="name"
			):
				frappe.delete_doc("AC Notification Log", log_name, force=True, ignore_permissions=True)
			for entry_name in frappe.get_all("AC Time Entry", filters={"ticket": ticket_name}, pluck="name"):
				entry = frappe.get_doc("AC Time Entry", entry_name)
				if entry.docstatus == 1:
					entry.cancel()
				frappe.delete_doc("AC Time Entry", entry_name, force=True, ignore_permissions=True)
			frappe.delete_doc("AC Smart Hands Request", ticket_name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def test_finds_engineer_without_entry_and_skips_one_who_logged(self):
		ticket = _make_ticket(self.customer.name, self.city.name, self.dc.name, engineers_required=2)
		ticket.append(
			"assigned_engineers",
			{"engineer": self.engineer_a.name, "assignment_type": "Primary", "added_by": "Administrator"},
		)
		ticket.append(
			"assigned_engineers",
			{"engineer": self.engineer_b.name, "assignment_type": "Support", "added_by": "Administrator"},
		)
		ticket.save(ignore_permissions=True)
		ticket.db_set("status", "Completed")
		ticket.db_set("completed_on", add_to_date(now_datetime(), hours=-48))

		# engineer_b logged and submitted their time; engineer_a did not.
		entry = frappe.get_doc(
			doctype="AC Time Entry",
			ticket=ticket.name,
			engineer=self.engineer_b.name,
			work_date="2026-06-15",
			start_time="2026-06-15 06:00:00",
			end_time="2026-06-15 07:00:00",
		).insert(ignore_permissions=True)
		entry.submit()

		with synchronous_delivery():
			send_unlogged_hours_nudges()

		logs_a = frappe.get_all(
			"AC Notification Log",
			filters={
				"event_key": "unlogged_hours",
				"reference_name": ticket.name,
				"recipient": self.engineer_a.name,
			},
		)
		logs_b = frappe.get_all(
			"AC Notification Log",
			filters={
				"event_key": "unlogged_hours",
				"reference_name": ticket.name,
				"recipient": self.engineer_b.name,
			},
		)
		self.assertTrue(logs_a)
		self.assertFalse(logs_b)


# -- inline email templates ------------------------------------------------
#
# test_portal.py's hygiene check scans the files under www/ and templates/,
# which is why it never saw the one template this app builds in Python.
# These two tests close that gap from both ends: one renders it with markup
# in the values an engineer controls, the other scans the app's Python for
# any other template literal handed to render_template.

PLACEHOLDER = re.compile(r"{{(.+?)}}")
ESCAPED = re.compile(r"\|\s*(e|escape)\s*$")

# Placeholders deliberately rendered without escaping, each with its
# reason. Empty today, and an entry is a promise that something else makes
# the value safe.
RAW_PLACEHOLDERS: dict[str, str] = {}


def _unescaped_placeholders(template):
	"""The placeholder expressions in `template` that do not end in | e."""
	return [
		expression.strip()
		for expression in PLACEHOLDER.findall(template)
		if not ESCAPED.search(expression.strip()) and expression.strip() not in RAW_PLACEHOLDERS
	]


def _template_literals_passed_to_render_template(app_path):
	"""[(path, lineno, template)] for every render_template() call in the
	app whose template argument this can read -- a string literal, or a
	module-level constant assigned one in the same file."""
	found = []
	for root, dirs, files in os.walk(app_path):
		dirs[:] = [d for d in dirs if d not in {"__pycache__", "node_modules"}]
		for filename in sorted(files):
			if not filename.endswith(".py"):
				continue
			path = os.path.join(root, filename)
			with open(path, encoding="utf-8") as f:
				tree = ast.parse(f.read(), path)

			constants = {
				target.id: node.value.value
				for node in tree.body
				if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
				for target in node.targets
				if isinstance(target, ast.Name) and isinstance(node.value.value, str)
			}

			for node in ast.walk(tree):
				if not isinstance(node, ast.Call) or not node.args:
					continue
				name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
				if name not in {"render_template", "get_template"}:
					continue
				first = node.args[0]
				if isinstance(first, ast.Constant) and isinstance(first.value, str):
					found.append((path, node.lineno, first.value))
				elif isinstance(first, ast.Name) and first.id in constants:
					found.append((path, node.lineno, constants[first.id]))
	return found


class TestEngineerDisclosureEmail(IntegrationTestCase):
	"""PDPA section of PLAN.md section 9: this mail carries a decrypted
	NRIC/FIN to the customer. The values around it are the engineer's own
	-- their User full_name and their profile phone -- and the customer
	reads the result as HTML."""

	MARKUP = '<img src=x onerror="alert(1)">'

	def _render(self, **overrides):
		context = {
			"ticket_name": "TKT-0001",
			"subject": "Rack audit",
			"engineer_name": "Priya Nair",
			"id_label": "FIN",
			"id_number": "G1234567X",
			"phone": "+6591234567",
		}
		context.update(overrides)
		return frappe.render_template(ENGINEER_DISCLOSURE_TEMPLATE, context)

	def test_markup_in_the_engineer_name_comes_out_escaped(self):
		html = self._render(engineer_name=self.MARKUP)

		self.assertNotIn("<img src=x", html)
		self.assertNotIn('onerror="alert(1)"', html)
		# Escaped, not dropped -- the customer still has to be able to read
		# whatever the name really is.
		self.assertIn("&lt;img src=x", html)

	def test_markup_in_every_engineer_controlled_value_comes_out_escaped(self):
		for field in ("engineer_name", "phone", "id_number", "subject"):
			with self.subTest(field=field):
				html = self._render(**{field: self.MARKUP})
				self.assertNotIn("<img src=x", html)
				self.assertIn("&lt;img src=x", html)

	def test_the_template_itself_escapes_every_placeholder(self):
		"""Renders prove the values that are tested; this covers the rest,
		so a sixth placeholder added later cannot arrive unescaped."""
		unescaped = _unescaped_placeholders(ENGINEER_DISCLOSURE_TEMPLATE)
		self.assertFalse(unescaped, msg=f"placeholders rendered raw: {unescaped}")

	def test_no_template_built_in_python_renders_a_value_raw(self):
		app_path = frappe.get_app_path("authcor")
		templates = _template_literals_passed_to_render_template(app_path)
		self.assertTrue(templates, msg=f"no render_template() calls found under {app_path}")

		offenders = [
			f"{os.path.relpath(path, app_path)}:{lineno} renders {expression!r} raw"
			for path, lineno, template in templates
			for expression in _unescaped_placeholders(template)
		]
		self.assertFalse(
			offenders,
			msg=(
				"Frappe's Jinja environment has autoescaping off, so a bare {{ }} emits "
				"the value verbatim. Add | e, or -- if something else already makes it "
				"safe -- add the placeholder to RAW_PLACEHOLDERS with the reason.\n  "
				+ "\n  ".join(offenders)
			),
		)
