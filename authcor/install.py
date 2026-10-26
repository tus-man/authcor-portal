"""Install-time setup. Wired up as `after_install` in hooks.py.

Everything here has to be safe to run against a site that already has the
app, because `authcor/patches/sync_role_permissions.py` calls the same
code to backfill sites installed before it existed.
"""

from authcor.roles import sync_role_permissions


def after_install():
	sync_role_permissions()
