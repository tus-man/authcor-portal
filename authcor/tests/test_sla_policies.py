"""The seeded AC SLA Policy table (authcor/sla_policies.py).

Nothing here asserts a particular response target: those are Authcor's
commercial numbers and the admins may revise them in Desk. What is tested
is the shape of the table and the one promise the seeding makes -- that it
creates what is missing and never touches what is there.

Non-committing throughout, so IntegrationTestCase's rollback puts the
site's real policies back at the end of the class.
"""

import frappe
from frappe.tests import IntegrationTestCase

from authcor.sla_policies import POLICY_FIELDS, SLA_POLICIES, policy_values, sync_sla_policies

PROBE = ("P4-Scheduled", "P3")


def _name(service_level, severity):
	return f"{service_level}-{severity}"


class TestSlaPolicyTable(IntegrationTestCase):
	def test_the_table_covers_every_pair_exactly_once(self):
		"""A dropped row is a service level that cannot raise a ticket, and
		a duplicated one is an ambiguous lookup -- the thing the
		{service_level}-{severity} autoname exists to prevent."""
		meta = frappe.get_meta("AC SLA Policy")
		service_levels = set(meta.get_field("service_level").options.split("\n")) - {""}
		severities = set(meta.get_field("severity").options.split("\n")) - {""}

		pairs = [(row[0], row[1]) for row in SLA_POLICIES]
		self.assertEqual(len(pairs), len(set(pairs)), msg="duplicate (service level, severity) row")
		self.assertEqual(
			set(pairs),
			{(level, severity) for level in service_levels for severity in severities},
		)

	def test_every_row_carries_a_value_for_every_field(self):
		for row in SLA_POLICIES:
			with self.subTest(policy=_name(row[0], row[1])):
				values = policy_values(row)
				self.assertEqual(set(values), set(POLICY_FIELDS))
				for fieldname, value in values.items():
					self.assertIsInstance(value, int, msg=fieldname)

	def test_a_row_is_either_a_duration_or_a_next_business_day_flag(self):
		"""Not both: _compute_due_datetime lets the flag win, so minutes
		beside a ticked flag would be a number that never applies."""
		for row in SLA_POLICIES:
			values = policy_values(row)
			with self.subTest(policy=_name(row[0], row[1])):
				self.assertFalse(
					values["response_minutes"] and values["response_next_business_day"],
					msg="response target set twice",
				)
				self.assertFalse(
					values["onsite_minutes"] and values["onsite_next_business_day"],
					msg="onsite target set twice",
				)


class TestSyncSlaPolicies(IntegrationTestCase):
	def test_sync_is_a_no_op_on_a_site_that_already_has_them(self):
		self.assertEqual(sync_sla_policies(), 0)

	def test_sync_recreates_a_missing_policy_with_the_tables_values(self):
		service_level, severity = PROBE
		expected = next(policy_values(row) for row in SLA_POLICIES if (row[0], row[1]) == PROBE)
		# force: a policy a ticket already links to must still be removable
		# here, and the rollback puts it back either way.
		frappe.delete_doc(
			"AC SLA Policy", _name(service_level, severity), force=True, ignore_permissions=True
		)

		self.assertEqual(sync_sla_policies(), 1)

		created = frappe.get_doc("AC SLA Policy", _name(service_level, severity))
		self.assertEqual(created.is_active, 1)
		for fieldname, value in expected.items():
			self.assertEqual(created.get(fieldname), value, msg=fieldname)

	def test_sync_does_not_overwrite_an_edited_policy(self):
		"""The whole reason seeding is create-only: Admin L1/L2 can edit
		these, and a migrate must not reset what they changed."""
		name = _name(*PROBE)
		frappe.db.set_value("AC SLA Policy", name, "response_minutes", 999)

		self.assertEqual(sync_sla_policies(), 0)

		self.assertEqual(frappe.db.get_value("AC SLA Policy", name, "response_minutes"), 999)

	def test_sync_leaves_a_deactivated_policy_deactivated(self):
		"""is_active = 0 is a decision too. Reactivating it would put a
		policy back in front of ticket creation that somebody retired."""
		name = _name(*PROBE)
		frappe.db.set_value("AC SLA Policy", name, "is_active", 0)

		self.assertEqual(sync_sla_policies(), 0)

		self.assertEqual(frappe.db.get_value("AC SLA Policy", name, "is_active"), 0)
