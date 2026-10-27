"""The sixteen AC SLA Policy records, as data.

Same problem as `authcor/roles.py` and the same answer. Phase 3 created
these in Desk, so they lived in one developer's database: a fresh site had
none of them, and because `before_insert` resolves a policy for every
ticket (`_get_active_sla_policy`), *every* ticket insert on a fresh site
threw "No active AC SLA Policy". Applied by `after_install`, backfilled
onto existing sites by `authcor/patches/create_sla_policies.py`.

**Create-only, never overwrite.** Admin L1 and L2 can edit these policies
in Desk now (`authcor/roles.py`), and these are Authcor's own commercial
targets -- a `bench migrate` must not quietly reset a target somebody
changed on purpose. A policy that already exists for a (service level,
severity) pair is left exactly as it is, including a deactivated one:
`is_active = 0` is a decision too, and reactivating it would be the same
kind of surprise. That also means this module is not a source of truth the
way roles.py is -- it seeds, it does not reconcile. Nothing checks a
running site's policies against this table, by design.

Tests do not rely on these rows either: every test class that creates a
ticket makes its own policy through `authcor/tests/sla_fixtures.py`, for
the same reason -- an admin editing a response target must not be able to
turn the suite red.

The table below is Authcor's own matrix, confirmed against it row by row,
and it is the authority -- not any running site. The dev site had
Standard-P1 at 15 minutes with no onsite target where the matrix says 60
and 240; that was test fixture residue, not a decision (see
`authcor/tests/sla_fixtures.py`), and the matrix value is what ships.

Two differences from `docs/PHASE-3-SLA.md` section 4 remain, both
deliberate:

- **`response_next_business_day` does not exist in the doc at all.** The
  four P4-Scheduled rows carry it, and no onsite flag: for scheduled work
  the onsite obligation is the confirmed slot, and the response obligation
  is "the next working day", not a duration. The doc's NBD column for
  those rows refers to onsite.
- The doc still lists a `policy_name` Data field; the doctype has none,
  having moved to the `{service_level}-{severity}` autoname the same
  section suggests as the alternative.

One target is an open question with Authcor rather than a difference --
Basic-P3's "48 Hours (Business Days)" is built as 2880 business minutes,
about five working days. Recorded in `docs/PHASE-3-SLA.md`, "Open with
Authcor".
"""

import frappe

POLICY_FIELDS = (
	"response_minutes",
	"response_next_business_day",
	"onsite_minutes",
	"onsite_next_business_day",
	"business_hours_only",
)

# Response and onsite targets are in minutes. business_hours_only is ticked
# on Basic alone, where the targets are business minutes rather than
# wall-clock (960 of an 8-hour day is two working days, not sixteen hours).
# A zero target with no next-business-day flag beside it means no target at
# all -- every P4 row, which is judged against the agreed slot instead.
#
# service level     sev   resp   rNBD  onsite  oNBD  bizhrs
SLA_POLICIES = (
	("Premium", "P1", 60, 0, 180, 0, 0),
	("Premium", "P2", 120, 0, 360, 0, 0),
	("Premium", "P3", 180, 0, 1440, 0, 0),
	("Premium", "P4", 360, 0, 0, 0, 0),
	("Standard", "P1", 60, 0, 240, 0, 0),
	("Standard", "P2", 120, 0, 0, 1, 0),
	("Standard", "P3", 240, 0, 2160, 0, 0),
	("Standard", "P4", 480, 0, 0, 0, 0),
	("Basic", "P1", 120, 0, 480, 0, 1),
	("Basic", "P2", 240, 0, 0, 1, 1),
	("Basic", "P3", 480, 0, 2880, 0, 1),
	("Basic", "P4", 960, 0, 0, 0, 1),
	("P4-Scheduled", "P1", 0, 1, 0, 0, 0),
	("P4-Scheduled", "P2", 0, 1, 0, 0, 0),
	("P4-Scheduled", "P3", 0, 1, 0, 0, 0),
	("P4-Scheduled", "P4", 0, 1, 0, 0, 0),
)


def policy_values(row):
	"""{fieldname: value} for one row of the table, autoname fields aside."""
	return dict(zip(POLICY_FIELDS, row[2:], strict=True))


def sync_sla_policies():
	"""Insert the policies that are missing. Returns how many were created.

	Existence is checked by (service_level, severity) rather than by the
	autoname, and without filtering on is_active -- the pair is what
	`_get_active_sla_policy` looks up and what the autoname is built from,
	so a row for that pair in any state means there is nothing to create.
	"""
	_validate_table()

	created = 0
	for row in SLA_POLICIES:
		service_level, severity = row[0], row[1]
		if frappe.db.exists("AC SLA Policy", {"service_level": service_level, "severity": severity}):
			continue
		frappe.get_doc(
			doctype="AC SLA Policy",
			service_level=service_level,
			severity=severity,
			is_active=1,
			**policy_values(row),
		).insert(ignore_permissions=True)
		created += 1

	return created


def sync_and_report():
	"""sync_sla_policies(), plus a line on stdout saying what it did. What
	the patch calls -- print rather than frappe.logger() for the reason
	given in authcor.roles.sync_and_report."""
	created = sync_sla_policies()
	print(
		f"authcor: {created} AC SLA Policy row(s) created"
		if created
		else f"authcor: all {len(SLA_POLICIES)} AC SLA Policy rows already present"
	)
	return created


def _validate_table():
	"""Fail before writing anything, rather than part way through.

	Catches the table drifting from the schema: a renamed field would
	otherwise be dropped on the floor by get_doc and leave a policy with a
	silent zero target, and a service level or severity the Select no
	longer offers would be a row that no ticket can ever resolve.
	"""
	meta = frappe.get_meta("AC SLA Policy")
	fieldnames = {df.fieldname for df in meta.fields}
	missing = sorted(set(POLICY_FIELDS) - fieldnames)
	if missing:
		frappe.throw(f"AC SLA Policy has no field(s) {missing} -- POLICY_FIELDS is out of date.")

	for fieldname, index in (("service_level", 0), ("severity", 1)):
		options = set((meta.get_field(fieldname).options or "").split("\n")) - {""}
		unknown = sorted({row[index] for row in SLA_POLICIES} - options)
		if unknown:
			frappe.throw(f"AC SLA Policy.{fieldname} does not offer {unknown} -- SLA_POLICIES is stale.")
