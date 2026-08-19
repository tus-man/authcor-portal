import frappe
from frappe.tests import IntegrationTestCase

from authcor.authcor.doctype.ac_engineer_count_request.ac_engineer_count_request import (
	approve_count_request,
	reject_count_request,
	withdraw_count_request,
)
from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	add_engineer,
	increase_engineer_count,
)
from authcor.tests.sla_fixtures import ensure_sla_policy_committed

SGT = "Asia/Singapore"


def _make_city(name):
	return frappe.get_doc(doctype="AC City", city_name=name, country="Singapore", timezone=SGT).insert(
		ignore_permissions=True
	)


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
	"""Mirrors how a Client L1/L2 portal user's visibility is actually
	scoped in production -- a User Permission binding them to their own
	AC Customer, on top of the doctype-level read/create role permission
	(PHASE-3B-ENGINEER-COUNT.md section 2)."""
	frappe.get_doc(
		doctype="User Permission",
		user=user,
		allow="AC Customer",
		for_value=customer,
		apply_to_all_doctypes=1,
	).insert(ignore_permissions=True)


def _make_ticket(customer, city, data_center, engineers_required=1):
	return frappe.get_doc(
		doctype="AC Smart Hands Request",
		customer=customer,
		country="Singapore",
		city=city,
		data_center=data_center,
		subject="Engineer count test ticket",
		description="Test",
		action_required="Remote Hands",
		engineers_required=engineers_required,
		status="In Pool",
	).insert(ignore_permissions=True)


def _delete_ticket(ticket_name):
	for log in frappe.get_all("AC ID Disclosure Log", filters={"ticket": ticket_name}, pluck="name"):
		frappe.delete_doc("AC ID Disclosure Log", log, force=True, ignore_permissions=True)
	for req in frappe.get_all("AC Engineer Count Request", filters={"ticket": ticket_name}, pluck="name"):
		frappe.delete_doc("AC Engineer Count Request", req, force=True, ignore_permissions=True)
	if frappe.db.exists("AC Smart Hands Request", ticket_name):
		frappe.delete_doc("AC Smart Hands Request", ticket_name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _cleanup_fixtures(user_emails, customer_names, dc_name, city_name):
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
	frappe.db.commit()


class TestEngineerCountChange(IntegrationTestCase):
	"""increase_engineer_count/approve_count_request/reject_count_request/
	withdraw_count_request all end with an explicit frappe.db.commit() --
	same reasoning as TestClaimTicket in test_claim.py, so this class
	manages its own fixture commit/teardown instead of relying on
	IntegrationTestCase's usual rollback."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.city = _make_city("Test ECR City")
		cls.dc = _make_data_center("Test ECR DC", cls.city.name)

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

		cls.engineer_a, cls.profile_a = _make_engineer("ecr_eng_a@authcor.test")
		cls.engineer_b, cls.profile_b = _make_engineer("ecr_eng_b@authcor.test")
		cls.engineer_c, cls.profile_c = _make_engineer("ecr_eng_c@authcor.test")
		cls.unassigned_engineer, cls.unassigned_profile = _make_engineer("ecr_eng_unassigned@authcor.test")

		cls.admin_user = _make_user("ecr_admin@authcor.test", roles=["Admin L1"])
		cls.portal_user = _make_user("ecr_portal@authcor.test", roles=["Client L1 Authorizer"])
		cls.other_portal_user = _make_user("ecr_other_portal@authcor.test", roles=["Client L1 Authorizer"])

		cls.customer = _make_customer(
			"Test ECR Customer",
			authorised_engineers=[
				cls.engineer_a.name,
				cls.engineer_b.name,
				cls.engineer_c.name,
				cls.unassigned_engineer.name,
			],
			portal_users=[cls.portal_user.name],
		)
		cls.other_customer = _make_customer(
			"Test ECR Other Customer",
			authorised_engineers=[],
			portal_users=[cls.other_portal_user.name],
		)
		frappe.db.commit()

		_grant_customer_access(cls.portal_user.name, cls.customer.name)
		_grant_customer_access(cls.other_portal_user.name, cls.other_customer.name)
		frappe.db.commit()

		cls.addClassCleanup(
			_cleanup_fixtures,
			[
				cls.engineer_a.name,
				cls.engineer_b.name,
				cls.engineer_c.name,
				cls.unassigned_engineer.name,
				cls.admin_user.name,
				cls.portal_user.name,
				cls.other_portal_user.name,
			],
			[cls.customer.name, cls.other_customer.name],
			cls.dc.name,
			cls.city.name,
		)

	def setUp(self):
		super().setUp()
		self.addCleanup(frappe.set_user, "Administrator")

	def _new_ticket(self, engineers_required=2):
		ticket = _make_ticket(
			self.customer.name, self.city.name, self.dc.name, engineers_required=engineers_required
		)
		frappe.db.commit()
		self.addCleanup(_delete_ticket, ticket.name)
		return ticket

	def _reload(self, ticket_name):
		return frappe.get_doc("AC Smart Hands Request", ticket_name)

	def _assign(self, ticket_name, engineer):
		frappe.set_user(self.admin_user.name)
		add_engineer(ticket_name, engineer)
		frappe.set_user("Administrator")

	# -- section 4: increases need no approval -----------------------------

	def test_increase_on_claimed_ticket_reopens_to_partially_claimed(self):
		ticket = self._new_ticket(engineers_required=2)
		self._assign(ticket.name, self.engineer_a.name)
		self._assign(ticket.name, self.engineer_b.name)
		self.assertEqual(self._reload(ticket.name).status, "Claimed")

		frappe.set_user(self.portal_user.name)
		increase_engineer_count(ticket.name, 3)

		reloaded = self._reload(ticket.name)
		self.assertEqual(reloaded.engineers_required, 3)
		self.assertEqual(reloaded.status, "Partially Claimed")
		self.assertEqual(frappe.db.count("AC Engineer Count Request", {"ticket": ticket.name}), 0)

	def test_increase_on_in_progress_ticket_rejected(self):
		ticket = self._new_ticket(engineers_required=2)
		self._reload(ticket.name).db_set("status", "In Progress")

		frappe.set_user(self.admin_user.name)
		with self.assertRaises(frappe.ValidationError):
			increase_engineer_count(ticket.name, 3)

	# -- section 3: validation on creation -----------------------------------

	def test_decrease_request_created_leaves_ticket_unchanged(self):
		ticket = self._new_ticket(engineers_required=2)

		request = frappe.get_doc(
			doctype="AC Engineer Count Request",
			ticket=ticket.name,
			requested_count=1,
			reason="Fewer engineers needed",
		).insert(ignore_permissions=True)

		self.assertEqual(request.status, "Pending")
		self.assertEqual(request.customer, self.customer.name)
		self.assertEqual(request.current_count, 2)

		reloaded = self._reload(ticket.name)
		self.assertEqual(reloaded.engineers_required, 2)
		self.assertEqual(reloaded.status, "In Pool")

	def test_decrease_request_on_in_progress_ticket_rejected(self):
		ticket = self._new_ticket(engineers_required=2)
		self._reload(ticket.name).db_set("status", "In Progress")

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=1
			).insert(ignore_permissions=True)

	def test_second_pending_request_on_same_ticket_rejected(self):
		ticket = self._new_ticket(engineers_required=3)
		frappe.get_doc(doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=1).insert(
			ignore_permissions=True
		)

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=2
			).insert(ignore_permissions=True)

	# -- section 5: decisions ------------------------------------------------

	def test_approve_with_wrong_removal_count_rejected(self):
		ticket = self._new_ticket(engineers_required=3)
		self._assign(ticket.name, self.engineer_a.name)
		self._assign(ticket.name, self.engineer_b.name)
		self._assign(ticket.name, self.engineer_c.name)

		request = frappe.get_doc(
			doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=1
		).insert(ignore_permissions=True)

		frappe.set_user(self.admin_user.name)
		with self.assertRaises(frappe.ValidationError):
			approve_count_request(request.name, [self.engineer_a.name])

	def test_approve_naming_unassigned_engineer_rejected(self):
		ticket = self._new_ticket(engineers_required=2)
		self._assign(ticket.name, self.engineer_a.name)
		self._assign(ticket.name, self.engineer_b.name)

		request = frappe.get_doc(
			doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=1
		).insert(ignore_permissions=True)

		frappe.set_user(self.admin_user.name)
		with self.assertRaises(frappe.ValidationError):
			approve_count_request(request.name, [self.unassigned_engineer.name])

	def test_approve_removing_primary_promotes_earliest_support(self):
		ticket = self._new_ticket(engineers_required=2)
		self._assign(ticket.name, self.engineer_a.name)  # Primary
		self._assign(ticket.name, self.engineer_b.name)  # Support

		request = frappe.get_doc(
			doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=1
		).insert(ignore_permissions=True)

		frappe.set_user(self.admin_user.name)
		approve_count_request(request.name, [self.engineer_a.name])

		reloaded = self._reload(ticket.name)
		self.assertEqual(len(reloaded.assigned_engineers), 1)
		self.assertEqual(reloaded.assigned_engineers[0].engineer, self.engineer_b.name)
		self.assertEqual(reloaded.assigned_engineers[0].assignment_type, "Primary")
		self.assertEqual(reloaded.engineers_required, 1)
		self.assertEqual(reloaded.status, "Claimed")

		reloaded_request = frappe.get_doc("AC Engineer Count Request", request.name)
		self.assertEqual(reloaded_request.status, "Approved")
		self.assertEqual(reloaded_request.decided_by, self.admin_user.name)
		self.assertIsNotNone(reloaded_request.decided_on)

	def test_approve_removing_two_of_three_leaves_correct_survivor_as_primary(self):
		ticket = self._new_ticket(engineers_required=3)
		self._assign(ticket.name, self.engineer_a.name)  # Primary
		self._assign(ticket.name, self.engineer_b.name)  # Support, earlier
		self._assign(ticket.name, self.engineer_c.name)  # Support, later

		request = frappe.get_doc(
			doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=1
		).insert(ignore_permissions=True)

		frappe.set_user(self.admin_user.name)
		approve_count_request(request.name, [self.engineer_a.name, self.engineer_b.name])

		reloaded = self._reload(ticket.name)
		self.assertEqual(len(reloaded.assigned_engineers), 1)
		self.assertEqual(reloaded.assigned_engineers[0].engineer, self.engineer_c.name)
		self.assertEqual(reloaded.assigned_engineers[0].assignment_type, "Primary")
		self.assertEqual(reloaded.engineers_required, 1)

	def test_approve_decrease_with_zero_assigned_succeeds_with_no_removals(self):
		ticket = self._new_ticket(engineers_required=2)

		request = frappe.get_doc(
			doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=1
		).insert(ignore_permissions=True)

		frappe.set_user(self.admin_user.name)
		approve_count_request(request.name, [])

		reloaded = self._reload(ticket.name)
		self.assertEqual(len(reloaded.assigned_engineers), 0)
		self.assertEqual(reloaded.engineers_required, 1)
		self.assertEqual(reloaded.status, "In Pool")

		reloaded_request = frappe.get_doc("AC Engineer Count Request", request.name)
		self.assertEqual(reloaded_request.status, "Approved")
		self.assertEqual(reloaded_request.decided_by, self.admin_user.name)

	def test_approve_decrease_with_fewer_assigned_than_requested_succeeds_with_no_removals(self):
		ticket = self._new_ticket(engineers_required=3)
		self._assign(ticket.name, self.engineer_a.name)

		request = frappe.get_doc(
			doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=2
		).insert(ignore_permissions=True)

		frappe.set_user(self.admin_user.name)
		approve_count_request(request.name, [])

		reloaded = self._reload(ticket.name)
		self.assertEqual({row.engineer for row in reloaded.assigned_engineers}, {self.engineer_a.name})
		self.assertEqual(reloaded.engineers_required, 2)
		self.assertEqual(reloaded.status, "Partially Claimed")

		reloaded_request = frappe.get_doc("AC Engineer Count Request", request.name)
		self.assertEqual(reloaded_request.status, "Approved")

	def test_approve_decrease_with_assigned_equal_to_requested_succeeds_with_no_removals(self):
		ticket = self._new_ticket(engineers_required=3)
		self._assign(ticket.name, self.engineer_a.name)
		self._assign(ticket.name, self.engineer_b.name)

		request = frappe.get_doc(
			doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=2
		).insert(ignore_permissions=True)

		frappe.set_user(self.admin_user.name)
		approve_count_request(request.name, [])

		reloaded = self._reload(ticket.name)
		self.assertEqual(
			{row.engineer for row in reloaded.assigned_engineers},
			{self.engineer_a.name, self.engineer_b.name},
		)
		self.assertEqual(reloaded.engineers_required, 2)
		self.assertEqual(reloaded.status, "Claimed")

		reloaded_request = frappe.get_doc("AC Engineer Count Request", request.name)
		self.assertEqual(reloaded_request.status, "Approved")

	def test_reject_leaves_ticket_completely_unchanged(self):
		ticket = self._new_ticket(engineers_required=2)
		self._assign(ticket.name, self.engineer_a.name)
		self._assign(ticket.name, self.engineer_b.name)
		before = self._reload(ticket.name)

		request = frappe.get_doc(
			doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=1
		).insert(ignore_permissions=True)

		frappe.set_user(self.admin_user.name)
		reject_count_request(request.name, note="Not approved")

		after = self._reload(ticket.name)
		self.assertEqual(after.status, before.status)
		self.assertEqual(after.engineers_required, before.engineers_required)
		self.assertEqual(
			{row.engineer for row in after.assigned_engineers},
			{row.engineer for row in before.assigned_engineers},
		)

		reloaded_request = frappe.get_doc("AC Engineer Count Request", request.name)
		self.assertEqual(reloaded_request.status, "Rejected")
		self.assertEqual(reloaded_request.decided_by, self.admin_user.name)

	# -- section 6: withdrawal ------------------------------------------------

	def test_withdraw_by_requesting_customer_succeeds(self):
		ticket = self._new_ticket(engineers_required=2)
		request = frappe.get_doc(
			doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=1
		).insert(ignore_permissions=True)

		frappe.set_user(self.portal_user.name)
		withdraw_count_request(request.name)

		self.assertEqual(frappe.get_doc("AC Engineer Count Request", request.name).status, "Withdrawn")

	def test_withdraw_by_another_customer_denied(self):
		ticket = self._new_ticket(engineers_required=2)
		request = frappe.get_doc(
			doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=1
		).insert(ignore_permissions=True)

		frappe.set_user(self.other_portal_user.name)
		with self.assertRaises(frappe.PermissionError):
			withdraw_count_request(request.name)

	# -- section 9: validated at creation is re-validated at approval --------

	def test_approve_rejected_when_ticket_moved_to_in_progress_after_request_raised(self):
		ticket = self._new_ticket(engineers_required=2)
		self._assign(ticket.name, self.engineer_a.name)
		self._assign(ticket.name, self.engineer_b.name)

		request = frappe.get_doc(
			doctype="AC Engineer Count Request", ticket=ticket.name, requested_count=1
		).insert(ignore_permissions=True)

		self._reload(ticket.name).db_set("status", "In Progress")

		frappe.set_user(self.admin_user.name)
		with self.assertRaises(frappe.ValidationError):
			approve_count_request(request.name, [self.engineer_a.name])
		frappe.set_user("Administrator")

		self.assertEqual(frappe.get_doc("AC Engineer Count Request", request.name).status, "Pending")
