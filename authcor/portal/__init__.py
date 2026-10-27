# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

"""PHASE-7-PORTAL.md section 2 -- routing and access.

Everything customer-facing passes through this module first. There is one
rule it exists to enforce, and it is worth stating plainly because every
other control in section 6 depends on it:

    the customer is derived from the session, never from the request.

No portal page accepts a customer identifier. `get_portal_customer()`
resolves `frappe.session.user` through `AC Customer.portal_users` and
throws if there is no active match, and detail pages verify ownership
explicitly on top of the User Permission that would already have caught
it. A page that accepted `?customer=` would be one typo away from being an
enumeration tool.
"""

from urllib.parse import quote

import frappe
from frappe import _

LOGIN_ROUTE = "/login"

# Section 3: status badges get their own muted palette so they don't fight
# the brand red. Open work is amber, work in hand is blue, work happening
# right now is the brand red, finished is green, cancelled is grey.
STATUS_BADGE_CLASS = {
	"Open": "amber",
	"Partially Claimed": "amber",
	"Claimed": "blue",
	"Scheduled": "blue",
	"In Progress": "active",
	"Completed": "green",
	"Closed": "green",
	"Cancelled": "grey",
}


def get_portal_customer(user=None):
	"""The single gate. Resolve the session user to the one AC Customer
	they are an active portal user of, or raise PermissionError.

	Deliberately strict about the ambiguous case. The rest of the
	permission model -- the User Permission on AC Customer, the
	denormalised `customer` on every ticket -- assumes a portal user
	belongs to exactly one customer, so silently picking one of two would
	hand the wrong company's data to whoever happened to be listed twice.
	Better to fail loudly and have an admin fix the membership."""
	user = user or frappe.session.user

	if user == "Guest":
		raise frappe.PermissionError(_("Portal access requires a signed-in portal user."))

	customers = frappe.get_all(
		"AC Customer User",
		filters={
			"parenttype": "AC Customer",
			"parentfield": "portal_users",
			"user": user,
			"is_active": 1,
		},
		pluck="parent",
		distinct=True,
	)

	if not customers:
		raise frappe.PermissionError(_("{0} is not an active portal user for any customer.").format(user))

	if len(customers) > 1:
		raise frappe.PermissionError(
			_("{0} is listed as a portal user for more than one customer. Ask Authcor to correct it.").format(
				user
			)
		)

	return frappe.get_doc("AC Customer", customers[0])


def login_redirect_location():
	"""Where a guest gets sent, with the route they asked for carried along
	so sign-in lands them back on it."""
	request = getattr(frappe.local, "request", None)
	path = getattr(request, "full_path", None) or getattr(request, "path", None)
	if not path:
		return LOGIN_ROUTE
	return f"{LOGIN_ROUTE}?redirect-to={quote(path.rstrip('?'), safe='/')}"


def enforce_portal_session(context, title=None, nav=None):
	"""The three things section 2 says every page must do, in one call.

	`context.no_cache` is not decoration. The website page cache is keyed
	on the request path with no user component (frappe.website.utils.
	cache_html), so a portal page that was ever allowed into it would be
	served to the next visitor on that URL whoever they are. Setting it
	here keeps every page out of the cache, which is what makes the
	per-session scoping below meaningful."""
	context.no_cache = 1

	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = login_redirect_location()
		raise frappe.Redirect

	customer = get_portal_customer()

	# Only scalars reach the template -- never the AC Customer document,
	# which carries the authorised-engineer and lead/head tables with it.
	context.portal_customer = customer.name
	context.portal_customer_title = customer.customer_name
	context.portal_nav = nav
	if title:
		context.title = title

	return customer


def get_owned_doc(doctype, name, customer):
	"""Section 2's detail-page rule: fetch by name, then verify ownership
	explicitly. The User Permission would catch this anyway -- check
	regardless, because defence in depth is the whole point and because it
	makes the intent readable at the top of the page.

	A name that does not exist is denied identically to one that belongs to
	someone else. Answering 404 for the first and 403 for the second would
	turn the route into an existence oracle over a sequential naming
	series, which is a slower version of the enumeration this module is
	built to prevent."""
	denied = frappe.PermissionError(_("This record is not available to your account."))

	if not name or not isinstance(name, str):
		raise denied

	if not frappe.db.exists(doctype, name):
		raise denied

	doc = frappe.get_doc(doctype, name)

	if doc.customer != customer.name:
		raise denied

	# The second mechanism: role permissions, User Permissions and the
	# doctype's own has_permission hook, none of which frappe.get_doc
	# consults on its own.
	doc.check_permission("read")

	return doc
