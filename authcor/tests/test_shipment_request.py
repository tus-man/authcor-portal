import frappe
from frappe.tests import IntegrationTestCase

from authcor.authcor.doctype.ac_shipment_request.ac_shipment_request import (
	build_tracking_url,
	cancel_shipment,
	close_shipment,
	mark_received,
)
from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	get_allowed_cities,
	get_allowed_countries,
	get_allowed_data_centers,
)
from authcor.tests.sla_fixtures import ensure_sla_policy

SGT = "Asia/Singapore"


def _get_or_create(doctype, name, values):
	"""Get-or-create by primary name, tolerating a leftover row from an
	earlier interrupted run on a reused dev site -- CI always builds a
	fresh site, so this never bites there. Same "get-or-mutate" shape as
	sla_fixtures.ensure_sla_policy."""
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


def _make_customer(name, service_areas, service_level="Standard"):
	return _get_or_create(
		"AC Customer",
		name,
		{"customer_name": name, "service_level": service_level, "service_areas": service_areas},
	)


def _get_or_create_user(email):
	if frappe.db.exists("User", email):
		return frappe.get_doc("User", email)
	return frappe.get_doc(
		doctype="User", email=email, first_name=email.split("@", 1)[0], send_welcome_email=0
	).insert(ignore_permissions=True)


def _make_user(email, roles):
	user = _get_or_create_user(email)
	for role in roles:
		user.add_roles(role)
	return user


def _grant_customer_access(user, customer):
	"""Get-or-create: User Permission's own validate() rejects a second
	identical (user, allow, for_value) row with DuplicateEntryError
	regardless of its own random autoname -- same leftover-from-an-
	interrupted-run failure mode as a fixed-name insert, just enforced by
	the doctype's own business logic instead of the primary key."""
	if frappe.db.exists("User Permission", {"user": user, "allow": "AC Customer", "for_value": customer}):
		return
	frappe.get_doc(
		doctype="User Permission",
		user=user,
		allow="AC Customer",
		for_value=customer,
		apply_to_all_doctypes=1,
	).insert(ignore_permissions=True)


def _make_shipment(customer, city, data_center, **overrides):
	values = {
		"doctype": "AC Shipment Request",
		"customer": customer,
		"country": "Singapore",
		"city": city,
		"data_center": data_center,
		"subject": "Test shipment",
		"carrier": "DHL",
		"expected_delivery": "2026-07-01",
		"package_description": "2x switches",
	}
	values.update(overrides)
	return frappe.get_doc(values).insert(ignore_permissions=True)


def _make_ticket(customer, city, data_center, **overrides):
	values = {
		"doctype": "AC Smart Hands Request",
		"customer": customer,
		"country": "Singapore",
		"city": city,
		"data_center": data_center,
		"subject": "Related ticket",
		"description": "Test",
	}
	values.update(overrides)
	return frappe.get_doc(values).insert(ignore_permissions=True)


def _delete_shipment(shipment_name):
	frappe.delete_doc("AC Shipment Request", shipment_name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _cleanup_fixtures(user_emails, customer_names, dc_name, city_name):
	for name in customer_names:
		if frappe.db.exists("AC Customer", name):
			frappe.delete_doc("AC Customer", name, force=True, ignore_permissions=True)
	for email in user_emails:
		for perm in frappe.get_all("User Permission", filters={"user": email}, pluck="name"):
			frappe.delete_doc("User Permission", perm, force=True, ignore_permissions=True)
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	if frappe.db.exists("AC Data Center", dc_name):
		frappe.delete_doc("AC Data Center", dc_name, force=True, ignore_permissions=True)
	if frappe.db.exists("AC City", city_name):
		frappe.delete_doc("AC City", city_name, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestShipmentRequestLifecycle(IntegrationTestCase):
	"""PHASE-6-SHIPMENT.md sections 2-3."""

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
		cls.city = _make_city("Test Shipment City")
		cls.dc = _make_data_center("Test Shipment DC", cls.city.name)
		cls.customer = _make_customer(
			"Test Shipment Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
		)
		cls.other_customer = _make_customer(
			"Test Shipment Other Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
		)
		cls.ops_user = _make_user("shipment_ops@authcor.test", roles=["Operations L1 Authorizer"])
		cls.client_user = _make_user("shipment_client@authcor.test", roles=["Client L1 Authorizer"])

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _new_shipment(self, **overrides):
		return _make_shipment(self.customer.name, self.city.name, self.dc.name, **overrides)

	# -- defaults ------------------------------------------------------------

	def test_status_defaults_to_open(self):
		shipment = self._new_shipment()
		self.assertEqual(shipment.status, "Open")

	# -- carrier_other ---------------------------------------------------------

	def test_carrier_others_without_details_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self._new_shipment(carrier="Others", carrier_other="")

	def test_carrier_others_with_details_accepted(self):
		shipment = self._new_shipment(carrier="Others", carrier_other="Local Courier Co")
		self.assertEqual(shipment.carrier_other, "Local Courier Co")

	def test_non_others_carrier_does_not_require_details(self):
		shipment = self._new_shipment(carrier="DHL")
		self.assertFalse(shipment.carrier_other)

	# -- related_ticket cross-customer guard ------------------------------------

	def test_related_ticket_from_another_customer_rejected(self):
		other_ticket = _make_ticket(self.other_customer.name, self.city.name, self.dc.name)
		with self.assertRaises(frappe.ValidationError):
			self._new_shipment(related_ticket=other_ticket.name)

	def test_related_ticket_from_same_customer_accepted(self):
		ticket = _make_ticket(self.customer.name, self.city.name, self.dc.name)
		shipment = self._new_shipment(related_ticket=ticket.name)
		self.assertEqual(shipment.related_ticket, ticket.name)

	# -- Received gate ---------------------------------------------------------

	def test_received_without_receipt_proof_is_rejected(self):
		shipment = self._new_shipment()
		shipment.status = "Received"
		with self.assertRaises(frappe.ValidationError):
			shipment.save(ignore_permissions=True)

	def test_received_with_receipt_proof_succeeds_and_stamps_actual_delivery(self):
		shipment = self._new_shipment()
		shipment.append("receipt_proof", {"image": "/files/receipt.jpg", "image_type": "After"})
		shipment.status = "Received"
		shipment.save(ignore_permissions=True)

		self.assertEqual(shipment.status, "Received")
		self.assertIsNotNone(shipment.actual_delivery)

	def test_resaving_received_does_not_move_actual_delivery(self):
		shipment = self._new_shipment()
		shipment.append("receipt_proof", {"image": "/files/receipt.jpg", "image_type": "After"})
		shipment.status = "Received"
		shipment.save(ignore_permissions=True)
		original = shipment.actual_delivery

		shipment.notes = "Some follow-up note"
		shipment.save(ignore_permissions=True)

		self.assertEqual(shipment.actual_delivery, original)

	# -- stored_on ---------------------------------------------------------------

	def test_stored_on_stamps_when_warehouse_flag_set(self):
		shipment = self._new_shipment(store_in_warehouse=1)
		shipment.append("receipt_proof", {"image": "/files/receipt.jpg", "image_type": "After"})
		shipment.status = "Received"
		shipment.save(ignore_permissions=True)

		self.assertIsNotNone(shipment.stored_on)

	def test_stored_on_stays_unset_without_warehouse_flag(self):
		shipment = self._new_shipment(store_in_warehouse=0)
		shipment.append("receipt_proof", {"image": "/files/receipt.jpg", "image_type": "After"})
		shipment.status = "Received"
		shipment.save(ignore_permissions=True)

		self.assertIsNone(shipment.stored_on)

	# -- Closed gate ---------------------------------------------------------------

	def _received_shipment(self, store_in_warehouse=0):
		shipment = self._new_shipment(store_in_warehouse=store_in_warehouse)
		shipment.append("receipt_proof", {"image": "/files/receipt.jpg", "image_type": "After"})
		shipment.status = "Received"
		shipment.save(ignore_permissions=True)
		return shipment

	def test_closing_warehoused_shipment_without_location_is_rejected(self):
		shipment = self._received_shipment(store_in_warehouse=1)
		shipment.status = "Closed"
		with self.assertRaises(frappe.ValidationError):
			shipment.save(ignore_permissions=True)

	def test_closing_warehoused_shipment_with_location_succeeds(self):
		shipment = self._received_shipment(store_in_warehouse=1)
		shipment.storage_location = "Bay 4, Shelf B"
		shipment.status = "Closed"
		shipment.save(ignore_permissions=True)

		self.assertEqual(shipment.status, "Closed")

	def test_closing_non_warehoused_shipment_succeeds_with_no_location(self):
		shipment = self._received_shipment(store_in_warehouse=0)
		shipment.status = "Closed"
		shipment.save(ignore_permissions=True)

		self.assertEqual(shipment.status, "Closed")
		self.assertFalse(shipment.storage_location)

	# -- Cancelled -------------------------------------------------------------

	def test_cancelling_a_closed_shipment_is_rejected(self):
		shipment = self._received_shipment()
		shipment.status = "Closed"
		shipment.save(ignore_permissions=True)

		shipment.status = "Cancelled"
		with self.assertRaises(frappe.ValidationError):
			shipment.save(ignore_permissions=True)

	def test_cancelling_an_open_shipment_succeeds(self):
		shipment = self._new_shipment()
		shipment.status = "Cancelled"
		shipment.save(ignore_permissions=True)

		self.assertEqual(shipment.status, "Cancelled")

	# -- whitelisted status-transition methods: role gate -----------------------
	# (the actual permission denial happens before any write, so it can't
	# force a commit -- this test stays here rather than in
	# TestShipmentStatusActions below)

	def test_client_role_cannot_mark_received(self):
		shipment = self._new_shipment()
		shipment.append("receipt_proof", {"image": "/files/receipt.jpg", "image_type": "After"})
		shipment.save(ignore_permissions=True)

		frappe.set_user(self.client_user.name)
		with self.assertRaises(frappe.PermissionError):
			mark_received(shipment.name)

	# -- cascading location filters ---------------------------------------------

	def test_cascading_location_filters_respect_service_areas(self):
		frappe.set_user(self.ops_user.name)
		self.assertEqual(get_allowed_countries(customer=self.customer.name), ["Singapore"])
		self.assertEqual(
			get_allowed_cities(customer=self.customer.name, country="Singapore"), [self.city.name]
		)
		self.assertEqual(
			get_allowed_data_centers(customer=self.customer.name, city=self.city.name), [self.dc.name]
		)


class TestShipmentStatusActions(IntegrationTestCase):
	"""mark_received/close_shipment/cancel_shipment all end with an explicit
	frappe.db.commit() (same convention as claim_ticket/add_engineer/
	submit_claim elsewhere in this app), which breaks IntegrationTestCase's
	usual rollback for everything already pending in this class -- so
	fixtures are committed and explicitly torn down here, same shape as
	TestClaimTicket in test_claim.py, rather than relying on the normal
	per-class rollback the way TestShipmentRequestLifecycle above does."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.city = _make_city("Test Shipment Actions City")
		cls.dc = _make_data_center("Test Shipment Actions DC", cls.city.name)
		cls.customer = _make_customer(
			"Test Shipment Actions Customer",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
		)
		cls.ops_user = _make_user("shipment_actions_ops@authcor.test", roles=["Operations L1 Authorizer"])
		frappe.db.commit()

		cls.addClassCleanup(
			_cleanup_fixtures, [cls.ops_user.name], [cls.customer.name], cls.dc.name, cls.city.name
		)

	def setUp(self):
		super().setUp()
		self.shipment = _make_shipment(self.customer.name, self.city.name, self.dc.name)
		self.shipment.append("receipt_proof", {"image": "/files/receipt.jpg", "image_type": "After"})
		self.shipment.save(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(_delete_shipment, self.shipment.name)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_ops_role_can_mark_received_then_close(self):
		frappe.set_user(self.ops_user.name)
		received = mark_received(self.shipment.name)
		self.assertEqual(received.status, "Received")

		closed = close_shipment(self.shipment.name)
		self.assertEqual(closed.status, "Closed")

	def test_ops_role_cannot_cancel_a_closed_shipment(self):
		frappe.set_user(self.ops_user.name)
		mark_received(self.shipment.name)
		close_shipment(self.shipment.name)

		with self.assertRaises(frappe.ValidationError):
			cancel_shipment(self.shipment.name)

	def test_ops_role_can_cancel_an_open_shipment(self):
		frappe.set_user(self.ops_user.name)
		cancelled = cancel_shipment(self.shipment.name)
		self.assertEqual(cancelled.status, "Cancelled")


class TestShipmentRequestIsolation(IntegrationTestCase):
	"""PHASE-6-SHIPMENT.md section 6/8: customer isolation is supposed to
	come free from the standard User Permission on the `customer` Link
	field -- no has_permission/permission_query_conditions hook is
	registered for this DocType at all. This is the test that proves that
	claim true, for both the get_list path and the direct has_permission
	path."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.city = _make_city("Test Shipment Isolation City")
		cls.dc = _make_data_center("Test Shipment Isolation DC", cls.city.name)

		cls.customer_a = _make_customer(
			"Test Shipment Isolation Customer A",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
		)
		cls.customer_b = _make_customer(
			"Test Shipment Isolation Customer B",
			service_areas=[{"country": "Singapore", "city": cls.city.name, "data_center": cls.dc.name}],
		)

		cls.client_a = _make_user("shipment_iso_client_a@authcor.test", roles=["Client L1 Authorizer"])
		_grant_customer_access(cls.client_a.name, cls.customer_a.name)

		cls.shipment_a = _make_shipment(cls.customer_a.name, cls.city.name, cls.dc.name)
		cls.shipment_b = _make_shipment(cls.customer_b.name, cls.city.name, cls.dc.name)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_client_sees_own_customer_shipment_via_list(self):
		frappe.set_user(self.client_a.name)
		names = {
			row.name for row in frappe.get_list("AC Shipment Request", fields=["name"], limit_page_length=0)
		}
		self.assertIn(self.shipment_a.name, names)

	def test_client_does_not_see_other_customer_shipment_via_list(self):
		frappe.set_user(self.client_a.name)
		names = {
			row.name for row in frappe.get_list("AC Shipment Request", fields=["name"], limit_page_length=0)
		}
		self.assertNotIn(self.shipment_b.name, names)

	def test_client_has_direct_read_on_own_customer_shipment(self):
		self.assertTrue(
			frappe.has_permission("AC Shipment Request", doc=self.shipment_a.name, user=self.client_a.name)
		)

	def test_client_denied_direct_read_on_other_customer_shipment(self):
		self.assertFalse(
			frappe.has_permission("AC Shipment Request", doc=self.shipment_b.name, user=self.client_a.name)
		)


class TestBuildTrackingUrl(IntegrationTestCase):
	"""PHASE-6-SHIPMENT.md section 5. Pure function, no DB."""

	def test_dhl_url(self):
		self.assertEqual(
			build_tracking_url("DHL", "1234567890"),
			"https://www.dhl.com/en/express/tracking.html?AWB=1234567890",
		)

	def test_fedex_url(self):
		self.assertEqual(
			build_tracking_url("FedEx", "9876543210"),
			"https://www.fedex.com/fedextrack/?trknbr=9876543210",
		)

	def test_ups_url(self):
		self.assertEqual(
			build_tracking_url("UPS", "1Z999AA10123456784"),
			"https://www.ups.com/track?tracknum=1Z999AA10123456784",
		)

	def test_tnt_url(self):
		self.assertEqual(
			build_tracking_url("TNT", "GD123456789"),
			"https://www.tnt.com/express/en_us/site/shipping-tools/tracking.html?searchType=CON&cons=GD123456789",
		)

	def test_others_has_no_url(self):
		self.assertIsNone(build_tracking_url("Others", "12345"))

	def test_no_tracking_number_has_no_url(self):
		self.assertIsNone(build_tracking_url("DHL", ""))
		self.assertIsNone(build_tracking_url("DHL", None))

	def test_tracking_number_is_url_encoded(self):
		self.assertEqual(
			build_tracking_url("DHL", "AB 123/456"),
			"https://www.dhl.com/en/express/tracking.html?AWB=AB%20123%2F456",
		)
