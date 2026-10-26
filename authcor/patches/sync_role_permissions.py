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

from authcor.roles import sync_role_permissions


def execute():
	written = sync_role_permissions()
	# print, not frappe.logger(): a logger set up by frappe.logger() runs at
	# WARNING on a dev server and ERROR otherwise (frappe/utils/logger.py:
	# default_log_level), so an .info() call here is discarded before it
	# reaches a file -- and even when it is not, it goes to logs/frappe.log
	# rather than to the terminal running the migrate. bench migrate shows a
	# patch's stdout, and "did this change anything" is exactly what someone
	# running it wants to see.
	print(
		f"authcor: {written} role permission row(s) written"
		if written
		else "authcor: role permissions already match authcor/roles.py"
	)
