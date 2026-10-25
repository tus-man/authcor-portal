# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

"""/portal/tickets -- PHASE-7-PORTAL.md section 4, the ticket list.

Filterable by status, newest first, paginated at twenty. No performance
columns: the portal shows no SLA information at all, per Authcor's
instruction that performance data stays internal.

Lives at www/portal/tickets/index.py rather than www/portal/tickets.py on
purpose. The raise-a-ticket page needs www/portal/tickets/new.py, which
makes www/portal/tickets a Python package -- and a package shadows a
sibling module of the same name, so frappe.get_module("authcor.www.portal.
tickets") would return the package and this get_context would never be
found. The index form resolves /portal/tickets through TemplatePage's
"/index.html" lookup instead.
"""

from urllib.parse import urlencode

import frappe
from frappe import _

from authcor.portal import enforce_portal_session
from authcor.portal.tickets import list_portal_tickets

no_cache = 1


def _page_url(status, page):
	query = {}
	if status:
		query["status"] = status
	if page > 1:
		query["page"] = page
	return "/portal/tickets?" + urlencode(query) if query else "/portal/tickets"


def get_context(context):
	customer = enforce_portal_session(context, title=_("Tickets"), nav="tickets")

	tickets = list_portal_tickets(
		customer,
		status=frappe.form_dict.get("status"),
		page=frappe.form_dict.get("page"),
	)

	# Links are built here rather than in the template so the query string
	# is encoded once, by urlencode, instead of being concatenated in Jinja.
	tickets["prev_url"] = _page_url(tickets["status"], tickets["page"] - 1) if tickets["has_prev"] else None
	tickets["next_url"] = _page_url(tickets["status"], tickets["page"] + 1) if tickets["has_next"] else None

	context.tickets = tickets
