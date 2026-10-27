"""Create the sixteen AC SLA Policy records on a site that predates
authcor/sla_policies.py.

Create-only: a policy that already exists is left untouched, so this is
safe on a site whose admins have edited their own targets, and a no-op on
any site installed after this landed (after_install seeded them).
"""

from authcor.sla_policies import sync_and_report


def execute():
	sync_and_report()
