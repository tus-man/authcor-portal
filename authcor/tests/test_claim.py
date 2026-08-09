import threading

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate

from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	add_engineer,
	claim_ticket,
	remove_engineer,
)

SGT = "Asia/Singapore"


def _make_city(name):
	return frappe.get_doc(doctype="AC City", city_name=name, country="Singapore", timezone=SGT).insert(
		ignore_permissions=True
	)


def _make_data_center(name, city):
	return frappe.get_doc(doctype="AC Data Center", data_center_name=name, city=city).insert(
		ignore_permissions=True
	)


def _make_engineer(email, id_type="NRIC", id_number="S1234567A", id_expiry=None, is_active=1):
	user = frappe.get_doc(
		doctype="User", email=email, first_name=email.split("@", 1)[0], send_welcome_email=0
	).insert(ignore_permissions=True)
	user.add_roles("Operations L3 Authorizer")
	profile = frappe.get_doc(
		doctype="AC Engineer Profile",
		user=user.name,
		phone="+65 8000 0000",
		id_type=id_type,
		id_number=id_number,
		id_expiry=id_expiry,
		is_active=is_active,
	).insert(ignore_permissions=True)
	return user, profile


def _make_user(email, roles):
	user = frappe.get_doc(
		doctype="User", email=email, first_name=email.split("@", 1)[0], send_welcome_email=0
	).insert(ignore_permissions=True)
	for role in roles:
		user.add_roles(role)
	return user


def _make_customer(name, authorised_engineers, portal_users=None, leads_and_heads=None):
	return frappe.get_doc(
		doctype="AC Customer",
		customer_name=name,
		service_level="Standard",
		authorised_engineers=[{"engineer": u} for u in authorised_engineers],
		portal_users=[
			{"user": u, "authorizer_level": "L1 Authorizer", "is_active": 1} for u in (portal_users or [])
		],
		leads_and_heads=[{"user": u, "designation": "Lead"} for u in (leads_and_heads or [])],
	).insert(ignore_permissions=True)


def _make_ticket(customer, city, data_center, engineers_required=1):
	return frappe.get_doc(
		doctype="AC Smart Hands Request",
		customer=customer,
		country="Singapore",
		city=city,
		data_center=data_center,
		subject="Claim test ticket",
		description="Test",
		action_required="Remote Hands",
		engineers_required=engineers_required,
		status="In Pool",
	).insert(ignore_permissions=True)


def _delete_ticket(ticket_name):
	for log in frappe.get_all("AC ID Disclosure Log", filters={"ticket": ticket_name}, pluck="name"):
		frappe.delete_doc("AC ID Disclosure Log", log, force=True, ignore_permissions=True)
	if frappe.db.exists("AC Smart Hands Request", ticket_name):
		frappe.delete_doc("AC Smart Hands Request", ticket_name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _cleanup_fixtures(user_emails, customer_name, dc_name, city_name):
	if frappe.db.exists("AC Customer", customer_name):
		frappe.delete_doc("AC Customer", customer_name, force=True, ignore_permissions=True)
	for email in user_emails:
		profile_name = frappe.db.get_value("AC Engineer Profile", {"user": email}, "name")
		if profile_name:
			frappe.delete_doc("AC Engineer Profile", profile_name, force=True, ignore_permissions=True)
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	if frappe.db.exists("AC Data Center", dc_name):
		frappe.delete_doc("AC Data Center", dc_name, force=True, ignore_permissions=True)
	if frappe.db.exists("AC City", city_name):
		frappe.delete_doc("AC City", city_name, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestClaimTicket(IntegrationTestCase):
	"""claim_ticket/add_engineer/remove_engineer all end with an explicit
	frappe.db.commit() (per PHASE-2B-CLAIM.md section 3 step 10) -- which,
	same as the login tests in test_auth.py, commits this whole test's
	pending transaction, not just what the function itself touched.
	Nothing here can rely on IntegrationTestCase's usual rollback, so
	class fixtures are committed and explicitly torn down, and each test
	gets its own fresh ticket via setUp/addCleanup."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.city = _make_city("Test Claim City")
		cls.dc = _make_data_center("Test Claim DC", cls.city.name)

		cls.engineer_a, cls.profile_a = _make_engineer("claim_eng_a@authcor.test")
		cls.engineer_b, cls.profile_b = _make_engineer("claim_eng_b@authcor.test")
		cls.engineer_c, cls.profile_c = _make_engineer("claim_eng_c@authcor.test")
		cls.unauthorised_engineer, cls.unauthorised_profile = _make_engineer("claim_eng_unauth@authcor.test")
		cls.inactive_engineer, cls.inactive_profile = _make_engineer(
			"claim_eng_inactive@authcor.test", is_active=0
		)
		cls.expired_engineer, cls.expired_profile = _make_engineer(
			"claim_eng_expired@authcor.test", id_type="FIN", id_expiry=add_days(getdate(), -1)
		)

		cls.portal_user = _make_user("claim_portal_user@authcor.test", roles=["Client L1 Authorizer"])
		cls.lead_user = _make_user("claim_lead_user@authcor.test", roles=["Client L1 Authorizer"])
		cls.admin_user = _make_user("claim_admin@authcor.test", roles=["Admin L1"])
		cls.ops_l1_user = _make_user("claim_ops_l1@authcor.test", roles=["Operations L1 Authorizer"])
		cls.ops_l3_user = _make_user("claim_ops_l3@authcor.test", roles=["Operations L3 Authorizer"])

		cls.customer = _make_customer(
			"Test Claim Customer",
			authorised_engineers=[
				cls.engineer_a.name,
				cls.engineer_b.name,
				cls.engineer_c.name,
				cls.inactive_engineer.name,
				cls.expired_engineer.name,
			],
			portal_users=[cls.portal_user.name],
			leads_and_heads=[cls.lead_user.name],
		)
		frappe.db.commit()

		cls.addClassCleanup(
			_cleanup_fixtures,
			[
				cls.engineer_a.name,
				cls.engineer_b.name,
				cls.engineer_c.name,
				cls.unauthorised_engineer.name,
				cls.inactive_engineer.name,
				cls.expired_engineer.name,
				cls.portal_user.name,
				cls.lead_user.name,
				cls.admin_user.name,
				cls.ops_l1_user.name,
				cls.ops_l3_user.name,
			],
			cls.customer.name,
			cls.dc.name,
			cls.city.name,
		)

	def setUp(self):
		super().setUp()
		self.ticket = _make_ticket(self.customer.name, self.city.name, self.dc.name, engineers_required=2)
		frappe.db.commit()
		self.addCleanup(_delete_ticket, self.ticket.name)
		self.addCleanup(frappe.set_user, "Administrator")

	def _reload_ticket(self):
		return frappe.get_doc("AC Smart Hands Request", self.ticket.name)

	# -- claim_ticket -------------------------------------------------

	def test_first_claimer_is_primary(self):
		frappe.set_user(self.engineer_a.name)
		claim_ticket(self.ticket.name)

		ticket = self._reload_ticket()
		self.assertEqual(len(ticket.assigned_engineers), 1)
		self.assertEqual(ticket.assigned_engineers[0].engineer, self.engineer_a.name)
		self.assertEqual(ticket.assigned_engineers[0].assignment_type, "Primary")
		self.assertEqual(ticket.status, "Partially Claimed")

	def test_second_claimer_is_support_and_ticket_becomes_claimed(self):
		frappe.set_user(self.engineer_a.name)
		claim_ticket(self.ticket.name)
		frappe.set_user(self.engineer_b.name)
		claim_ticket(self.ticket.name)

		ticket = self._reload_ticket()
		self.assertEqual(len(ticket.assigned_engineers), 2)
		by_engineer = {row.engineer: row.assignment_type for row in ticket.assigned_engineers}
		self.assertEqual(by_engineer[self.engineer_a.name], "Primary")
		self.assertEqual(by_engineer[self.engineer_b.name], "Support")
		self.assertEqual(ticket.status, "Claimed")

	def test_claiming_full_ticket_is_rejected(self):
		frappe.set_user(self.engineer_a.name)
		claim_ticket(self.ticket.name)
		frappe.set_user(self.engineer_b.name)
		claim_ticket(self.ticket.name)

		frappe.set_user(self.engineer_c.name)
		with self.assertRaises(frappe.ValidationError):
			claim_ticket(self.ticket.name)

	def test_claiming_twice_as_same_engineer_is_rejected(self):
		frappe.set_user(self.engineer_a.name)
		claim_ticket(self.ticket.name)
		with self.assertRaises(frappe.ValidationError):
			claim_ticket(self.ticket.name)

	def test_unauthorised_engineer_is_rejected(self):
		frappe.set_user(self.unauthorised_engineer.name)
		with self.assertRaises(frappe.ValidationError):
			claim_ticket(self.ticket.name)

	def test_inactive_engineer_profile_is_rejected(self):
		frappe.set_user(self.inactive_engineer.name)
		with self.assertRaises(frappe.ValidationError):
			claim_ticket(self.ticket.name)

	def test_expired_fin_engineer_cannot_claim(self):
		frappe.set_user(self.expired_engineer.name)
		with self.assertRaises(frappe.ValidationError) as ctx:
			claim_ticket(self.ticket.name)
		# formatdate() renders in the site's locale date format, not ISO --
		# compare against the same call the implementation makes rather
		# than the raw value, so this isn't fragile to that format.
		self.assertIn(frappe.utils.formatdate(self.expired_profile.id_expiry), str(ctx.exception))

	def test_disclosure_log_written_on_claim(self):
		frappe.set_user(self.engineer_a.name)
		claim_ticket(self.ticket.name)

		logs = frappe.get_all(
			"AC ID Disclosure Log",
			filters={"ticket": self.ticket.name},
			fields=["customer", "engineer", "disclosed_to", "channel"],
		)
		self.assertTrue(logs, "expected at least one disclosure log row")
		for log in logs:
			self.assertEqual(log.customer, self.customer.name)
			self.assertEqual(log.engineer, self.profile_a.name)
			self.assertEqual(log.channel, "Email")

		recipient_emails = {log.disclosed_to for log in logs}
		expected_portal_email = frappe.db.get_value("User", self.portal_user.name, "email")
		expected_lead_email = frappe.db.get_value("User", self.lead_user.name, "email")
		self.assertIn(expected_portal_email, recipient_emails)
		self.assertIn(expected_lead_email, recipient_emails)

	# -- add_engineer / remove_engineer --------------------------------

	def test_add_engineer_by_admin_records_admin_as_added_by(self):
		frappe.set_user(self.admin_user.name)
		add_engineer(self.ticket.name, self.engineer_a.name)

		ticket = self._reload_ticket()
		self.assertEqual(len(ticket.assigned_engineers), 1)
		row = ticket.assigned_engineers[0]
		self.assertEqual(row.engineer, self.engineer_a.name)
		self.assertEqual(row.added_by, self.admin_user.name)

	def test_add_engineer_denied_for_plain_engineer_role(self):
		frappe.set_user(self.ops_l3_user.name)
		with self.assertRaises(frappe.PermissionError):
			add_engineer(self.ticket.name, self.engineer_a.name)

	def test_remove_primary_promotes_earliest_support(self):
		frappe.set_user(self.admin_user.name)
		add_engineer(self.ticket.name, self.engineer_a.name)  # Primary
		add_engineer(self.ticket.name, self.engineer_b.name)  # Support

		remove_engineer(self.ticket.name, self.engineer_a.name)

		ticket = self._reload_ticket()
		remaining = {row.engineer: row.assignment_type for row in ticket.assigned_engineers}
		self.assertNotIn(self.engineer_a.name, remaining)
		self.assertEqual(remaining[self.engineer_b.name], "Primary")

	def test_removing_engineer_from_full_ticket_returns_to_partially_claimed(self):
		frappe.set_user(self.admin_user.name)
		add_engineer(self.ticket.name, self.engineer_a.name)
		add_engineer(self.ticket.name, self.engineer_b.name)
		self.assertEqual(self._reload_ticket().status, "Claimed")

		remove_engineer(self.ticket.name, self.engineer_b.name)

		self.assertEqual(self._reload_ticket().status, "Partially Claimed")

	def test_remove_engineer_denied_for_plain_engineer_role(self):
		frappe.set_user(self.admin_user.name)
		add_engineer(self.ticket.name, self.engineer_a.name)

		frappe.set_user(self.ops_l3_user.name)
		with self.assertRaises(frappe.PermissionError):
			remove_engineer(self.ticket.name, self.engineer_a.name)


class TestClaimRaceCondition(IntegrationTestCase):
	"""This is a genuinely concurrent test, not a sequential one: each
	competing claim runs on its own OS thread with its own
	frappe.init()/frappe.connect() -- a real, separate MySQL connection --
	synchronized with a threading.Barrier so all threads attempt
	claim_ticket() at nearly the same instant, rather than one after
	another reusing this test's own connection.

	That distinction is the entire point. A sequential "call claim_ticket
	twice in this same test" would pass whether or not the `for_update`
	lock is present in the implementation, because there is no
	concurrency for a missing lock to fail to serialize -- it would only
	prove the count check. This test can only pass *reliably*, across
	many competing threads all racing the same two-slot ticket, if the
	lock genuinely makes overlapping writers block and wait rather than
	interleave.

	Because the lock is real DB-level locking, the fixture data (customer,
	engineers, ticket) has to actually be committed before the threads
	start -- each thread's connection is a separate session and can't see
	this test's own uncommitted transaction.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.city = _make_city("Test Race City")
		cls.dc = _make_data_center("Test Race DC", cls.city.name)
		cls.num_engineers = 6
		cls.engineers_required = 2

		cls.engineers = []
		for i in range(cls.num_engineers):
			user, _profile = _make_engineer(f"claim_race_eng_{i}@authcor.test")
			cls.engineers.append(user)

		cls.customer = _make_customer("Test Race Customer", authorised_engineers=[u.name for u in cls.engineers])
		frappe.db.commit()

		cls.addClassCleanup(
			_cleanup_fixtures,
			[u.name for u in cls.engineers],
			cls.customer.name,
			cls.dc.name,
			cls.city.name,
		)

	def setUp(self):
		super().setUp()
		self.ticket = _make_ticket(
			self.customer.name, self.city.name, self.dc.name, engineers_required=self.engineers_required
		)
		frappe.db.commit()
		self.addCleanup(_delete_ticket, self.ticket.name)

	def test_concurrent_claims_never_exceed_engineers_required(self):
		site = frappe.local.site
		ticket_name = self.ticket.name
		barrier = threading.Barrier(self.num_engineers)
		results = [None] * self.num_engineers

		def worker(index, user_email):
			frappe.init(site, force=True)
			try:
				frappe.connect()
				frappe.set_user(user_email)
				try:
					barrier.wait(timeout=10)
				except threading.BrokenBarrierError:
					results[index] = "error: barrier broken"
					return
				try:
					claim_ticket(ticket_name)
					results[index] = "ok"
				except Exception as e:
					results[index] = f"rejected: {e}"
			finally:
				frappe.destroy()

		threads = [
			threading.Thread(target=worker, args=(i, self.engineers[i].name))
			for i in range(self.num_engineers)
		]
		for t in threads:
			t.start()
		for t in threads:
			t.join(timeout=30)

		self.assertTrue(all(r is not None for r in results), f"a worker thread never finished: {results}")

		successes = [r for r in results if r == "ok"]
		self.assertEqual(
			len(successes),
			self.engineers_required,
			f"expected exactly {self.engineers_required} successful claims out of "
			f"{self.num_engineers} concurrent attempts on a two-slot ticket; results={results}",
		)

		final = frappe.get_doc("AC Smart Hands Request", ticket_name)
		self.assertEqual(len(final.assigned_engineers), self.engineers_required)
		self.assertEqual(len({row.engineer for row in final.assigned_engineers}), self.engineers_required)
		self.assertEqual(final.status, "Claimed")
