"""The seeded AC SLA Policy table (authcor/sla_policies.py).

Three things: the shape of the table, the one promise the seeding makes
(it creates what is missing and never touches what is there), and that the
site's own rows still match the table.

That last one is a backstop, not a style check. The fixtures in
sla_fixtures.py have to mutate these shared rows to test specific targets,
and a run killed outright leaves no cleanup running. That happened: a
Standard-P1 mutation survived, every later run re-blessed it, and the
wrong value reached sla_policies.py before Authcor's matrix was checked
against it. A divergence here means either that, or an admin edit in Desk
that SLA_POLICIES has not been told about -- in which case new sites are
being seeded with different numbers from this one. Either way it is worth
knowing, so the failure names the row and the bench command to fix it.

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


class TestSlaPolicyMatchesTheMatrix(IntegrationTestCase):
	def test_every_policy_on_this_site_matches_the_shipped_table(self):
		rows = {
			(row.service_level, row.severity): row
			for row in frappe.get_all(
				"AC SLA Policy",
				fields=["name", "service_level", "severity", "is_active", *POLICY_FIELDS],
			)
		}

		problems = []
		repairs = {}
		for table_row in SLA_POLICIES:
			pair = (table_row[0], table_row[1])
			name = _name(*pair)
			live = rows.get(pair)
			if live is None:
				problems.append(f"{name} is missing from this site")
				continue
			for fieldname, expected in policy_values(table_row).items():
				if live.get(fieldname) != expected:
					problems.append(f"{name}.{fieldname} is {live.get(fieldname)}, table says {expected}")
					repairs.setdefault(name, {})[fieldname] = expected

		if not problems:
			return

		commands = "\n".join(
			f'  frappe.db.set_value("AC SLA Policy", "{name}", {values})' for name, values in repairs.items()
		)
		self.fail(
			"This site's AC SLA Policy rows have drifted from authcor/sla_policies.py:\n  "
			+ "\n  ".join(problems)
			+ "\n\nIf the table is right, repair the site in bench console (which runs in an "
			"uncommitted transaction, so the commit matters):\n"
			+ commands
			+ "\n  frappe.db.commit()\n\nIf the site is right, the table is what needs changing -- "
			"and so does Authcor's matrix in docs/PHASE-3-SLA.md section 4."
		)
