// Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
// For license information, please see license.txt

// PHASE-6-SHIPMENT.md section 2/6. Same actor set as the server's
// OPS_ADMIN_ROLES -- Client L1/L2 can read and create a shipment but never
// transition its status.
const SHIPMENT_MANAGER_ROLES = [
	"Operations L1 Authorizer",
	"Operations L2 Authorizer",
	"Operations L3 Authorizer",
	"Admin L1",
	"Admin L2",
	"Admin L3",
];

frappe.ui.form.on("AC Shipment Request", {
	setup(frm) {
		frm.set_query("country", () => ({
			filters: { name: ["in", frm.allowed_countries || []] },
		}));
		frm.set_query("city", () => ({
			filters: { name: ["in", frm.allowed_cities || []] },
		}));
		frm.set_query("data_center", () => ({
			filters: { name: ["in", frm.allowed_data_centers || []] },
		}));
		// PHASE-6-SHIPMENT.md section 2 cross-link: only offer tickets
		// belonging to the same customer. validate() enforces this
		// server-side too -- this is UI convenience only.
		frm.set_query("related_ticket", () => ({
			filters: { customer: frm.doc.customer },
		}));
	},

	onload(frm) {
		refresh_allowed_countries(frm);
		refresh_allowed_cities(frm);
		refresh_allowed_data_centers(frm);
	},

	refresh(frm) {
		setup_status_buttons(frm);
		setup_tracking_button(frm);
	},

	customer(frm) {
		frm.set_value("country", "");
		refresh_allowed_countries(frm);
	},

	country(frm) {
		frm.set_value("city", "");
		refresh_allowed_cities(frm);
	},

	city(frm) {
		frm.set_value("data_center", "");
		refresh_allowed_data_centers(frm);
	},
});

// Section 3. status is read_only on the DocType, so these buttons are the
// only way to move it -- each calls the matching whitelisted method, which
// re-checks role and gate requirements server-side regardless of what's
// shown here.
function setup_status_buttons(frm) {
	if (frm.is_new() || !frappe.user.has_role(SHIPMENT_MANAGER_ROLES)) return;

	if (frm.doc.status === "Open") {
		frm.add_custom_button(__("Mark Received"), () => call_status_action(frm, "mark_received"));
		frm.add_custom_button(__("Cancel Shipment"), () =>
			call_status_action(frm, "cancel_shipment")
		);
	} else if (frm.doc.status === "Received") {
		frm.add_custom_button(__("Close Shipment"), () =>
			call_status_action(frm, "close_shipment")
		);
		frm.add_custom_button(__("Cancel Shipment"), () =>
			call_status_action(frm, "cancel_shipment")
		);
	}
}

function call_status_action(frm, method) {
	frappe.call({
		method: `authcor.authcor.doctype.ac_shipment_request.ac_shipment_request.${method}`,
		args: { shipment: frm.doc.name },
		freeze: true,
		callback() {
			frm.reload_doc();
		},
	});
}

// Section 5. One button, built from the same server-side map
// (CARRIER_TRACKING_URL_TEMPLATES) so there's only ever one place that
// knows the URL patterns.
function setup_tracking_button(frm) {
	if (frm.is_new() || !frm.doc.tracking_number) return;

	frappe.call({
		method: "authcor.authcor.doctype.ac_shipment_request.ac_shipment_request.get_tracking_url",
		type: "GET",
		args: { carrier: frm.doc.carrier, tracking_number: frm.doc.tracking_number },
		callback(r) {
			if (r.message) {
				frm.add_custom_button(__("Track Shipment"), () =>
					window.open(r.message, "_blank")
				);
			}
		},
	});
}

function refresh_allowed_countries(frm) {
	frm.allowed_countries = [];
	if (!frm.doc.customer) return;

	frappe.call({
		method: "authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request.get_allowed_countries",
		type: "GET",
		args: { customer: frm.doc.customer },
		callback(r) {
			frm.allowed_countries = r.message || [];
		},
	});
}

function refresh_allowed_cities(frm) {
	frm.allowed_cities = [];
	if (!frm.doc.customer || !frm.doc.country) return;

	frappe.call({
		method: "authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request.get_allowed_cities",
		type: "GET",
		args: { customer: frm.doc.customer, country: frm.doc.country },
		callback(r) {
			frm.allowed_cities = r.message || [];
		},
	});
}

function refresh_allowed_data_centers(frm) {
	frm.allowed_data_centers = [];
	if (!frm.doc.customer || !frm.doc.city) return;

	frappe.call({
		method: "authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request.get_allowed_data_centers",
		type: "GET",
		args: { customer: frm.doc.customer, city: frm.doc.city },
		callback(r) {
			frm.allowed_data_centers = r.message || [];
		},
	});
}
