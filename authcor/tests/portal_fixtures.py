"""Shared test helpers for the Phase 7 portal -- not a test module itself
(no Test* classes, filename doesn't match test_*).

Two jobs.

**Role permissions.** The portal reads tickets through the ordinary ORM
precisely so that User Permissions and permlevel do the work
(PHASE-7-PORTAL.md section 1), which means these tests need the client
roles' grants to exist or they fail for a reason that has nothing to do
with the portal. Those grants now live in `authcor/roles.py` and are
applied by `after_install`, so this delegates there rather than keeping a
second, smaller copy of the matrix that could disagree with what gets
deployed -- a test passing against a hand-written subset proved nothing
about production. Called with overwrite=False: it fills in what a site is
missing and never rewrites a row the site already has, because the suite
commits in several places and must not quietly normalise someone's Desk
edits.

**Portal membership.** `get_portal_customer()` resolves the session user
through `AC Customer.portal_users`, and detail pages are scoped by a User
Permission on AC Customer. Both are needed for every portal test.
"""

import frappe

from authcor.roles import sync_role_permissions


def ensure_portal_role_permissions():
	"""Create whatever of the app's roles and permission rows this site is
	missing. Idempotent, and never mutates a row the site already has.

	Every role in the matrix is created, not just the two client ones: the
	engineer-profile fixtures need an Ops role to put on a user, and
	Administrator's bypass of validate_engineer_assignment works by holding
	every role that exists, so the whole set has to be there for it to hold
	them."""
	sync_role_permissions(overwrite=False)


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

	if not frappe.db.exists("User Permission", {"user": user, "allow": "AC Customer", "for_value": customer}):
		frappe.get_doc(
			doctype="User Permission",
			user=user,
			allow="AC Customer",
			for_value=customer,
			apply_to_all_doctypes=1,
		).insert(ignore_permissions=True)
