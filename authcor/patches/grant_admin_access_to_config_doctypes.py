"""Grant Admin L1/L2 access to the five configuration doctypes (AC SLA
Policy, AC Holiday Calendar, AC Escalation Rule, AC Notification Settings,
AC Auth Settings) on a site that has already run
authcor.patches.sync_role_permissions.

That patch is recorded in `tabPatch Log` and never runs again, so rows
added to authcor/roles.py after it ran need a patch of their own. This runs
the same sync: it is idempotent, so it writes the rows that are missing and
leaves every row that already matches exactly as it is. On a site installed
after this landed it is a no-op, because after_install applied the whole
table already.
"""

from authcor.roles import sync_and_report


def execute():
	sync_and_report()
