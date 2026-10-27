"""The app's roles and their DocType permissions, as data.

Phase 1 created these by hand in Desk (`docs/PLAN.md` section 2), which left
them in one developer's database and nowhere else. A fresh site had no
`Client L1 Authorizer` for `add_roles()` to link to, so every test class
died in setUpClass and CI has never meaningfully run; a fresh VPS install
would have come up with every Authcor doctype readable by System Manager
alone. This module is the source of truth instead -- applied by
`after_install`, backfilled onto existing sites by
`authcor/patches/sync_role_permissions.py`, and read by the test fixtures,
so the suite and the deploy cannot disagree about what a role may do.

Kept as data rather than a `fixtures` export on purpose. A
`custom_docperm.json` would be 97 hash-named blobs that nobody can review
in a diff; this is a table that can be checked against `docs/PLAN.md`
section 2 line by line, which for a permission model is the whole point.

Two things in here are not obvious from the table itself:

- **The System Manager rows are not redundant.** The first Custom DocPerm
  row written against a doctype makes frappe ignore that doctype's standard
  DocPerm rows entirely (`get_valid_perms`). `setup_custom_perms` copies the
  standard rows across before the first write, so nothing is lost in
  practice -- but the matrix still has to state the full picture for every
  doctype it touches, because after that first write the JSON no longer
  contributes anything. Every Authcor doctype ships exactly one standard
  perm: System Manager at permlevel 0.

- **Engineers are `Operations L3 Authorizer`.** There is no engineer role;
  an engineer is a user with an active AC Engineer Profile, and in practice
  they hold Ops L3. That is why Ops L3 appears with permlevel 1 read on AC
  Smart Hands Request: engineers are meant to see SLA timers. Client roles
  hold no permlevel 1 grant anywhere, which is the line that matters.

The five configuration doctypes are in the table for the humans, not for
the app. Controllers read them server-side with get_doc / get_cached_doc,
which does not consult permissions at all, so the app would run with no
grant on any of them -- these rows exist so that an Authcor admin can
manage the configuration in Desk instead of needing a System Manager.

Two tiers there, deliberately. AC SLA Policy, AC Holiday Calendar and AC
Escalation Rule are ordinary records an admin maintains, so Admin L1 and L2
get read, write and create. AC Auth Settings and AC Notification Settings
are Singles holding switches that reach past their own doctype -- password
login per role, and the notification kill switch that silences every event
in the app -- so only Admin L1 gets them, and only read and write; there is
nothing to create on a Single anyway. `delete` stays with System Manager
throughout: removing an SLA policy or a holiday calendar that tickets
already reference is a different kind of act from editing one.

Child tables are absent because they inherit their parent's permissions.
"""

import frappe

# desk_access 0 keeps the client roles out of Desk altogether -- they reach
# the portal pages and nothing else.
ROLES = {
	"Admin L1": {"desk_access": 1},
	"Admin L2": {"desk_access": 1},
	"Admin L3": {"desk_access": 1},
	"Operations L1 Authorizer": {"desk_access": 1},
	"Operations L2 Authorizer": {"desk_access": 1},
	"Operations L3 Authorizer": {"desk_access": 1},
	"Client L1 Authorizer": {"desk_access": 0},
	"Client L2 Authorizer": {"desk_access": 0},
}

# The grantable properties of a Custom DocPerm row. Anything not named for a
# role below is written as 0, so the table reads as the whole truth about
# that row rather than a set of additions to whatever was there before.
# Validated against the live schema in _write_permission, so a typo here
# fails at install instead of silently granting nothing.
GRANTS = (
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"print",
	"email",
	"share",
	"select",
	"mask",
	"if_owner",
)

PERMISSIONS = {
	"AC Auth Settings": {
		0: {
			"Admin L1": "read write",
			"System Manager": "read write create delete print email share",
		},
	},
	"AC City": {
		0: {
			"Admin L1": "read write create delete",
			"Admin L2": "read write create",
			"Admin L3": "read",
			"Client L1 Authorizer": "read",
			"Client L2 Authorizer": "read",
			"Operations L1 Authorizer": "read",
			"Operations L2 Authorizer": "read",
			"Operations L3 Authorizer": "read",
			"System Manager": "read write create delete report export print email share",
		},
	},
	"AC Customer": {
		0: {
			"Admin L1": "read write create delete",
			"Admin L2": "read write create",
			"Admin L3": "read write",
			"Client L1 Authorizer": "read",
			"Client L2 Authorizer": "read",
			"Operations L1 Authorizer": "read",
			"Operations L2 Authorizer": "read",
			"Operations L3 Authorizer": "read",
			"System Manager": "read write create delete report export print email share",
		},
	},
	"AC Data Center": {
		0: {
			"Admin L1": "read write create delete",
			"Admin L2": "read write create",
			"Admin L3": "read",
			"Client L1 Authorizer": "read",
			"Client L2 Authorizer": "read",
			"Operations L1 Authorizer": "read",
			"Operations L2 Authorizer": "read",
			"Operations L3 Authorizer": "read",
			"System Manager": "read write create delete report export print email share",
		},
	},
	"AC Engineer Count Request": {
		0: {
			"Admin L1": "read write",
			"Admin L2": "read write",
			"Admin L3": "read write",
			"Client L1 Authorizer": "read create",
			"Client L2 Authorizer": "read create",
			"Operations L1 Authorizer": "read write",
			"Operations L2 Authorizer": "read",
			"Operations L3 Authorizer": "read",
			"System Manager": "read write create delete report export print email share",
		},
	},
	"AC Engineer Profile": {
		0: {
			"Admin L1": "read write create delete",
			"Admin L2": "read write create",
			"Admin L3": "read",
			"Operations L1 Authorizer": "read",
			"Operations L2 Authorizer": "read",
			"System Manager": "read write create delete report export print email share",
		},
		1: {
			"Admin L1": "read write export",
			"Admin L2": "read write export",
			"Operations L1 Authorizer": "read export",
		},
	},
	"AC Escalation Rule": {
		0: {
			"Admin L1": "read write create",
			"Admin L2": "read write create",
			"System Manager": "read write create delete report export print email share",
		},
	},
	"AC Expense Claim": {
		0: {
			"Admin L1": "read write create delete",
			"Admin L2": "read write create",
			"Admin L3": "read",
			"Client L1 Authorizer": "read",
			"Client L2 Authorizer": "read",
			"Operations L1 Authorizer": "read write create",
			"Operations L2 Authorizer": "read write create if_owner",
			"Operations L3 Authorizer": "read write create if_owner",
			"System Manager": "read write create delete report export print email share",
		},
	},
	"AC Holiday Calendar": {
		0: {
			"Admin L1": "read write create",
			"Admin L2": "read write create",
			"System Manager": "read write create delete report export print email share",
		},
	},
	"AC ID Disclosure Log": {
		0: {
			"Admin L1": "read",
			"Admin L2": "read",
			"System Manager": "read write create delete report export print email share",
		},
	},
	"AC Notification Log": {
		0: {
			"Admin L1": "read",
			"Admin L2": "read",
			"System Manager": "read write create delete report export print email share",
		},
	},
	"AC Notification Settings": {
		0: {
			"Admin L1": "read write",
			"System Manager": "read write create delete print email share",
		},
	},
	"AC Shipment Request": {
		0: {
			"Admin L1": "read write create delete",
			"Admin L2": "read write create",
			"Admin L3": "read write",
			"Client L1 Authorizer": "read create",
			"Client L2 Authorizer": "read create",
			"Operations L1 Authorizer": "read write create",
			"Operations L2 Authorizer": "read write create",
			"Operations L3 Authorizer": "read write create",
			"System Manager": "read write create delete report export print email share",
		},
	},
	"AC SLA Policy": {
		0: {
			"Admin L1": "read write create",
			"Admin L2": "read write create",
			"System Manager": "read write create delete report export print email share",
		},
	},
	"AC Smart Hands Request": {
		0: {
			"Admin L1": "read write create delete",
			"Admin L2": "read write create",
			"Admin L3": "read write",
			"Client L1 Authorizer": "read",
			"Client L2 Authorizer": "read create",
			"Operations L1 Authorizer": "read write create",
			"Operations L2 Authorizer": "read write",
			"Operations L3 Authorizer": "read",
			"System Manager": "read write create delete report export print email share",
		},
		1: {
			"Admin L1": "read export",
			"Admin L2": "read export",
			"Admin L3": "read export",
			"Operations L1 Authorizer": "read export",
			"Operations L2 Authorizer": "read export",
			"Operations L3 Authorizer": "read export",
		},
	},
	"AC Time Entry": {
		0: {
			"Admin L1": "read write create submit cancel",
			"Admin L2": "read write create submit cancel",
			"Admin L3": "read",
			"Client L1 Authorizer": "read",
			"Client L2 Authorizer": "read",
			"Operations L1 Authorizer": "read write create submit cancel",
			"Operations L2 Authorizer": "read write create submit",
			"Operations L3 Authorizer": "read write create submit",
			"System Manager": "read write create delete report export print email share",
		},
		1: {
			"Admin L1": "read write export",
			"Admin L2": "read write export",
			"Admin L3": "read export",
			"Operations L1 Authorizer": "read export",
		},
	},
}


def sync_role_permissions(overwrite=True):
	"""Create the roles and permission rows described above.

	`overwrite=True` -- install, and the backfill patch -- makes this module
	authoritative: an existing row is rewritten to match the table, so a
	site that has drifted comes back into line.

	`overwrite=False` -- the test fixtures -- is strictly get-or-create. It
	fills in what a fresh site is missing and leaves anything the site
	already has exactly as it stands, so running the suite can never quietly
	rewrite permissions a developer has been editing in Desk. The suite
	commits in several places, so that is a real risk and not a theoretical
	one.

	Returns the number of permission rows written, which is what the patch
	logs.
	"""
	from frappe.core.doctype.doctype.doctype import validate_permissions_for_doctype
	from frappe.permissions import setup_custom_perms

	_validate_table()
	_sync_roles(overwrite=overwrite)

	written = 0
	for doctype, levels in PERMISSIONS.items():
		if not frappe.db.exists("DocType", doctype):
			# A doctype that has not been migrated onto this site yet. The
			# patch runs post_model_sync so this should not happen, but
			# skipping beats failing the whole install over one name.
			continue

		# Copies the standard DocPerm rows into Custom DocPerm before the
		# first custom row lands, which is what stops System Manager losing
		# access on a fresh site. A no-op once any custom row exists.
		setup_custom_perms(doctype)

		for permlevel, roles in levels.items():
			for role, flags in roles.items():
				written += _write_permission(doctype, role, permlevel, flags, overwrite=overwrite)

		# Once per doctype, after all of its rows are in, for two reasons.
		# Cost is one: update_permission_property re-runs this on every
		# single property, which is most of the price of doing it the
		# obvious way. Correctness is the other: frappe rejects a permlevel
		# 1 row for a role that has no permlevel 0 row yet
		# (check_level_zero_is_set), so validating mid-doctype would depend
		# on the table happening to list level 0 first.
		validate_permissions_for_doctype(doctype)

	# Permissions are cached per doctype, and a freshly written row is
	# invisible to frappe.has_permission until the cache is dropped.
	frappe.clear_cache()
	return written


def sync_and_report():
	"""sync_role_permissions(), plus a line on stdout saying what it did.

	What the patches call. print, not frappe.logger(): a logger from
	frappe.logger() runs at WARNING on a dev server and ERROR otherwise
	(frappe/utils/logger.py: default_log_level), so an .info() call is
	discarded before it reaches a file -- and even when it is not, it lands
	in logs/frappe.log rather than in the terminal running the migrate.
	bench migrate shows a patch's stdout, and "did this change anything" is
	exactly what someone running it wants to see.
	"""
	written = sync_role_permissions()
	print(
		f"authcor: {written} role permission row(s) written"
		if written
		else "authcor: role permissions already match authcor/roles.py"
	)
	return written


def _sync_roles(overwrite=True):
	for role_name, properties in ROLES.items():
		if not frappe.db.exists("Role", role_name):
			frappe.get_doc(doctype="Role", role_name=role_name, disabled=0, **properties).insert(
				ignore_permissions=True
			)
			continue
		if not overwrite:
			continue
		role = frappe.get_doc("Role", role_name)
		if any(role.get(key) != value for key, value in properties.items()):
			role.update(properties)
			role.save(ignore_permissions=True)


def _validate_table():
	"""Fail before writing anything, rather than part way through.

	Checks the table's own vocabulary and checks GRANTS against the live
	Custom DocPerm schema -- a flag misspelled here would otherwise be
	written as a silent no-grant, which on a permission matrix is the
	failure mode worth the most care.
	"""
	fieldnames = {df.fieldname for df in frappe.get_meta("Custom DocPerm").fields}
	missing = set(GRANTS) - fieldnames
	if missing:
		frappe.throw(f"Custom DocPerm has no field(s) {sorted(missing)} -- GRANTS is out of date.")

	for doctype, levels in PERMISSIONS.items():
		for permlevel, roles in levels.items():
			for role, flags in roles.items():
				unknown = set(flags.split()) - set(GRANTS)
				if unknown:
					frappe.throw(
						f"Unknown permission flag(s) {sorted(unknown)} on "
						f"{doctype} / {role} / permlevel {permlevel}."
					)


def _write_permission(doctype, role, permlevel, flags, overwrite=True):
	"""One Custom DocPerm row. Returns 1 if it wrote, 0 if it left it alone."""
	granted = flags.split()

	# (parent, role, permlevel) is treated as unique. frappe's own
	# add_permission keys on if_owner too, which would allow a second row per
	# pair; the table above has no such pair, and a duplicate would make
	# "what may this role do" ambiguous, so this collapses onto one row and
	# carries if_owner as an ordinary flag.
	existing = frappe.db.get_value(
		"Custom DocPerm", {"parent": doctype, "role": role, "permlevel": permlevel}
	)
	if existing and not overwrite:
		return 0

	values = dict.fromkeys(GRANTS, 0) | dict.fromkeys(granted, 1)
	if existing:
		perm = frappe.get_doc("Custom DocPerm", existing)
		if all(perm.get(key) == value for key, value in values.items()):
			return 0
		perm.update(values)
		perm.save(ignore_permissions=True)
		return 1

	frappe.get_doc(
		doctype="Custom DocPerm",
		parent=doctype,
		parenttype="DocType",
		parentfield="permissions",
		role=role,
		permlevel=permlevel,
		**values,
	).insert(ignore_permissions=True)
	return 1
