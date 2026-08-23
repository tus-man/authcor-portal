"""PHASE-5-NOTIFICATIONS.md section 4: one function per event, returning
User names. Centralising it here means a recipient rule can be read in
one place rather than inferred from a template.

None of these exclude the acting user or filter Client roles off sla_*
events -- notify() (authcor/notifications/__init__.py) does both, once,
for every event, so these stay plain "who is documented to hear about
this" lookups.
"""

import frappe
from frappe.core.doctype.role.role import get_users as get_users_with_role


def get_customer_contacts(customer):
	"""Portal users from AC Customer.portal_users."""
	return [row.user for row in frappe.get_cached_doc("AC Customer", customer).portal_users if row.user]


def get_leads_and_heads(customer):
	"""From AC Customer.leads_and_heads. Authcor confirmed these are
	copied on created, claimed, SLA breach, and closed -- applying them
	more broadly risks the noise problem that makes people filter
	notifications into a folder they never open."""
	return [row.user for row in frappe.get_cached_doc("AC Customer", customer).leads_and_heads if row.user]


def get_pool_engineers(customer):
	"""Engineers in AC Customer.authorised_engineers with an active
	profile. Pools are customer-scoped, not city-scoped -- AC Engineer
	Profile carries no per-engineer city field in this schema, so there
	was never anything to filter on. ticket_created/shipment_created's
	"authorised for that customer + city" wording in
	PHASE-5-NOTIFICATIONS.md section 3 turned out not to map to a real
	field; every authorised, active engineer for the customer is the
	actual pool."""
	engineers = [row.engineer for row in frappe.get_cached_doc("AC Customer", customer).authorised_engineers]
	if not engineers:
		return []
	return frappe.get_all("AC Engineer Profile", filters={"user": ["in", engineers], "is_active": 1}, pluck="user")


# -- Ticket events ------------------------------------------------------


def resolve_ticket_created(ticket):
	return get_pool_engineers(ticket.customer) + get_leads_and_heads(ticket.customer)


def resolve_ticket_claimed(ticket):
	return get_customer_contacts(ticket.customer) + get_leads_and_heads(ticket.customer)


def resolve_slot_confirmed(ticket):
	return get_customer_contacts(ticket.customer) + get_leads_and_heads(ticket.customer)


def resolve_ticket_in_progress(ticket):
	return get_customer_contacts(ticket.customer)


def resolve_ticket_completed(ticket):
	return get_customer_contacts(ticket.customer) + get_leads_and_heads(ticket.customer)


def resolve_ticket_closed(ticket):
	return get_customer_contacts(ticket.customer) + get_leads_and_heads(ticket.customer)


def resolve_severity_escalated(ticket):
	return [row.engineer for row in ticket.assigned_engineers] + get_leads_and_heads(ticket.customer)


def resolve_engineer_removed(removed_engineer_user):
	return [removed_engineer_user]


def resolve_count_request_raised(request):
	return get_users_with_role("Operations L1 Authorizer") + get_leads_and_heads(request.customer)


def resolve_count_request_decided(request, affected_engineers=None):
	return get_customer_contacts(request.customer) + list(affected_engineers or [])


# -- Shipment events ------------------------------------------------------


def resolve_shipment_created(shipment):
	return get_pool_engineers(shipment.customer) + get_leads_and_heads(shipment.customer)


def resolve_shipment_received(shipment):
	return get_customer_contacts(shipment.customer) + get_leads_and_heads(shipment.customer)


def resolve_shipment_closed(shipment):
	return get_customer_contacts(shipment.customer)
