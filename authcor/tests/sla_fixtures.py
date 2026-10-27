"""Shared test helpers for AC SLA Policy -- not a test module itself (no
Test* classes, filename doesn't match test_*), just imported by the test
files that need a policy to exist before creating a ticket.

The sixteen policy rows are reference data: shipped by
`authcor/sla_policies.py`, created by `after_install`, and editable in Desk
by an Authcor admin. A test needing particular targets cannot create a
row of its own beside them -- AC SLA Policy is autonamed
`{service_level}-{severity}`, so there is exactly one row per pair and the
only way to get specific values is to mutate the shared one. These helpers
do that, and then put it back.

**Putting it back means resetting to `SLA_POLICIES`, not to a snapshot.**
That is the lesson from the one time this went wrong.
`TestEscalationRuleMatching` asks for Standard-P1 at 15 minutes with no
onsite target; a run interrupted between the mutation and its class
cleanup (the suite used to die on a full Redis queue, which is exactly
that) left 15 on the site. From then on every later run snapshotted 15,
"restored" 15, and the wrong value looked like Authcor's own -- which is
how it reached `authcor/sla_policies.py` before Authcor's matrix was
checked against it. Resetting to the shipped table instead is
self-healing: the row goes back to the matrix value whatever it was
before, and a leak cannot survive the next run.

The cleanup is also registered *before* the write rather than after, so
there is no window in which the row is mutated and nothing is responsible
for restoring it.

What this cannot do is undo a mutation after `kill -9`, which leaves no
cleanup running at all. `test_sla_policies.TestSlaPolicyMatchesTheMatrix`
is the backstop for that: it fails with the bench command to repair the
row.
"""

import frappe

from authcor.sla_policies import SLA_POLICIES, policy_values

_MATRIX = {(row[0], row[1]): policy_values(row) for row in SLA_POLICIES}


def ensure_sla_policy(service_level, severity, **fields):
	"""Get-or-create an active AC SLA Policy for (service_level, severity)
	with the given field values, for the life of the current test.

	No commit and no cleanup: as long as the calling test never itself
	commits, IntegrationTestCase's rollback reverts this fixture -- a
	freshly inserted row, or a mutated existing one -- same as any other
	test data. A test that will force a commit (claim_ticket/add_engineer/
	remove_engineer, check_sla_breaches, or anything that notifies) must
	use ensure_sla_policy_committed instead: once anything commits there is
	no rollback left to restore this, and the mutation outlives the run.
	"""
	_write(service_level, severity, fields)


def ensure_sla_policy_committed(add_cleanup, service_level, severity, **fields):
	"""Same as ensure_sla_policy, but for a test that will itself force a
	commit, so the fixture is committed too and restored explicitly.

	`add_cleanup` is whatever registers a cleanup in the caller's context
	-- self.addCleanup in a test/setUp, cls.addClassCleanup in setUpClass.
	It is registered before the write, and it resets the row to its
	`SLA_POLICIES` values rather than to what happened to be there.
	"""
	pair = (service_level, severity)
	existed = frappe.db.exists("AC SLA Policy", {"service_level": service_level, "severity": severity})

	if existed and pair in _MATRIX:
		add_cleanup(reset_sla_policy_to_matrix, service_level, severity)
	else:
		# Nothing to reset to: leave the site as it was found.
		add_cleanup(_delete_sla_policy, f"{service_level}-{severity}")

	name = _write(service_level, severity, fields)
	frappe.db.commit()
	return name


def reset_sla_policy_to_matrix(service_level, severity):
	"""Put one policy back to the values authcor/sla_policies.py ships,
	and commit -- the caller committed the mutation, so the repair has to
	commit as well or it is rolled back with the rest of the test."""
	values = dict(_MATRIX[(service_level, severity)], is_active=1)
	frappe.db.set_value("AC SLA Policy", f"{service_level}-{severity}", values)
	frappe.db.commit()


def _write(service_level, severity, fields):
	name = f"{service_level}-{severity}"
	values = {"is_active": 1, **fields}
	if frappe.db.exists("AC SLA Policy", name):
		frappe.db.set_value("AC SLA Policy", name, values)
	else:
		frappe.get_doc(
			doctype="AC SLA Policy",
			service_level=service_level,
			severity=severity,
			**values,
		).insert(ignore_permissions=True)
	return name


def _delete_sla_policy(name):
	if frappe.db.exists("AC SLA Policy", name):
		frappe.delete_doc("AC SLA Policy", name, force=True, ignore_permissions=True)
	frappe.db.commit()
