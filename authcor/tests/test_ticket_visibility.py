import frappe
from frappe.tests import IntegrationTestCase

from authcor.tests.sla_fixtures import ensure_sla_policy

SGT = "Asia/Singapore"


def _make_city(city_name):
	return frappe.get_doc(
		doctype="AC City", city_name=city_name, country="Singapore", timezone=SGT
	).insert(ignore_permissions=True)


def _make_data_center(data_center_name, city):
	return frappe.get_doc(
		doctype="AC Data Center", data_center_name=data_center_name, city=city
	).insert(ignore_permissions=True)


def _make_customer(customer_name, authorised_engineers=None):
	return frappe.get_doc(
		doctype="AC Customer",
		customer_name=customer_name,
		service_level="Standard",
		authorised_engineers=[{"engineer": u} for u in (authorised_engineers or [])],
	).insert(ignore_permissions=True)


def _make_user(email, roles=None):
	user = frappe.get_doc(
		doctype="User",
		email=email,
		first_name=email.split("@", 1)[0],
		send_welcome_email=0,
	).insert(ignore_permissions=True)
	for role in roles or []:
		user.add_roles(role)
	return user


def _make_ticket(customer, city, data_center, **overrides):
	values = {
		"doctype": "AC Smart Hands Request",
		"customer": customer,
		"country": "Singapore",
		"city": city,
		"data_center": data_center,
		"subject": "Visibility test ticket",
		"description": "Test",
		"action_required": "Remote Hands",
	}
	values.update(overrides)
	return frappe.get_doc(values).insert(ignore_permissions=True)


class TestTicketVisibilityPredicate(IntegrationTestCase):
	"""Section 1: the same predicate must hold through both the
	permission_query_conditions path (get_list) and the has_permission
	path (direct record access) -- these are registered as two separate
	hooks.py entries pointing at two separate functions, so this is the
	test that would catch them drifting apart (e.g. one updated, the
	other forgotten)."""

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
		cls.city = _make_city("Test Visibility City")
		cls.dc = _make_data_center("Test Visibility DC", cls.city.name)

		cls.engineer_user = _make_user(
			"vis_engineer@authcor.test", roles=["Operations L2 Authorizer"]
		)
		cls.other_engineer_user = _make_user(
			"vis_other_engineer@authcor.test", roles=["Operations L2 Authorizer"]
		)
		cls.ops_l1_user = _make_user("vis_ops_l1@authcor.test", roles=["Operations L1 Authorizer"])

		cls.authorised_customer = _make_customer(
			"Test Visibility Authorised Customer", authorised_engineers=[cls.engineer_user.name]
		)
		cls.unauthorised_customer = _make_customer(
			"Test Visibility Unauthorised Customer", authorised_engineers=[cls.other_engineer_user.name]
		)

		cls.authorised_ticket = _make_ticket(cls.authorised_customer.name, cls.city.name, cls.dc.name)
		cls.unauthorised_ticket = _make_ticket(cls.unauthorised_customer.name, cls.city.name, cls.dc.name)

		# A ticket under the customer the engineer is NOT authorised for,
		# but that they are directly assigned to -- the "plus any ticket
		# they are already assigned to" clause.
		cls.assigned_but_unauthorised_ticket = _make_ticket(
			cls.unauthorised_customer.name, cls.city.name, cls.dc.name
		)
		cls.assigned_but_unauthorised_ticket.append(
			"assigned_engineers",
			{
				"engineer": cls.engineer_user.name,
				"assignment_type": "Primary",
				"added_by": "Administrator",
			},
		)
		cls.assigned_but_unauthorised_ticket.save(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_engineer_sees_authorised_customer_ticket_via_get_list(self):
		frappe.set_user(self.engineer_user.name)
		names = {
			row.name
			for row in frappe.get_list("AC Smart Hands Request", fields=["name"], limit_page_length=0)
		}
		self.assertIn(self.authorised_ticket.name, names)

	def test_engineer_does_not_see_unauthorised_customer_ticket_via_get_list(self):
		frappe.set_user(self.engineer_user.name)
		names = {
			row.name
			for row in frappe.get_list("AC Smart Hands Request", fields=["name"], limit_page_length=0)
		}
		self.assertNotIn(self.unauthorised_ticket.name, names)

	def test_engineer_sees_assigned_ticket_despite_no_customer_authorisation(self):
		frappe.set_user(self.engineer_user.name)
		names = {
			row.name
			for row in frappe.get_list("AC Smart Hands Request", fields=["name"], limit_page_length=0)
		}
		self.assertIn(self.assigned_but_unauthorised_ticket.name, names)

	def test_engineer_has_direct_read_permission_on_authorised_ticket(self):
		"""The has_permission path, proven independently of get_list --
		this is the mechanism a direct-URL/get_doc/API access goes
		through."""
		self.assertTrue(
			frappe.has_permission(
				"AC Smart Hands Request", doc=self.authorised_ticket.name, user=self.engineer_user.name
			)
		)

	def test_engineer_denied_direct_read_permission_on_unauthorised_ticket(self):
		"""Same predicate, has_permission path this time: a direct
		get_doc/URL visit to a ticket outside the engineer's scope must be
		denied exactly as the list view already hides it -- this is the
		test that would catch the list looking filtered while the direct
		URL still loads."""
		self.assertFalse(
			frappe.has_permission(
				"AC Smart Hands Request", doc=self.unauthorised_ticket.name, user=self.engineer_user.name
			)
		)

	def test_engineer_has_direct_read_permission_on_assigned_ticket(self):
		self.assertTrue(
			frappe.has_permission(
				"AC Smart Hands Request",
				doc=self.assigned_but_unauthorised_ticket.name,
				user=self.engineer_user.name,
			)
		)

	def test_operations_l1_sees_everything(self):
		frappe.set_user(self.ops_l1_user.name)
		names = {
			row.name
			for row in frappe.get_list("AC Smart Hands Request", fields=["name"], limit_page_length=0)
		}
		self.assertIn(self.authorised_ticket.name, names)
		self.assertIn(self.unauthorised_ticket.name, names)

	def test_operations_l1_has_direct_read_permission_on_any_ticket(self):
		self.assertTrue(
			frappe.has_permission(
				"AC Smart Hands Request", doc=self.unauthorised_ticket.name, user=self.ops_l1_user.name
			)
		)
