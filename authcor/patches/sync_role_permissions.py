"""Backfill the roles and Custom DocPerm rows onto a site installed before
authcor/roles.py existed -- the dev site, and the VPS if it is deployed
before this lands.

Runs once per site (frappe records it in `tabPatch Log`), post_model_sync
because it needs the doctypes to exist. It is not an every-migrate sync: a
permission edited in Desk afterwards stays edited, which is the right
default while DocTypes are still Desk-managed. If the matrix should become
authoritative on every migrate, that is an `after_migrate` hook, and a
decision to take deliberately rather than inherit from a patch.
"""

from authcor.roles import sync_and_report


def execute():
	sync_and_report()
