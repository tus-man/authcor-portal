// Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
// For license information, please see license.txt

// PHASE-3B-ENGINEER-COUNT.md section 5/8. Visibility here is UI convenience
// only -- approve_count_request/reject_count_request re-check the role and
// the request/ticket state server-side, so a stale role list just means a
// button that would fail with a clear server error, not a security gap.
const DECISION_ROLES = ["Admin L1", "Admin L2", "Admin L3", "Operations L1 Authorizer"];

frappe.ui.form.on("AC Engineer Count Request", {
	refresh(frm) {
		setup_decision_buttons(frm);
	},
});

function setup_decision_buttons(frm) {
	if (frm.is_new() || frm.doc.status !== "Pending") return;
	if (!frappe.user.has_role(DECISION_ROLES)) return;

	frm.add_custom_button(__("Approve"), () => approve_request(frm));
	frm.add_custom_button(__("Reject"), () => reject_request(frm));
}

function reject_request(frm) {
	frappe.prompt(
		[
			{
				fieldname: "note",
				fieldtype: "Small Text",
				label: __("Decision Note"),
			},
		],
		(values) => {
			frappe.call({
				method: "authcor.authcor.doctype.ac_engineer_count_request.ac_engineer_count_request.reject_count_request",
				args: { request: frm.doc.name, note: values.note },
				freeze: true,
				callback() {
					frm.reload_doc();
				},
			});
		},
		__("Reject Engineer Count Request"),
		__("Reject")
	);
}

// The multi-select is constrained to exactly the number that must be
// removed (assigned count - requested count, floored at 0) -- fetched from
// the ticket itself, not from current_count on this request, since
// assignments can have moved since the request was raised.
// approve_count_request re-checks the same count server-side regardless of
// what's selected here. When assigned count is already at or below the
// requested count -- the customer reduced before anyone (enough) claimed --
// there's nothing to remove, so the multi-select is skipped entirely and
// approval just updates the count.
function approve_request(frm) {
	frappe.model.with_doc("AC Smart Hands Request", frm.doc.ticket).then((ticket) => {
		if (!ticket) {
			frappe.msgprint(__("Could not load ticket {0}.", [frm.doc.ticket]));
			return;
		}

		const assigned = (ticket.assigned_engineers || []).map((row) => row.engineer);
		const required_removals = Math.max(0, assigned.length - frm.doc.requested_count);

		const fields = [];
		if (required_removals > 0) {
			fields.push({
				fieldname: "engineers_to_remove",
				fieldtype: "MultiCheck",
				label: __("Engineers to remove (select exactly {0})", [required_removals]),
				options: assigned.map((engineer) => ({ label: engineer, value: engineer })),
				columns: 1,
			});
		}
		fields.push({
			fieldname: "note",
			fieldtype: "Small Text",
			label: __("Decision Note"),
		});

		const dialog = new frappe.ui.Dialog({
			title: __("Approve Engineer Count Request"),
			fields: fields,
			primary_action_label: __("Approve"),
			primary_action: (values) => {
				const selected = required_removals > 0 ? dialog.get_value("engineers_to_remove") || [] : [];
				if (selected.length !== required_removals) {
					frappe.msgprint(__("Select exactly {0} engineer(s) to remove.", [required_removals]));
					return;
				}

				frappe.call({
					method: "authcor.authcor.doctype.ac_engineer_count_request.ac_engineer_count_request.approve_count_request",
					args: {
						request: frm.doc.name,
						engineers_to_remove: selected,
						note: values.note,
					},
					freeze: true,
					callback() {
						dialog.hide();
						frm.reload_doc();
					},
				});
			},
		});

		dialog.show();
	});
}
