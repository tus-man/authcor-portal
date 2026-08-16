// Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
// For license information, please see license.txt

// (d) Cascading Country -> City -> Data Center, each restricted to the
// selected customer's AC Customer Service Area rows. The constraint lives
// in a child table on a different document, so it can't be expressed as a
// plain Link filter -- each field's allowed values are fetched from a
// whitelisted server method and cached on `frm`, and frm.set_query()
// reads that cache.

// Section 3/4 assignment buttons. Visibility is UI convenience only -- the
// whitelisted methods re-check everything server-side (engineer profile,
// authorisation, capacity, roles), so a stale role list here just means a
// button that would fail with a clear server error, not a security gap.
const ENGINEER_ROLES = ["Operations L2 Authorizer", "Operations L3 Authorizer"];
const ASSIGNMENT_MANAGER_ROLES = [
	"Admin L1",
	"Admin L2",
	"Admin L3",
	"Operations L1 Authorizer",
	"Operations L2 Authorizer",
];

frappe.ui.form.on("AC Smart Hands Request", {
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
	},

	onload(frm) {
		refresh_allowed_countries(frm);
		refresh_allowed_cities(frm);
		refresh_allowed_data_centers(frm);
	},

	refresh(frm) {
		setup_assignment_buttons(frm);
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

function setup_assignment_buttons(frm) {
	if (frm.is_new()) return;

	const assigned = frm.doc.assigned_engineers || [];
	const has_room = assigned.length < frm.doc.engineers_required;
	const already_assigned = assigned.some((row) => row.engineer === frappe.session.user);

	if (has_room && !already_assigned && frappe.user.has_role(ENGINEER_ROLES)) {
		frm.add_custom_button(__("Claim Ticket"), () => claim_ticket(frm));
	}

	if (frappe.user.has_role(ASSIGNMENT_MANAGER_ROLES)) {
		frm.add_custom_button(__("Add Engineer"), () => add_engineer(frm));
		if (assigned.length) {
			frm.add_custom_button(__("Remove Engineer"), () => remove_engineer(frm));
		}
	}
}

function claim_ticket(frm) {
	frappe.call({
		method: "authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request.claim_ticket",
		args: { ticket: frm.doc.name },
		freeze: true,
		callback() {
			frm.reload_doc();
		},
	});
}

function add_engineer(frm) {
	frappe.prompt(
		[
			{
				fieldname: "engineer",
				fieldtype: "Link",
				options: "User",
				label: __("Engineer"),
				reqd: 1,
			},
		],
		(values) => {
			frappe.call({
				method: "authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request.add_engineer",
				args: { ticket: frm.doc.name, engineer: values.engineer },
				freeze: true,
				callback() {
					frm.reload_doc();
				},
			});
		},
		__("Add Engineer"),
		__("Add")
	);
}

function remove_engineer(frm) {
	const assigned = frm.doc.assigned_engineers || [];
	frappe.prompt(
		[
			{
				fieldname: "engineer",
				fieldtype: "Select",
				options: assigned.map((row) => row.engineer),
				label: __("Engineer"),
				reqd: 1,
			},
		],
		(values) => {
			frappe.call({
				method: "authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request.remove_engineer",
				args: { ticket: frm.doc.name, engineer: values.engineer },
				freeze: true,
				callback() {
					frm.reload_doc();
				},
			});
		},
		__("Remove Engineer"),
		__("Remove")
	);
}

function refresh_allowed_countries(frm) {
	frm.allowed_countries = [];
	if (!frm.doc.customer) return;

	frappe.call({
		method: "authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request.get_allowed_countries",
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
		args: { customer: frm.doc.customer, city: frm.doc.city },
		callback(r) {
			frm.allowed_data_centers = r.message || [];
		},
	});
}
