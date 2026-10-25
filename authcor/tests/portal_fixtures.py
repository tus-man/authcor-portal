"""Shared test helpers for the Phase 7 portal -- not a test module itself
(no Test* classes, filename doesn't match test_*).

Two jobs.

**Role permissions.** The AC roles and their Role Permissions Manager rows
are Desk-managed data living in `tabRole` / `tabCustom DocPerm`, not in the
app's DocType JSON. A shared dev site already has them; a fresh CI site
does not. The portal reads tickets through the ordinary ORM precisely so
that User Permissions and permlevel do the work (PHASE-7-PORTAL.md section
1), which means these tests need the client roles' read grants to exist or
they fail for a reason that has nothing to do with the portal. These
helpers are strictly get-or-create: an existing Role or Custom DocPerm row
is left exactly as the site has it, never mutated to match.

**Portal membership.** `get_portal_customer()` resolves the session user
through `AC Customer.portal_users`, and detail pages are scoped by a User
Permission on AC Customer. Both are needed for every portal test.
"""

import frappe
from frappe.permissions import add_permission, update_permission_property

PORTAL_ROLES = ("Client L1 Authorizer", "Client L2 Authorizer")

# Roles the portal fixtures only need to *link* to -- an engineer profile
# fixture needs an Ops role on the user, and Administrator's bypass of
# validate_engineer_assignment works by holding every role that exists, so
# the unrestricted set has to exist for it to hold them. Created with no
# permission rows: a role with no DocPerm grants nothing.
FIXTURE_ROLES = (
	"Operations L1 Authorizer",
	"Operations L2 Authorizer",
	"Operations L3 Authorizer",
	"Admin L1",
	"Admin L2",
	"Admin L3",
)

# The minimum the portal itself needs. Deliberately not the full production
# matrix -- only the grants a Phase 7 page actually exercises, so a missing
# row here points at the portal rather than at unrelated Desk config.
PORTAL_DOCPERMS = (
	("AC Customer", "Client L1 Authorizer", {}),
	("AC Customer", "Client L2 Authorizer", {}),
	("AC Smart Hands Request", "Client L1 Authorizer", {}),
	("AC Smart Hands Request", "Client L2 Authorizer", {"create": 1}),
)


def ensure_portal_role_permissions():
	"""Get-or-create the roles and permission rows the portal pages read
	through. Idempotent, and never touches a row the site already has.

	Goes through frappe.permissions.add_permission rather than inserting
	Custom DocPerm directly: the first Custom DocPerm row on a doctype makes
	Frappe ignore that doctype's standard DocPerm rows entirely
	(get_valid_perms), so a bare insert on a fresh site would silently strip
	System Manager's access. add_permission calls setup_custom_perms first,
	which copies the standard rows across before adding anything."""
	for role in (*PORTAL_ROLES, *FIXTURE_ROLES):
		if not frappe.db.exists("Role", role):
			frappe.get_doc(
				doctype="Role", role_name=role, desk_access=0 if role in PORTAL_ROLES else 1
			).insert(ignore_permissions=True)

	added = False
	for doctype, role, extra_rights in PORTAL_DOCPERMS:
		if frappe.db.exists(
			"Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0}
		):
			continue
		# add_permission's default ptype is "read" -- that plus whatever else
		# the role needs on top.
		add_permission(doctype, role, 0)
		for ptype, value in extra_rights.items():
			update_permission_property(doctype, role, 0, ptype, value)
		added = True

	if added:
		# Permissions are cached per doctype; a freshly added row is invisible
		# to frappe.has_permission until the cache is dropped.
		frappe.clear_cache()


def ensure_portal_membership(user, customer):
	"""Both halves of how a portal user is actually scoped in production:
	an active `portal_users` row on their AC Customer (what
	get_portal_customer resolves through) and a User Permission binding
	them to it (what the ORM enforces on every read).

	Get-or-create on both counts. `User Permission`'s own validate()
	rejects a second identical (user, allow, for_value) row with
	DuplicateEntryError regardless of its own random autoname, so a
	leftover row from an interrupted run has to be tolerated here the same
	way a fixed-name insert does."""
	doc = frappe.get_doc("AC Customer", customer)
	if not any(row.user == user for row in doc.portal_users):
		doc.append("portal_users", {"user": user, "authorizer_level": "L1 Authorizer", "is_active": 1})
		doc.save(ignore_permissions=True)

	if not frappe.db.exists(
		"User Permission", {"user": user, "allow": "AC Customer", "for_value": customer}
	):
		frappe.get_doc(
			doctype="User Permission",
			user=user,
			allow="AC Customer",
			for_value=customer,
			apply_to_all_doctypes=1,
		).insert(ignore_permissions=True)
