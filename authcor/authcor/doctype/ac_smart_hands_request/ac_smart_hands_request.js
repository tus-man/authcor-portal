// Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
// For license information, please see license.txt

// (d) Cascading Country -> City -> Data Center, each restricted to the
// selected customer's AC Customer Service Area rows. The constraint lives
// in a child table on a different document, so it can't be expressed as a
// plain Link filter -- each field's allowed values are fetched from a
// whitelisted server method and cached on `frm`, and frm.set_query()
// reads that cache.

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
