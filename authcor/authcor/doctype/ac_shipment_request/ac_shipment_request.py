# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

from urllib.parse import quote

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	SCOPED_TICKET_ROLES,
	UNRESTRICTED_TICKET_ROLES,
)
from authcor.notifications import notify
from authcor.notifications.context import (
	shipment_closed_context,
	shipment_created_context,
	shipment_received_context,
)
from authcor.notifications.recipients import (
	resolve_shipment_closed,
	resolve_shipment_created,
	resolve_shipment_received,
)
from authcor.permissions import readable_response

# Section 5. One small map in code, not on the DocType -- these change
# occasionally and a code change is easier to review than a data edit.
# No API, no cost, no integration (v1): a plain deep link straight to the
# carrier's own tracking page. "Others" has no single tracking site to
# link to, so it's deliberately absent here.
CARRIER_TRACKING_URL_TEMPLATES = {
	"DHL": "https://www.dhl.com/en/express/tracking.html?AWB={tracking_number}",
	"FedEx": "https://www.fedex.com/fedextrack/?trknbr={tracking_number}",
	"UPS": "https://www.ups.com/track?tracknum={tracking_number}",
	"TNT": "https://www.tnt.com/express/en_us/site/shipping-tools/tracking.html?searchType=CON&cons={tracking_number}",
}


def build_tracking_url(carrier, tracking_number):
	"""Section 5. None when there's no tracking number yet -- it's optional,
	since a customer arranging a shipment may not have one -- or when the
	carrier has no known tracking URL template (Others)."""
	if not tracking_number:
		return None
	template = CARRIER_TRACKING_URL_TEMPLATES.get(carrier)
	if not template:
		return None
	return template.format(tracking_number=quote(tracking_number, safe=""))


@frappe.whitelist()
def get_tracking_url(carrier, tracking_number):
	return build_tracking_url(carrier, tracking_number)


# Section 3: who may transition status. Exactly the union of
# ac_smart_hands_request's own UNRESTRICTED_TICKET_ROLES and
# SCOPED_TICKET_ROLES -- "Ops, Admin" in the lifecycle table is precisely
# every Operations/Admin role that DocType already defines, so this reuses
# the existing sets rather than re-listing the same role names a third
# time.
OPS_ADMIN_ROLES = UNRESTRICTED_TICKET_ROLES | SCOPED_TICKET_ROLES


def _ensure_can_manage_shipment():
	"""status is read_only in the DocType (like completed_by/actual_onsite_at
	on the ticket), so the whitelisted methods below are the only way to
	move it -- and since they save with ignore_permissions=True, this check
	is what actually keeps a Client L1/L2 caller (read + create only, per
	PHASE-6-SHIPMENT.md section 6) from hitting one of these methods
	directly over the API and forcing a transition anyway."""
	if not set(frappe.get_roles()) & OPS_ADMIN_ROLES:
		frappe.throw(_("You are not permitted to update this shipment's status."), frappe.PermissionError)


@frappe.whitelist()
def mark_received(shipment):
	"""Section 3: Open -> Received. Stamps actual_delivery/received_by/
	stored_on and requires receipt proof -- all enforced in validate() so
	this whitelisted entry point can't drift from any other write path
	that sets status directly."""
	_ensure_can_manage_shipment()
	doc = frappe.get_doc("AC Shipment Request", shipment)
	if doc.status != "Open":
		frappe.throw(_("Only an Open shipment can be marked Received."))
	doc.status = "Received"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return readable_response(doc)


@frappe.whitelist()
def close_shipment(shipment):
	"""Section 3: Received -> Closed. storage_location-if-warehoused is
	enforced in validate()."""
	_ensure_can_manage_shipment()
	doc = frappe.get_doc("AC Shipment Request", shipment)
	if doc.status != "Received":
		frappe.throw(_("Only a Received shipment can be Closed."))
	doc.status = "Closed"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return readable_response(doc)


@frappe.whitelist()
def cancel_shipment(shipment):
	"""Section 3: Cancelled is reachable any time before Closed."""
	_ensure_can_manage_shipment()
	doc = frappe.get_doc("AC Shipment Request", shipment)
	if doc.status == "Closed":
		frappe.throw(_("A Closed shipment cannot be Cancelled."))
	doc.status = "Cancelled"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return readable_response(doc)


class ACShipmentRequest(Document):
	def before_insert(self):
		"""The DocType has no default on `status` (unlike, say, a Select
		field with a Desk-set default) -- PHASE-6-SHIPMENT.md section 2
		calls for defaulting to Open, so it's enforced here instead, same
		convention as AC Expense Claim's before_insert defaulting status to
		Draft."""
		if not self.status:
			self.status = "Open"

	def after_insert(self):
		"""PHASE-5-NOTIFICATIONS.md section 3: shipment_created."""
		notify(
			event_key="shipment_created",
			reference_doctype="AC Shipment Request",
			reference_name=self.name,
			recipients=resolve_shipment_created(self),
			context=shipment_created_context(self),
			subject=_("[{0}] New shipment -- {1}").format(self.name, self.subject),
		)

	def on_update(self):
		"""PHASE-5-NOTIFICATIONS.md section 3: shipment_received/
		shipment_closed. Same is_new()/get_doc_before_save() guard as the
		ticket's own on_update -- nothing to compare a brand-new document
		against, and after_insert above already covers creation."""
		if self.is_new():
			return
		if self.get_doc_before_save() is None:
			return

		if self.has_value_changed("status") and self.status == "Received":
			notify(
				event_key="shipment_received",
				reference_doctype="AC Shipment Request",
				reference_name=self.name,
				recipients=resolve_shipment_received(self),
				context=shipment_received_context(self),
				subject=_("[{0}] Shipment received -- {1}").format(self.name, self.subject),
			)

		if self.has_value_changed("status") and self.status == "Closed":
			notify(
				event_key="shipment_closed",
				reference_doctype="AC Shipment Request",
				reference_name=self.name,
				recipients=resolve_shipment_closed(self),
				context=shipment_closed_context(self),
				subject=_("[{0}] Shipment closed -- {1}").format(self.name, self.subject),
			)

	def validate(self):
		self.validate_carrier_other()
		self.validate_related_ticket()
		self.validate_received_requirements()
		self.validate_closure_requirements()
		self.validate_cancellation()
		self.stamp_receipt()

	def validate_carrier_other(self):
		"""Depends On / Mandatory Depends On on carrier_other are client-side
		only and don't apply to API writes -- enforce the same rule here,
		same pattern as the ticket's validate_action_other_details."""
		if self.carrier == "Others" and not self.carrier_other:
			frappe.throw(_("Carrier Name is required when Carrier is Others."))

	def validate_related_ticket(self):
		"""The client script's set_query restricts the dropdown to
		matching-customer tickets, but that's UI-only -- enforce the same
		rule server-side so an API write can't cross-link a shipment to
		another customer's ticket (PHASE-6-SHIPMENT.md section 2)."""
		if not self.related_ticket:
			return
		ticket_customer = frappe.db.get_value("AC Smart Hands Request", self.related_ticket, "customer")
		if ticket_customer != self.customer:
			frappe.throw(_("{0} does not belong to this shipment's customer.").format(self.related_ticket))

	def validate_received_requirements(self):
		"""Section 3: entering Received requires at least one receipt proof
		image."""
		if self.status == "Received" and not self.receipt_proof:
			frappe.throw(
				_("At least one receipt proof image is required before this shipment can be marked Received.")
			)

	def validate_closure_requirements(self):
		"""Section 3: entering Closed requires storage_location, but only if
		store_in_warehouse is ticked -- warehouse storage is an attribute of
		what happened, not a separate stage, so an un-warehoused shipment
		closes with no location at all."""
		if self.status == "Closed" and self.store_in_warehouse and not self.storage_location:
			frappe.throw(
				_(
					"Storage Location is required for a shipment stored in the warehouse before it can be Closed."
				)
			)

	def validate_cancellation(self):
		"""Section 3: Cancelled is reachable any time before Closed -- not
		after."""
		if self.status != "Cancelled":
			return
		previous = self.get_doc_before_save()
		if previous and previous.status == "Closed":
			frappe.throw(_("A Closed shipment cannot be Cancelled."))

	def stamp_receipt(self):
		"""Section 3: actual_delivery stamps the first time status becomes
		Received -- same "only when currently empty" guard as the ticket's
		stamp_completion/stamp_onsite_arrival, so re-saving an
		already-Received shipment doesn't move the clock. received_by is
		editable, like completed_by on the ticket (whoever records receipt
		may not be who physically took the delivery), but still defaults to
		the recording user's engineer profile when nobody's set it
		explicitly. stored_on stamps alongside, but only when
		store_in_warehouse is ticked -- storage is an attribute of what
		happened at receipt, not a separate stage."""
		if self.status != "Received":
			return

		if not self.actual_delivery:
			self.actual_delivery = now_datetime()

		if not self.received_by:
			engineer_profile = frappe.db.get_value(
				"AC Engineer Profile", {"user": frappe.session.user}, "name"
			)
			if engineer_profile:
				self.received_by = engineer_profile

		if self.store_in_warehouse and not self.stored_on:
			self.stored_on = now_datetime()
