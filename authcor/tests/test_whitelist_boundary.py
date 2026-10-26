"""Permlevel at the whitelisted-method boundary.

A whitelisted method's return value is serialised with no permlevel
filtering -- json_handler reaches for Document.__json__, which is
as_dict(no_nulls=True), and as_dict knows nothing about permlevel. So
every method that hands back a document has to filter for itself.

Two classes, because there are two ways this breaks. TestReadableResponse
exercises the filtering itself against the real schema. TestWhitelistReturn
Shapes is the generic one: rather than asserting twelve times that twelve
known methods strip, it reads every @frappe.whitelist() function in the
app and requires each of its returns to be either readable_response() or an
expression that cannot carry a field at all. A thirteenth method written
later is covered the moment it is added -- and fails until someone has
decided which of the two it is, which is the point. The failure is at
worst a false positive on a safe return shape, and the fix for that is one
justified line in DOCUMENT_FREE_CALLS.
"""

import ast
import os

import frappe
from frappe.model import float_like_fields
from frappe.tests import IntegrationTestCase

from authcor.permissions import readable_response

BOUNDARY = "readable_response"

# Fieldtypes get_valid_dict() casts to a concrete value before it drops
# nulls, so a field stripped by delattr still serialises -- as 0, 0.0 or ""
# rather than being absent (frappe/model/base_document.py). The reason
# readable_response has to filter the serialised payload and not just the
# document.
CAST_TO_NON_NULL_FIELDTYPES = {"Int", "Check", "Read Only"} | float_like_fields

# Call targets whose return value provably cannot be a Document, each with
# the reason it is here. Keep this list short: every entry is a promise
# that the scan can no longer check.
DOCUMENT_FREE_CALLS = {
	"frappe.get_all": "query result -- plain dicts and scalars, never a Document",
	"frappe.get_list": "as frappe.get_all, with permissions applied",
	"list": "a plain list built from a set or comprehension",
	"build_tracking_url": "ac_shipment_request helper -- returns a URL string or None",
}

# Return shapes that carry no document because they are built on the spot.
SAFE_LITERAL_NODES = (
	ast.Constant,
	ast.Dict,
	ast.List,
	ast.Set,
	ast.Tuple,
	ast.ListComp,
	ast.DictComp,
	ast.SetComp,
	ast.GeneratorExp,
	ast.JoinedStr,
)

# Methods the scan must find, or it is scanning the wrong thing. Not the
# full list -- that would have to be updated on every new method, which is
# exactly the maintenance this test exists to avoid -- just enough that an
# empty or misdirected scan cannot pass.
EXPECTED_WHITELISTED = {
	"claim_ticket",
	"create_portal_ticket",
	"increase_engineer_count",
	"mark_received",
	"submit_claim",
	"withdraw_count_request",
}


def _dotted_name(node):
	"""`frappe.get_all` for an Attribute chain, `list` for a Name, None for
	anything else (a call on a subscript or a call result)."""
	parts = []
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		parts.append(node.id)
		return ".".join(reversed(parts))
	return None


def _is_whitelisted(fn):
	for decorator in fn.decorator_list:
		target = decorator.func if isinstance(decorator, ast.Call) else decorator
		if _dotted_name(target) == "frappe.whitelist":
			return True
	return False


def _own_returns(fn):
	"""The returns belonging to `fn` itself.

	Nested functions are skipped on purpose: the lock-retry helpers in this
	app take an inner closure (_attempt, _finalize) whose `return doc` is
	not a response -- it hands the document back to the outer function,
	which is where the boundary belongs and where this looks for it.
	"""
	found = []

	def walk(node):
		for child in ast.iter_child_nodes(node):
			if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
				continue
			if isinstance(child, ast.Return):
				found.append(child)
			walk(child)

	walk(fn)
	return found


def _classify_return(node):
	"""("boundary"|"safe"|"unchecked", description)."""
	value = node.value
	if value is None or isinstance(value, SAFE_LITERAL_NODES):
		return "safe", type(value).__name__
	if isinstance(value, ast.Call):
		name = _dotted_name(value.func)
		if name == BOUNDARY:
			return "boundary", name
		if name in DOCUMENT_FREE_CALLS:
			return "safe", name
		return "unchecked", f"{name or 'call'}(...)"
	return "unchecked", type(value).__name__


def _scan_whitelisted_returns(app_path):
	"""[(path, lineno, fn_name, verdict, description)] for every return of
	every whitelisted function in the app."""
	rows = []
	for root, dirs, files in os.walk(app_path):
		dirs[:] = [d for d in dirs if d not in {"__pycache__", "node_modules"}]
		for filename in sorted(files):
			if not filename.endswith(".py"):
				continue
			path = os.path.join(root, filename)
			with open(path, encoding="utf-8") as f:
				tree = ast.parse(f.read(), path)
			for node in ast.walk(tree):
				if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
					continue
				if not _is_whitelisted(node):
					continue
				returns = _own_returns(node)
				if not returns:
					# Returns None implicitly -- nothing to serialise.
					rows.append((path, node.lineno, node.name, "safe", "implicit None"))
					continue
				for ret in returns:
					verdict, description = _classify_return(ret)
					rows.append((path, ret.lineno, node.name, verdict, description))
	return rows


class TestWhitelistReturnShapes(IntegrationTestCase):
	def setUp(self):
		self.app_path = frappe.get_app_path("authcor")
		self.rows = _scan_whitelisted_returns(self.app_path)

	def test_the_scan_found_the_whitelisted_methods(self):
		"""A scan of nothing must not pass."""
		found = {name for _, _, name, _, _ in self.rows}
		self.assertTrue(found, msg=f"no whitelisted methods found under {self.app_path}")
		missing = EXPECTED_WHITELISTED - found
		self.assertFalse(missing, msg=f"scan missed known whitelisted methods: {sorted(missing)}")

	def test_every_whitelisted_return_is_filtered_or_cannot_carry_a_field(self):
		offenders = [
			f"{os.path.relpath(path, self.app_path)}:{lineno} {name}() returns {description}"
			for path, lineno, name, verdict, description in self.rows
			if verdict == "unchecked"
		]
		self.assertFalse(
			offenders,
			msg=(
				"A whitelisted method's return value is serialised without permlevel "
				"filtering. Wrap the return in readable_response() (authcor/permissions.py), "
				"or -- if it provably cannot carry a document field -- add the call to "
				"DOCUMENT_FREE_CALLS with the reason. Offending returns:\n  "
				+ "\n  ".join(offenders)
			),
		)


def _placeholder(df):
	"""A non-null value of roughly the right shape. Nothing is saved, so it
	only has to be distinguishable from absent."""
	if df.fieldtype == "Datetime":
		return "2026-01-01 00:00:00"
	if df.fieldtype == "Date":
		return "2026-01-01"
	if df.fieldtype == "Time":
		return "09:00:00"
	if df.fieldtype == "Check":
		return 1
	if df.fieldtype in {"Int"} | float_like_fields:
		return 5
	return "X"


def _restricted_fields(doctype):
	return [df for df in frappe.get_meta(doctype).fields if (df.permlevel or 0) > 0]


def _restricted_child_fields(doctype):
	"""{table_fieldname: [df, ...]} -- restricted fields on `doctype`'s child
	tables.

	A child field's permlevel is evaluated against the *parent's*
	permissions (Document.get_permissions switches to parenttype for a
	table), so a child row is only reachable, and only strippable, through
	its parent -- which is why these are tested through the parent rather
	than by instantiating the child doctype on its own. No Authcor child
	table has a restricted field today, so this contributes nothing yet and
	starts contributing the day one does.
	"""
	found = {}
	for table_field in frappe.get_meta(doctype).get_table_fields(include_computed=True):
		restricted = _restricted_fields(table_field.options)
		if restricted:
			found[table_field.fieldname] = restricted
	return found


class TestReadableResponse(IntegrationTestCase):
	"""readable_response() against the real schema, for every Authcor doctype
	that has a permlevel-restricted field -- derived from frappe.get_meta
	rather than listed, so a field moved to permlevel 1 in a later phase is
	covered without touching this file.

	Asserts on key membership, not hasattr: the return value is a
	frappe._dict, whose __getattr__ answers None for any missing key rather
	than raising, so hasattr() is True for everything and would pass
	vacuously.

	Runs as Guest purely because it is the identity with no role that holds
	read at permlevel 1 on anything, which is what makes the stripping
	observable; Administrator short-circuits apply_fieldlevel_read_
	permissions entirely. Nothing is inserted -- these documents only ever
	exist in memory, so there is nothing to commit or clean up.
	"""

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _doctypes_with_restricted_fields(self):
		"""{doctype: [df, ...]} for the app's own restricted fields.

		istable: 0 because a child doctype cannot be filtered on its own --
		apply_fieldlevel_read_permissions resolves a table's permissions
		through self.parenttype, which is unset on a standalone child
		document. Child fields are covered through their parent instead;
		see _restricted_child_fields.
		"""
		found = {}
		for doctype in frappe.get_all(
			"DocType", filters={"module": "Authcor", "istable": 0}, pluck="name"
		):
			restricted = _restricted_fields(doctype)
			if restricted:
				found[doctype] = restricted
		return found

	def _filled_doc(self, doctype, restricted):
		"""An in-memory document with every restricted field set, on the
		parent and on any child row."""
		doc = frappe.get_doc({"doctype": doctype})
		for df in restricted:
			doc.set(df.fieldname, _placeholder(df))
		for fieldname, child_dfs in _restricted_child_fields(doctype).items():
			row = doc.append(fieldname, {})
			for df in child_dfs:
				row.set(df.fieldname, _placeholder(df))
		return doc

	def test_schema_has_restricted_fields_to_check(self):
		self.assertTrue(
			self._doctypes_with_restricted_fields(),
			msg="no permlevel-restricted fields in the schema -- this suite would pass vacuously",
		)

	def test_restricted_fields_are_absent_from_the_payload(self):
		"""Absent, not null and not zero. The payload *is* the wire format
		-- __json__ is as_dict(no_nulls=True), which is what this returns --
		so a key missing here is a key the client never sees."""
		for doctype, restricted in self._doctypes_with_restricted_fields().items():
			with self.subTest(doctype=doctype):
				doc = self._filled_doc(doctype, restricted)

				frappe.set_user("Guest")
				payload = readable_response(doc)

				for df in restricted:
					self.assertNotIn(
						df.fieldname,
						payload,
						msg=(
							f"{doctype}.{df.fieldname} (permlevel {df.permlevel}, "
							f"{df.fieldtype}) reached the response as "
							f"{payload.get(df.fieldname)!r}"
						),
					)
				for fieldname, child_dfs in _restricted_child_fields(doctype).items():
					for row_payload in payload.get(fieldname) or []:
						for df in child_dfs:
							self.assertNotIn(
								df.fieldname,
								row_payload,
								msg=(
									f"{doctype}.{fieldname}[].{df.fieldname} "
									f"(permlevel {df.permlevel}, {df.fieldtype}) reached "
									f"the response as {row_payload.get(df.fieldname)!r}"
								),
							)

	def test_restricted_numeric_fields_are_absent_rather_than_zero(self):
		"""The regression this boundary exists to close.

		get_valid_dict() casts a missing field back to a concrete value
		before it drops nulls, so deleting the attribute is not enough: an
		absent Int or Check serialises as 0, Float/Currency/Percent as 0.0
		and Read Only as "". None of those is null, so no_nulls keeps them,
		and the field name plus a plausible value reaches the client. No
		real value leaks while the stripped value is always zero, but a
		restricted numeric field with a meaningful default would, and the
		field name is itself information.

		Asserts the fieldtypes are actually present in the schema first, so
		this cannot start passing because the fields moved.
		"""
		doctype = "AC Smart Hands Request"
		restricted = _restricted_fields(doctype)
		affected = [df for df in restricted if df.fieldtype in CAST_TO_NON_NULL_FIELDTYPES]
		self.assertTrue(
			affected,
			msg=(
				f"no restricted {sorted(CAST_TO_NON_NULL_FIELDTYPES)} field on {doctype} -- "
				"this test no longer covers the cast it was written for"
			),
		)

		doc = self._filled_doc(doctype, restricted)
		frappe.set_user("Guest")
		payload = readable_response(doc)

		for df in affected:
			self.assertNotIn(
				df.fieldname,
				payload,
				msg=(
					f"{doctype}.{df.fieldname} ({df.fieldtype}) serialised as "
					f"{payload.get(df.fieldname)!r} instead of being absent"
				),
			)

	def test_the_original_is_not_mutated(self):
		"""apply_fieldlevel_read_permissions() strips by delattr, so calling
		it on the document itself would strip it out from under an internal
		caller -- approve_count_request() reuses remove_engineer() in
		exactly that shape. The copy is what makes that safe."""
		for doctype, restricted in self._doctypes_with_restricted_fields().items():
			with self.subTest(doctype=doctype):
				doc = self._filled_doc(doctype, restricted)

				frappe.set_user("Guest")
				readable_response(doc)

				for df in restricted:
					self.assertEqual(
						doc.get(df.fieldname),
						_placeholder(df),
						msg=f"readable_response mutated the caller's {doctype}.{df.fieldname}",
					)

	def test_unrestricted_fields_survive(self):
		"""The filtering has to be narrower than "hide everything" -- the
		response is still the response."""
		doctype = "AC Smart Hands Request"
		doc = frappe.get_doc({"doctype": doctype, "subject": "Kept", "status": "Open"})
		for df in _restricted_fields(doctype):
			doc.set(df.fieldname, _placeholder(df))

		frappe.set_user("Guest")
		payload = readable_response(doc)

		self.assertEqual(payload.get("subject"), "Kept")
		self.assertEqual(payload.get("status"), "Open")
		self.assertEqual(payload.get("doctype"), doctype)
