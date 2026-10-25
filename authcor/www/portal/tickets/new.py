# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

"""/portal/tickets/new -- PHASE-7-PORTAL.md section 4, raise a ticket.

The cascading country -> city -> data centre selects are driven by this
customer's own service-area tree, rendered into the page as data. That is
not just a performance choice: a cascade fed by API calls would need a
customer on every request, and section 2 says no request on a portal page
carries one.

The preferred-slot grid is pre-populated with the next seven days, all
three bands ticked, exactly as the Desk form generates them -- Authcor's
reasoning being that a customer who never sees the grid would be offering
availability they never chose.
"""

import frappe
from frappe import _
from frappe.utils import format_date

from authcor.portal import enforce_portal_session
from authcor.portal.tickets import (
	SLOT_BAND_LABELS,
	action_options,
	create_portal_ticket,
	default_slot_grid,
	get_service_area_tree,
	method_path,
	severity_options,
)

no_cache = 1


def get_context(context):
	customer = enforce_portal_session(context, title=_("Raise a ticket"), nav="new-ticket")

	tree = get_service_area_tree(customer)

	context.service_areas = tree
	# Embedded for the cascade to read out of a <script type="application/
	# json"> block. `</` is escaped because an HTML parser ends a script
	# element on that sequence wherever it appears, string literal or not.
	# Nothing here is user-supplied -- these are Authcor-managed masters --
	# but the page must not depend on that staying true.
	context.service_areas_json = frappe.as_json(tree).replace("</", "<\\/")

	context.severity_options = severity_options()
	context.action_options = action_options()
	context.slot_band_labels = SLOT_BAND_LABELS
	context.slots = [
		{**row, "slot_label": format_date(row["slot_date"])} for row in default_slot_grid()
	]

	# Prefilled country and city, per PLAN.md section 8's Phase 7 note --
	# but only when they are actually inside this customer's service tree,
	# so a stale default cannot preselect something the form would then
	# reject.
	countries = {entry["name"] for entry in tree}
	cities = {city["name"] for entry in tree for city in entry["cities"]}
	context.default_country = customer.default_country if customer.default_country in countries else None
	context.default_city = (
		customer.default_city
		if context.default_country and customer.default_city in cities
		else None
	)

	context.create_method = method_path(create_portal_ticket)
