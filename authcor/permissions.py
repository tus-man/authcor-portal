"""Field-level read filtering at the whitelisted-method boundary.

A whitelisted method's return value is serialised into the response body
with no permlevel filtering of any kind: frappe.utils.response.json_handler
reaches for Document.__json__, which is as_dict(no_nulls=True), and as_dict
knows nothing about permlevel. Permlevel protects Desk rendering and the
report API -- it does not protect a method return. So a method that hands
back a document has to filter for itself, and readable_response() is the
one way this app does it (authcor/tests/test_whitelist_boundary.py
enforces that).
"""

import frappe


def readable_response(doc):
	"""`doc` as a response payload, filtered to what the caller may read.

	Returns a plain dict -- frappe._dict, so attribute access still works
	-- rather than a Document, for two reasons.

	The first is that a Document cannot be filtered reliably.
	apply_fieldlevel_read_permissions() strips a field by deleting the
	attribute, but get_valid_dict() casts a missing field back to a
	concrete value *before* it drops nulls (frappe/model/base_document.py:
	the Int/Check/float/Read Only casts sit above the `ignore_nulls`
	check), so an absent Int or Check serialises as 0, Float/Currency/
	Percent as 0.0, Read Only as "", and any not_nullable field as its
	default. The field name and a plausible-looking value for it reach the
	client anyway; only Data/Link/Select/Text/Datetime fields actually
	disappear. Filtering therefore has to happen after serialisation,
	which means the boundary has to own the serialised form. Note that
	this is true of apply_fieldlevel_read_permissions() wherever it is
	called, not of the copy made here -- a document built from as_dict()
	starts with those columns at None, not 0.

	The second is that a stripped Document is a hazard in its own right:
	saving one writes those zeros back over the real permlevel 1 values.
	A dict cannot be saved by accident.

	A copy, deliberately, for the same shape of reason.
	apply_fieldlevel_read_permissions() mutates what it is handed, so
	calling it on a document an internal caller still holds would strip
	that caller's fields out from under it -- remove_engineer() is reused
	by approve_count_request() exactly that way, and reads result.name and
	result.subject itself when it notifies. Copying means the boundary can
	never reach back into the call it came from, and no future caller has
	to know that it might.

	A no-op for Administrator, and for anyone who holds read at the
	field's own permlevel -- which is what makes it safe to apply
	unconditionally. That includes the documents whose doctypes have no
	permlevel field at all today (AC Engineer Count Request, AC Expense
	Claim, AC Shipment Request): nothing to strip now, and nothing to
	remember later, when the first permlevel 1 field added to one of them
	would otherwise start leaking with no code change and nothing to
	notice it.
	"""
	readable = frappe.get_doc(doc.as_dict())
	readable.apply_fieldlevel_read_permissions()
	payload = readable.as_dict(no_nulls=True)

	# Read back what frappe actually removed rather than re-deriving the
	# rule: apply_fieldlevel_read_permissions() deletes the attribute, and
	# every valid column is present (at None) before it runs, so a
	# restricted field missing from __dict__ is exactly one the caller may
	# not read. Masking -- mask_fields(), called from the same method --
	# deliberately leaves a placeholder value in place, and is left alone.
	for df in readable.meta.fields:
		if df.permlevel and df.fieldname not in readable.__dict__:
			payload.pop(df.fieldname, None)

	# A child field's permlevel is evaluated against the parent, so child
	# rows are filtered by the same call and need the same treatment.
	for table_field in readable.meta.get_table_fields(include_computed=True):
		restricted = [
			df.fieldname for df in (frappe.get_meta(table_field.options).fields or []) if df.permlevel
		]
		if not restricted:
			continue
		rows = readable.get(table_field.fieldname) or []
		row_payloads = payload.get(table_field.fieldname) or []
		for row, row_payload in zip(rows, row_payloads, strict=False):
			for fieldname in restricted:
				if fieldname not in row.__dict__:
					row_payload.pop(fieldname, None)

	return payload
