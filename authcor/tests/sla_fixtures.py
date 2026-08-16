"""Shared test helpers for AC SLA Policy -- not a test module itself (no
Test* classes, filename doesn't match test_*), just imported by the test
files that need a policy to exist before creating a ticket.

The 16 real policy rows (PHASE-3-SLA.md section 4) are Desk-managed data:
a fresh CI site won't have them, but a shared dev site might already have
this exact one, since AC SLA Policy's autoname is `{service_level}-
{severity}`. That rules out an unconditional insert -- these helpers
get-or-mutate instead.
"""

import frappe

_SNAPSHOT_FIELDS = [
	"response_minutes",
	"onsite_minutes",
	"onsite_next_business_day",
	"business_hours_only",
	"response_next_business_day",
	"is_active",
]


def ensure_sla_policy(service_level, severity, **fields):
	"""Get-or-create an active AC SLA Policy for (service_level, severity)
	with the given field values, for the life of the current test.

	Deliberately does not commit or register any cleanup: as long as the
	calling test never itself commits, IntegrationTestCase's normal
	per-test rollback reverts this fixture -- a freshly inserted row, or a
	mutated existing one -- same as any other test data. A test that will
	force a commit (claim_ticket/add_engineer/remove_engineer, or
	check_sla_breaches) must use ensure_sla_policy_committed instead."""
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


def ensure_sla_policy_committed(add_cleanup, service_level, severity, **fields):
	"""Same as ensure_sla_policy, but for a test that will itself force a
	commit -- which breaks IntegrationTestCase's rollback for everything
	already pending, this fixture included (same reason TestClaimTicket
	manages its own teardown instead of relying on it). `add_cleanup` is
	whatever registers a cleanup in the caller's context --
	self.addCleanup in a test/setUp, cls.addClassCleanup in setUpClass."""
	name = f"{service_level}-{severity}"
	values = {"is_active": 1, **fields}
	if frappe.db.exists("AC SLA Policy", name):
		original = frappe.db.get_value("AC SLA Policy", name, _SNAPSHOT_FIELDS, as_dict=True)
		frappe.db.set_value("AC SLA Policy", name, values)
		add_cleanup(_restore_sla_policy, name, original)
	else:
		doc = frappe.get_doc(
			doctype="AC SLA Policy",
			service_level=service_level,
			severity=severity,
			**values,
		).insert(ignore_permissions=True)
		add_cleanup(_delete_sla_policy, doc.name)
	return name


def _restore_sla_policy(name, original_values):
	frappe.db.set_value("AC SLA Policy", name, original_values)
	frappe.db.commit()


def _delete_sla_policy(name):
	frappe.delete_doc("AC SLA Policy", name, force=True, ignore_permissions=True)
	frappe.db.commit()
