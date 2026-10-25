"""PHASE-7-PORTAL.md section 7.

The cross-customer pair is the point of this module: the portal is the
first surface in this system reachable from the public internet, and the
same two-mechanism problem as everywhere else (list filtering vs direct
record access) now has a URL bar attached to it.

Every page is driven through the real website stack -- set_request +
frappe.website.serve.get_response -- rather than by calling get_context
directly. That way the route rules, the session guard, the template render
and the HTTP status code are all under test, which is what a browser
actually hits.
"""

import re
from pathlib import Path

import frappe
from frappe.app import make_form_dict
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, set_request
from frappe.website.serve import get_response, get_response_content, get_response_without_exception_handling

from authcor.authcor.doctype.ac_engineer_count_request.ac_engineer_count_request import (
	create_count_request,
)
from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	increase_engineer_count,
)
from authcor.portal import get_portal_customer
from authcor.portal.tickets import (
	amend_preferred_slots,
	create_portal_ticket,
	default_slot_grid,
	list_portal_tickets,
	set_dc_access_ticket_number,
)
from authcor.tests.portal_fixtures import ensure_portal_membership, ensure_portal_role_permissions
from authcor.tests.sla_fixtures import ensure_sla_policy, ensure_sla_policy_committed

SGT = "Asia/Singapore"
APP_ROOT = Path(frappe.get_app_path("authcor"))


# -- fixtures --------------------------------------------------------------


def _get_or_create(doctype, name, values):
	"""Get-or-create by primary name, tolerating a leftover row from an
	earlier interrupted run on a reused dev site -- same shape as the other
	test modules in this suite."""
	if frappe.db.exists(doctype, name):
		doc = frappe.get_doc(doctype, name)
		doc.update(values)
		doc.save(ignore_permissions=True)
		return doc
	return frappe.get_doc(doctype=doctype, **values).insert(ignore_permissions=True)


def _make_city(name):
	return _get_or_create("AC City", name, {"city_name": name, "country": "Singapore", "timezone": SGT})


def _make_data_center(name, city):
	return _get_or_create("AC Data Center", name, {"data_center_name": name, "city": city})


def _make_customer(name, service_areas, authorised_engineers=None):
	return _get_or_create(
		"AC Customer",
		name,
		{
			"customer_name": name,
			"service_level": "Standard",
			"is_active": 1,
			"service_areas": service_areas,
			"authorised_engineers": [{"engineer": u} for u in (authorised_engineers or [])],
		},
	)


def _make_user(email, roles=()):
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.get_doc(
			doctype="User",
			email=email,
			first_name=email.split("@", 1)[0],
			send_welcome_email=0,
		).insert(ignore_permissions=True)
	for role in roles:
		user.add_roles(role)
	return user


def _make_engineer(email):
	user = _make_user(email, roles=["Operations L3 Authorizer"])
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


def _make_ticket(customer, city, data_center, **overrides):
	values = {
		"doctype": "AC Smart Hands Request",
		"customer": customer,
		"country": "Singapore",
		"city": city,
		"data_center": data_center,
		"subject": "Portal test ticket",
		"description": "Portal test description",
		"action_required": "Smart Hands",
	}
	values.update(overrides)
	return frappe.get_doc(values).insert(ignore_permissions=True)


class PortalPageMixin:
	"""Drives portal routes the way a browser does."""

	def tearDown(self):
		frappe.set_user("Administrator")
		if hasattr(frappe.local, "request"):
			delattr(frappe.local, "request")
		super().tearDown()

	@staticmethod
	def _request(path, query_string=None):
		set_request(method="GET", path=path, query_string=query_string)
		# set_request only builds frappe.local.request; the real stack also
		# runs make_form_dict, which is where ?status=/?page= land. Route
		# arguments (/portal/tickets/<name>) are added later by the path
		# resolver itself.
		make_form_dict(frappe.local.request)

	def render(self, path, query_string=None):
		self._request(path, query_string)
		return get_response_content()

	def response(self, path, query_string=None):
		self._request(path, query_string)
		return get_response()

	def render_raising(self, path, query_string=None):
		self._request(path, query_string)
		return get_response_without_exception_handling()


# -- section 2: the session guard -----------------------------------------


class TestPortalAccessGuard(PortalPageMixin, IntegrationTestCase):
	"""Section 2: every page checks the session, and every page resolves its
	customer through get_portal_customer() rather than from the request."""

	ROUTES = ("/portal/tickets", "/portal/tickets/new")

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_portal_role_permissions()
		ensure_sla_policy(
			"Standard",
			"P4",
			response_minutes=480,
			onsite_minutes=0,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		cls.city = _make_city("Test Portal Guard City")
		cls.dc = _make_data_center("Test Portal Guard DC", cls.city.name)
		cls.customer = _make_customer(
			"Test Portal Guard Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
		)
		cls.portal_user = _make_user("portal_guard@authcor.test", roles=["Client L2 Authorizer"])
		ensure_portal_membership(cls.portal_user.name, cls.customer.name)

		# A user with the client role but no portal_users row anywhere.
		cls.outsider = _make_user("portal_outsider@authcor.test", roles=["Client L2 Authorizer"])

		# A portal_users row that has been deactivated.
		cls.retired_user = _make_user("portal_retired@authcor.test", roles=["Client L2 Authorizer"])
		ensure_portal_membership(cls.retired_user.name, cls.customer.name)
		customer_doc = frappe.get_doc("AC Customer", cls.customer.name)
		for row in customer_doc.portal_users:
			if row.user == cls.retired_user.name:
				row.is_active = 0
		customer_doc.save(ignore_permissions=True)

		cls.ticket = _make_ticket(cls.customer.name, cls.city.name, cls.dc.name)

	def test_guest_request_to_every_portal_route_redirects_to_login(self):
		frappe.set_user("Guest")
		for route in (*self.ROUTES, f"/portal/tickets/{self.ticket.name}"):
			with self.subTest(route=route):
				response = self.response(route)
				self.assertEqual(response.status_code, 301)
				self.assertIn("/login", response.headers["Location"])

	def test_guest_redirect_carries_the_requested_route_back(self):
		frappe.set_user("Guest")
		response = self.response("/portal/tickets")
		self.assertIn("redirect-to=", response.headers["Location"])
		self.assertIn("portal", response.headers["Location"])

	def test_user_without_portal_membership_is_denied(self):
		frappe.set_user(self.outsider.name)
		with self.assertRaises(frappe.PermissionError):
			get_portal_customer()

	def test_user_without_portal_membership_gets_403_on_every_route(self):
		frappe.set_user(self.outsider.name)
		for route in self.ROUTES:
			with self.subTest(route=route):
				self.assertEqual(self.response(route).status_code, 403)

	def test_deactivated_portal_user_is_denied(self):
		frappe.set_user(self.retired_user.name)
		with self.assertRaises(frappe.PermissionError):
			get_portal_customer()

	def test_portal_customer_comes_from_the_session(self):
		frappe.set_user(self.portal_user.name)
		self.assertEqual(get_portal_customer().name, self.customer.name)

	def test_pages_ignore_a_customer_supplied_on_the_query_string(self):
		"""Section 2: "Never take a customer identifier from the request."
		A ?customer= must change nothing about what the page shows."""
		frappe.set_user(self.portal_user.name)
		response = self.response("/portal/tickets", query_string="customer=Some+Other+Customer")
		html = frappe.safe_decode(response.get_data())
		self.assertEqual(response.status_code, 200)
		self.assertIn(self.customer.customer_name, html)
		self.assertNotIn("Some Other Customer", html)
		self.assertIn(self.ticket.name, html)


# -- section 4: the list ---------------------------------------------------


class TestPortalTicketList(PortalPageMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_portal_role_permissions()
		ensure_sla_policy(
			"Standard",
			"P4",
			response_minutes=480,
			onsite_minutes=0,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		cls.city = _make_city("Test Portal List City")
		cls.dc = _make_data_center("Test Portal List DC", cls.city.name)

		cls.customer_a = _make_customer(
			"Test Portal List Customer A",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
		)
		cls.customer_b = _make_customer(
			"Test Portal List Customer B",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
		)
		cls.user_a = _make_user("portal_list_a@authcor.test", roles=["Client L2 Authorizer"])
		cls.user_b = _make_user("portal_list_b@authcor.test", roles=["Client L2 Authorizer"])
		ensure_portal_membership(cls.user_a.name, cls.customer_a.name)
		ensure_portal_membership(cls.user_b.name, cls.customer_b.name)

		cls.ticket_a = _make_ticket(
			cls.customer_a.name, cls.city.name, cls.dc.name, subject="Customer A open ticket"
		)
		cls.ticket_a_closed = _make_ticket(
			cls.customer_a.name, cls.city.name, cls.dc.name, subject="Customer A second ticket"
		)
		cls.ticket_a_closed.db_set("status", "Cancelled")
		cls.ticket_b = _make_ticket(
			cls.customer_b.name, cls.city.name, cls.dc.name, subject="Customer B secret ticket"
		)

	def test_list_shows_only_the_sessions_own_customer_tickets(self):
		frappe.set_user(self.user_a.name)
		html = self.render("/portal/tickets")
		self.assertIn(self.ticket_a.name, html)
		self.assertNotIn(self.ticket_b.name, html)
		self.assertNotIn("Customer B secret ticket", html)

	def test_the_other_customer_sees_the_mirror_image(self):
		"""Rendered back to back: proves the page is scoped per session and
		not served from the website page cache, whose key is the path
		alone."""
		frappe.set_user(self.user_a.name)
		html_a = self.render("/portal/tickets")
		frappe.set_user(self.user_b.name)
		html_b = self.render("/portal/tickets")
		self.assertIn(self.ticket_b.name, html_b)
		self.assertNotIn(self.ticket_a.name, html_b)
		self.assertNotIn(self.ticket_b.name, html_a)

	def test_list_filters_by_status(self):
		frappe.set_user(self.user_a.name)
		html = self.render("/portal/tickets", query_string="status=Cancelled")
		self.assertIn(self.ticket_a_closed.name, html)
		self.assertNotIn(self.ticket_a.name, html)

	def test_unknown_status_filter_is_ignored_rather_than_queried(self):
		frappe.set_user(self.user_a.name)
		html = self.render("/portal/tickets", query_string="status=Nonsense")
		self.assertIn(self.ticket_a.name, html)

	def test_list_paginates(self):
		"""Page size is a parameter so this costs two tickets rather than
		twenty-one; the default is asserted separately."""
		frappe.set_user(self.user_a.name)
		customer = get_portal_customer()

		first = list_portal_tickets(customer, page=1, page_size=1)
		self.assertEqual(len(first["rows"]), 1)
		self.assertTrue(first["has_next"])
		self.assertFalse(first["has_prev"])

		second = list_portal_tickets(customer, page=2, page_size=1)
		self.assertEqual(len(second["rows"]), 1)
		self.assertTrue(second["has_prev"])
		self.assertNotEqual(first["rows"][0]["name"], second["rows"][0]["name"])

	def test_default_page_size_is_twenty(self):
		from authcor.portal.tickets import PAGE_SIZE

		self.assertEqual(PAGE_SIZE, 20)

	def test_list_scoping_survives_a_page_parameter(self):
		frappe.set_user(self.user_b.name)
		html = self.render("/portal/tickets", query_string="page=1")
		self.assertNotIn(self.ticket_a.name, html)


# -- section 4: the detail page -------------------------------------------


class TestPortalTicketDetail(PortalPageMixin, IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_portal_role_permissions()
		ensure_sla_policy(
			"Standard",
			"P4",
			response_minutes=480,
			onsite_minutes=0,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		cls.city = _make_city("Test Portal Detail City")
		cls.dc = _make_data_center("Test Portal Detail DC", cls.city.name)
		cls.engineer, cls.profile = _make_engineer("portal_detail_eng@authcor.test")

		cls.customer_a = _make_customer(
			"Test Portal Detail Customer A",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
			authorised_engineers=[cls.engineer.name],
		)
		cls.customer_b = _make_customer(
			"Test Portal Detail Customer B",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
		)
		cls.user_a = _make_user("portal_detail_a@authcor.test", roles=["Client L2 Authorizer"])
		cls.user_b = _make_user("portal_detail_b@authcor.test", roles=["Client L2 Authorizer"])
		ensure_portal_membership(cls.user_a.name, cls.customer_a.name)
		ensure_portal_membership(cls.user_b.name, cls.customer_b.name)

		cls.ticket = _make_ticket(cls.customer_a.name, cls.city.name, cls.dc.name)
		cls.ticket.append(
			"assigned_engineers",
			{"engineer": cls.engineer.name, "assignment_type": "Primary", "added_by": "Administrator"},
		)
		cls.ticket.save(ignore_permissions=True)

		# 90 minutes of submitted work, so the page has hours to render --
		# and a rate_category (permlevel 1) sitting on the same rows that
		# must not come with it.
		entry = frappe.get_doc(
			doctype="AC Time Entry",
			ticket=cls.ticket.name,
			engineer=cls.profile.name,
			work_date="2026-06-15",
			start_time="2026-06-15 02:00:00",
			end_time="2026-06-15 03:30:00",
		).insert(ignore_permissions=True)
		entry.submit()
		cls.time_entry = entry

		frappe.get_doc(
			doctype="AC Expense Claim",
			ticket=cls.ticket.name,
			engineer=cls.profile.name,
			expense_date="2026-06-15",
			category="Transport",
			description="Taxi to site",
			amount=42.5,
			currency="SGD",
			receipt="/files/portal-test-receipt.pdf",
			status="Submitted",
		).insert(ignore_permissions=True)

		cls.other_ticket = _make_ticket(
			cls.customer_b.name, cls.city.name, cls.dc.name, subject="Customer B detail ticket"
		)

	def test_detail_renders_the_sessions_own_ticket(self):
		frappe.set_user(self.user_a.name)
		html = self.render(f"/portal/tickets/{self.ticket.name}")
		self.assertIn(self.ticket.name, html)
		self.assertIn("Portal test ticket", html)

	def test_another_customers_ticket_cannot_be_loaded_by_url(self):
		"""The one that matters most: the same ticket the list view hides
		must also be unreachable by typing its name into the URL bar."""
		frappe.set_user(self.user_a.name)
		with self.assertRaises(frappe.PermissionError):
			self.render_raising(f"/portal/tickets/{self.other_ticket.name}")

	def test_another_customers_ticket_returns_403(self):
		frappe.set_user(self.user_a.name)
		response = self.response(f"/portal/tickets/{self.other_ticket.name}")
		self.assertEqual(response.status_code, 403)
		self.assertNotIn("Customer B detail ticket", frappe.safe_decode(response.get_data()))

	def test_a_ticket_name_that_does_not_exist_is_denied_the_same_way(self):
		"""Answering 404 for "no such ticket" and 403 for "not yours" would
		turn the detail route into an existence oracle over a sequential
		naming series. Both answer the same."""
		frappe.set_user(self.user_a.name)
		with self.assertRaises(frappe.PermissionError):
			self.render_raising("/portal/tickets/AC-SH-2026-99999")

	def test_detail_renders_hours_but_no_rate(self):
		frappe.set_user(self.user_a.name)
		html = self.render(f"/portal/tickets/{self.ticket.name}")
		self.assertIn("1.5", html)
		self.assertNotIn("rate_category", html)
		self.assertNotIn(self.time_entry.rate_category, html)

	def test_detail_renders_expense_amounts(self):
		frappe.set_user(self.user_a.name)
		html = self.render(f"/portal/tickets/{self.ticket.name}")
		self.assertIn("42.5", html)
		self.assertIn("Taxi to site", html)

	def test_detail_names_the_assigned_engineer(self):
		frappe.set_user(self.user_a.name)
		html = self.render(f"/portal/tickets/{self.ticket.name}")
		full_name = frappe.db.get_value("User", self.engineer.name, "full_name")
		self.assertIn(full_name, html)

	def test_detail_carries_no_sla_values(self):
		"""The eleven performance fields are permlevel 1 (CHANGE-SET-POST-
		DEMO.md section 2) and the portal shows no SLA information at all.
		Checked against the real stored values, not just field names."""
		frappe.set_user("Administrator")
		ticket = frappe.get_doc("AC Smart Hands Request", self.ticket.name)
		frappe.set_user(self.user_a.name)
		html = self.render(f"/portal/tickets/{self.ticket.name}")
		# has_met_response_sla is deliberately not checked by value -- its
		# "Pending" reads as an ordinary English word that a portal page may
		# legitimately print about something else. Its fieldname is covered by
		# TestPortalTemplateHygiene instead.
		for value in (ticket.sla_policy, ticket.response_due):
			self.assertTrue(value, msg="fixture should have SLA values to leak")
			self.assertNotIn(str(value), html)

	def test_user_content_is_escaped(self):
		"""Frappe's Jinja environment has autoescaping off, so the templates
		escape explicitly and this is what proves they do.

		The payloads go in with db_set rather than a normal save:
		Document._sanitize_content would strip them on the way in, and then
		this would be testing the save path rather than the template.

		A bare "<script>" is no use as a marker -- the page carries its own
		script tags -- so each assertion names the specific unescaped form
		the payload would have produced."""
		frappe.set_user("Administrator")
		ticket = _make_ticket(self.customer_a.name, self.city.name, self.dc.name)
		ticket.db_set("subject", '<svg onload="alert(1)">', update_modified=False)
		ticket.db_set("target_equipment", '<img src=x onerror="alert(2)">', update_modified=False)
		ticket.db_set("external_reference", '" onmouseover="alert(3)', update_modified=False)
		# description is the one value rendered as HTML; build_portal_ticket_
		# view runs it through sanitize_html, which has to remove this.
		ticket.db_set(
			"description", '<p>ok</p><script>alert(4)</script>', update_modified=False
		)

		frappe.set_user(self.user_a.name)
		html = self.render(f"/portal/tickets/{ticket.name}")

		# Each marker is the *dangerous* form -- an unescaped tag or an
		# attribute whose quotes survived. The escaped text around them is
		# expected to still be there, which is why "onload" on its own is
		# not what gets asserted on: inside &lt;svg onload=&#34;...&#34;&gt;
		# it is just characters.
		for marker in ("<svg", "<img src=x", '" onmouseover="', "<script>alert"):
			with self.subTest(marker=marker):
				self.assertNotIn(marker, html)

		# Escaped, not silently dropped -- the customer still sees what they
		# typed.
		self.assertIn("&lt;svg", html)


# -- section 4: raising a ticket ------------------------------------------


class TestCreatePortalTicket(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_portal_role_permissions()
		ensure_sla_policy(
			"Standard",
			"P4",
			response_minutes=480,
			onsite_minutes=0,
			onsite_next_business_day=0,
			business_hours_only=0,
			response_next_business_day=0,
		)
		cls.city = _make_city("Test Portal Create City")
		cls.other_city = _make_city("Test Portal Create Other City")
		cls.dc = _make_data_center("Test Portal Create DC", cls.city.name)
		cls.forbidden_dc = _make_data_center("Test Portal Create Forbidden DC", cls.other_city.name)

		cls.customer = _make_customer(
			"Test Portal Create Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
		)
		cls.other_customer = _make_customer(
			"Test Portal Create Other Customer",
			service_areas=[
				{"country": "Singapore", "city": cls.other_city.name, "data_center": cls.forbidden_dc.name}
			],
		)
		cls.user = _make_user("portal_create@authcor.test", roles=["Client L2 Authorizer"])
		ensure_portal_membership(cls.user.name, cls.customer.name)

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(self.user.name)

	def _payload(self, **overrides):
		payload = {
			"country": "Singapore",
			"city": self.city.name,
			"data_center": self.dc.name,
			"subject": "Raised from the portal",
			"severity": "P3",
			"action_required": "Smart Hands",
			"description": "Replace the failed PSU in rack 14.",
			"engineers_required": 1,
		}
		payload.update(overrides)
		return payload

	def _create(self, **overrides):
		"""Goes through frappe.call, which is the production path: it is
		frappe.call that filters the posted form_dict down to the method's
		declared parameters. Calling the function directly would raise
		TypeError on an extra field and prove nothing about what a browser
		can actually post."""
		return frappe.call(create_portal_ticket, **self._payload(**overrides))

	def test_customer_is_set_from_the_session(self):
		ticket = self._create()
		self.assertEqual(ticket.customer, self.customer.name)

	def test_a_customer_in_the_payload_is_ignored(self):
		ticket = self._create(customer=self.other_customer.name)
		self.assertEqual(ticket.customer, self.customer.name)

	def test_arbitrary_fields_in_the_payload_are_dropped(self):
		"""Section 6: "Whitelisted methods accept a known field list." The
		browser must not be able to post its way into a field the form
		never offered."""
		ticket = self._create(status="Closed", total_hours=99, dc_access_ticket_number="DC-FORGED")
		self.assertEqual(ticket.status, "Open")
		self.assertFalse(ticket.total_hours)
		self.assertFalse(ticket.dc_access_ticket_number)

	def test_data_center_outside_the_service_areas_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._create(city=self.other_city.name, data_center=self.forbidden_dc.name)

	def test_data_center_in_another_customers_area_is_rejected_even_with_a_valid_city(self):
		with self.assertRaises(frappe.ValidationError):
			self._create(data_center=self.forbidden_dc.name)

	def test_seven_preferred_slot_rows_are_generated(self):
		ticket = self._create()
		self.assertEqual(len(ticket.preferred_slots), 7)
		for row in ticket.preferred_slots:
			self.assertTrue(row.band_morning)
			self.assertTrue(row.band_afternoon)
			self.assertTrue(row.band_overnight)

	def test_the_posted_slot_grid_is_what_gets_stored(self):
		grid = default_slot_grid()
		grid[0]["band_morning"] = 0
		grid[0]["band_afternoon"] = 0
		ticket = self._create(preferred_slots=frappe.as_json(grid))
		self.assertEqual(len(ticket.preferred_slots), 7)
		first = ticket.preferred_slots[0]
		self.assertFalse(first.band_morning)
		self.assertFalse(first.band_afternoon)
		self.assertTrue(first.band_overnight)

	def test_the_default_slot_grid_is_the_next_seven_days(self):
		grid = default_slot_grid()
		self.assertEqual(len(grid), 7)
		self.assertEqual(getdate(grid[0]["slot_date"]), add_days(getdate(), 1))
		self.assertEqual(getdate(grid[-1]["slot_date"]), add_days(getdate(), 7))

	def test_a_slot_row_with_no_band_ticked_is_rejected(self):
		grid = default_slot_grid()
		for row in grid:
			row["band_morning"] = row["band_afternoon"] = row["band_overnight"] = 0
		with self.assertRaises(frappe.ValidationError):
			self._create(preferred_slots=frappe.as_json(grid))

	def test_a_slot_date_in_the_past_is_rejected(self):
		grid = default_slot_grid()
		grid[0]["slot_date"] = str(add_days(getdate(), -1))
		with self.assertRaises(frappe.ValidationError):
			self._create(preferred_slots=frappe.as_json(grid))

	def test_severity_defaults_to_p4(self):
		payload = self._payload()
		payload.pop("severity")
		ticket = frappe.call(create_portal_ticket, **payload)
		self.assertEqual(ticket.severity, "P4")

	def test_an_invalid_severity_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._create(severity="P0")

	def test_others_requires_the_other_details(self):
		with self.assertRaises(frappe.ValidationError):
			self._create(action_required="Others")

	def test_a_user_without_portal_membership_cannot_create(self):
		stranger = _make_user("portal_create_stranger@authcor.test", roles=["Client L2 Authorizer"])
		frappe.set_user(stranger.name)
		with self.assertRaises(frappe.PermissionError):
			self._create()


# -- section 4: the ticket actions ----------------------------------------


def _cleanup_portal_action_fixtures(user_emails, customer_names, dc_name, city_name):
	for ticket in frappe.get_all(
		"AC Smart Hands Request", filters={"customer": ["in", customer_names]}, pluck="name"
	):
		for request in frappe.get_all(
			"AC Engineer Count Request", filters={"ticket": ticket}, pluck="name"
		):
			frappe.delete_doc("AC Engineer Count Request", request, force=True, ignore_permissions=True)
		frappe.delete_doc("AC Smart Hands Request", ticket, force=True, ignore_permissions=True)
	for name in customer_names:
		if frappe.db.exists("AC Customer", name):
			frappe.delete_doc("AC Customer", name, force=True, ignore_permissions=True)
	for email in user_emails:
		profile = frappe.db.get_value("AC Engineer Profile", {"user": email}, "name")
		if profile:
			frappe.delete_doc("AC Engineer Profile", profile, force=True, ignore_permissions=True)
		for perm in frappe.get_all("User Permission", filters={"user": email}, pluck="name"):
			frappe.delete_doc("User Permission", perm, force=True, ignore_permissions=True)
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	if frappe.db.exists("AC Data Center", dc_name):
		frappe.delete_doc("AC Data Center", dc_name, force=True, ignore_permissions=True)
	if frappe.db.exists("AC City", city_name):
		frappe.delete_doc("AC City", city_name, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestPortalTicketActions(IntegrationTestCase):
	"""Every action here ends in an explicit frappe.db.commit() -- the slot
	amendment and DC access number take the ticket lock the same way
	claim_ticket and increase_engineer_count do -- so this class manages its
	own fixture commit and teardown instead of relying on the rollback at
	class cleanup."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_portal_role_permissions()
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
		cls.city = _make_city("Test Portal Action City")
		cls.dc = _make_data_center("Test Portal Action DC", cls.city.name)
		cls.engineer, cls.profile = _make_engineer("portal_action_eng@authcor.test")

		cls.customer = _make_customer(
			"Test Portal Action Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
			authorised_engineers=[cls.engineer.name],
		)
		cls.other_customer = _make_customer(
			"Test Portal Action Other Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
		)
		cls.user = _make_user("portal_action@authcor.test", roles=["Client L2 Authorizer"])
		cls.other_user = _make_user("portal_action_other@authcor.test", roles=["Client L2 Authorizer"])
		frappe.db.commit()

		ensure_portal_membership(cls.user.name, cls.customer.name)
		ensure_portal_membership(cls.other_user.name, cls.other_customer.name)
		frappe.db.commit()

		cls.addClassCleanup(
			_cleanup_portal_action_fixtures,
			[cls.engineer.name, cls.user.name, cls.other_user.name],
			[cls.customer.name, cls.other_customer.name],
			cls.dc.name,
			cls.city.name,
		)

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.set_user, "Administrator")

	def _new_ticket(self, customer=None, **overrides):
		ticket = _make_ticket(customer or self.customer.name, self.city.name, self.dc.name, **overrides)
		frappe.db.commit()
		return ticket

	@staticmethod
	def _grid(**first_row_overrides):
		grid = default_slot_grid()
		grid[0].update(first_row_overrides)
		return frappe.as_json(grid)

	# -- amend preferred slots ------------------------------------------

	def test_slots_can_be_amended_while_unconfirmed(self):
		ticket = self._new_ticket()
		frappe.set_user(self.user.name)
		amend_preferred_slots(ticket.name, self._grid(band_morning=0, band_afternoon=0))

		reloaded = frappe.get_doc("AC Smart Hands Request", ticket.name)
		self.assertEqual(len(reloaded.preferred_slots), 7)
		self.assertFalse(reloaded.preferred_slots[0].band_morning)
		self.assertTrue(reloaded.preferred_slots[0].band_overnight)

	def test_slots_cannot_be_amended_once_the_visit_is_confirmed(self):
		ticket = self._new_ticket()
		confirmed_date = add_days(getdate(), 1)
		ticket.confirmed_date = confirmed_date
		ticket.confirmed_time = "14:00"
		ticket.save(ignore_permissions=True)
		frappe.db.commit()

		frappe.set_user(self.user.name)
		with self.assertRaises(frappe.ValidationError):
			amend_preferred_slots(ticket.name, self._grid(band_morning=0))

	def test_slots_cannot_be_amended_on_another_customers_ticket(self):
		ticket = self._new_ticket(customer=self.other_customer.name)
		frappe.set_user(self.user.name)
		with self.assertRaises(frappe.PermissionError):
			amend_preferred_slots(ticket.name, self._grid(band_morning=0))

	def test_slots_cannot_be_amended_on_a_cancelled_ticket(self):
		ticket = self._new_ticket()
		ticket.db_set("status", "Cancelled")
		frappe.db.commit()
		frappe.set_user(self.user.name)
		with self.assertRaises(frappe.ValidationError):
			amend_preferred_slots(ticket.name, self._grid(band_morning=0))

	# -- DC access ticket number ----------------------------------------

	def test_dc_access_number_is_refused_before_the_profile_is_released(self):
		ticket = self._new_ticket()
		frappe.set_user(self.user.name)
		with self.assertRaises(frappe.ValidationError):
			set_dc_access_ticket_number(ticket.name, "DC-12345")

	def test_dc_access_number_is_stored_once_the_profile_is_released(self):
		ticket = self._new_ticket()
		ticket.db_set("engineer_profile_released", 1)
		frappe.db.commit()

		frappe.set_user(self.user.name)
		set_dc_access_ticket_number(ticket.name, "  DC-12345  ")
		self.assertEqual(
			frappe.db.get_value("AC Smart Hands Request", ticket.name, "dc_access_ticket_number"),
			"DC-12345",
		)

	def test_dc_access_number_cannot_be_set_on_another_customers_ticket(self):
		ticket = self._new_ticket(customer=self.other_customer.name)
		ticket.db_set("engineer_profile_released", 1)
		frappe.db.commit()

		frappe.set_user(self.user.name)
		with self.assertRaises(frappe.PermissionError):
			set_dc_access_ticket_number(ticket.name, "DC-12345")

	def test_an_empty_dc_access_number_is_rejected(self):
		ticket = self._new_ticket()
		ticket.db_set("engineer_profile_released", 1)
		frappe.db.commit()
		frappe.set_user(self.user.name)
		with self.assertRaises(frappe.ValidationError):
			set_dc_access_ticket_number(ticket.name, "   ")

	# -- engineer count change: the existing methods, from the portal ----

	def test_count_increase_goes_through_the_existing_whitelisted_method(self):
		ticket = self._new_ticket(engineers_required=1)
		frappe.set_user(self.user.name)
		increase_engineer_count(ticket.name, 3)
		self.assertEqual(
			frappe.db.get_value("AC Smart Hands Request", ticket.name, "engineers_required"), 3
		)

	def test_count_decrease_raises_a_request_through_the_existing_method(self):
		ticket = self._new_ticket(engineers_required=3)
		frappe.set_user(self.user.name)
		request = create_count_request(ticket.name, 2, reason="One rack only after all")
		self.assertEqual(request.status, "Pending")
		self.assertEqual(request.customer, self.customer.name)
		self.assertEqual(request.current_count, 3)

	def test_count_change_denied_on_another_customers_ticket(self):
		ticket = self._new_ticket(customer=self.other_customer.name, engineers_required=1)
		frappe.set_user(self.user.name)
		with self.assertRaises(frappe.PermissionError):
			increase_engineer_count(ticket.name, 2)


# -- section 6: the template hygiene check --------------------------------


class TestPortalTemplateHygiene(IntegrationTestCase):
	"""Section 7: "No portal template references an SLA field -- grep the
	templates as a test."

	Widened slightly on purpose. The denylist is derived from the schema
	(every permlevel > 0 field on every Authcor doctype) rather than typed
	out, so a field moved to permlevel 1 in a later phase is covered
	without anyone remembering to update this list. And it covers the
	portal's Python modules as well as its templates, because a leak starts
	in whatever builds the context, not in the markup that prints it.

	One thing to know before Phase 7 step 5. The engineer details page is
	the deliberate exception: AC Engineer Profile's `id_type`, `id_number`
	and `id_expiry` are permlevel 1, and disclosing the number to the
	customer on that one page is Authcor's explicit decision (PLAN.md
	section 9). When that page lands it will need an exemption here --
	named and justified in this test, not a widening of the rule."""

	SLA_TERMS = (
		"response_due",
		"onsite_due",
		"sla_policy",
		"first_response_at",
		"first_response_minutes",
		"confirmed_at",
		"confirmation_minutes",
		"idle_minutes",
		"actual_onsite_at",
		"has_met_response_sla",
		"has_met_onsite_sla",
		"old_response_due",
		"old_verdict",
		"rate_category",
	)

	@classmethod
	def portal_sources(cls):
		paths = [APP_ROOT / "templates" / "portal_base.html"]
		paths += sorted((APP_ROOT / "www" / "portal").rglob("*.html"))
		paths += sorted((APP_ROOT / "www" / "portal").rglob("*.py"))
		paths += sorted((APP_ROOT / "portal").rglob("*.py"))
		return [p for p in paths if p.is_file()]

	@classmethod
	def restricted_fieldnames(cls):
		names = set()
		for doctype in frappe.get_all("DocType", filters={"module": "Authcor"}, pluck="name"):
			for df in frappe.get_meta(doctype).fields:
				if (df.permlevel or 0) > 0 and df.fieldname:
					names.add(df.fieldname)
		return names

	def test_portal_sources_were_found(self):
		"""Guards the grep itself: a test that scans nothing passes."""
		sources = self.portal_sources()
		self.assertGreaterEqual(len(sources), 5)
		self.assertTrue(any(p.suffix == ".html" for p in sources))
		self.assertTrue(any(p.suffix == ".py" for p in sources))

	def test_no_portal_source_references_a_permlevel_1_field(self):
		restricted = self.restricted_fieldnames()
		self.assertTrue(restricted, msg="schema should have permlevel-restricted fields to check against")
		for path in self.portal_sources():
			text = path.read_text()
			for fieldname in sorted(restricted):
				with self.subTest(path=path.name, fieldname=fieldname):
					self.assertIsNone(
						re.search(rf"\b{re.escape(fieldname)}\b", text),
						msg=f"{path} references permlevel-restricted field {fieldname}",
					)

	def test_no_portal_source_mentions_an_sla_term(self):
		for path in self.portal_sources():
			text = path.read_text()
			for term in self.SLA_TERMS:
				with self.subTest(path=path.name, term=term):
					self.assertIsNone(
						re.search(rf"\b{re.escape(term)}\b", text),
						msg=f"{path} mentions SLA field {term}",
					)

	def test_no_portal_template_pipes_user_content_through_safe(self):
		"""Section 6: never `| safe` on anything a user typed. Frappe's
		Jinja environment has no autoescaping, so the templates escape
		explicitly -- which makes a stray `| safe` the thing to watch for."""
		for path in self.portal_sources():
			if path.suffix != ".html":
				continue
			with self.subTest(path=path.name):
				self.assertNotIn("| safe", path.read_text())
