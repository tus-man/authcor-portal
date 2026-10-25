# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

"""/portal/tickets/<name> -- PHASE-7-PORTAL.md section 4, ticket detail.

Reached through the website_route_rules entry in hooks.py, which puts the
ticket name in frappe.form_dict. Section 2's rule for detail pages applies
in full: fetch by name, then verify the ticket belongs to the session's
customer explicitly, on top of the User Permission that would already have
caught it.
"""

import frappe

from authcor.portal import enforce_portal_session, get_owned_doc
from authcor.portal.tickets import TICKET, build_portal_ticket_view

no_cache = 1


def get_context(context):
	customer = enforce_portal_session(context, nav="tickets")

	doc = get_owned_doc(TICKET, frappe.form_dict.get("name"), customer)

	# The document stops here. Only the hand-built, permlevel-0 projection
	# is put on the context, so nothing in the template has a document to
	# reach an internal field through.
	context.ticket = build_portal_ticket_view(doc)
	context.title = doc.name
