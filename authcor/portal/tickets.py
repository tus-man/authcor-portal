# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

"""PHASE-7-PORTAL.md section 4 -- the ticket pages, and the whitelisted
methods behind their forms.

Two rules shape every function here.

**Nothing the browser posts picks a customer, a field, or a document.**
`create_portal_ticket` takes a declared parameter list and sets `customer`
itself; frappe.call drops anything the browser posts that is not on that
list. Every action re-derives the customer from the session and verifies
the ticket against it before touching the row.

**Nothing hands a whole document to a template or to an HTTP response.**
`build_portal_ticket_view` lists by hand every value the detail page
renders, the same discipline notifications.context.build_ticket_closed_
context applies to the closure email, and for the same reason: a field
added to the doctype in a later phase must not be able to arrive on a
customer-facing surface just by existing.
"""

import frappe
from frappe import _
from frappe.utils import (
	add_days,
	cint,
	cstr,
	flt,
	fmt_money,
	format_date,
	format_datetime,
	getdate,
	sanitize_html,
)

from authcor.authcor.doctype.ac_engineer_count_request.ac_engineer_count_request import (
	create_count_request,
	withdraw_count_request,
)
from authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request import (
	COUNT_CHANGE_BLOCKED_STATUSES,
	_run_with_lock_retry,
	get_allowed_cities,
	get_allowed_countries,
	get_allowed_data_centers,
	increase_engineer_count,
)
from authcor.notifications.context import build_ticket_closed_context
from authcor.permissions import readable_response
from authcor.portal import STATUS_BADGE_CLASS, get_portal_customer

TICKET = "AC Smart Hands Request"

PAGE_SIZE = 20

#: Statuses past the point where a customer can still change anything about
#: the visit itself.
FINAL_STATUSES = ("Completed", "Closed", "Cancelled")

#: Statuses where proof of work is shown -- section 4: "once completed".
PROOF_VISIBLE_STATUSES = ("Completed", "Closed")

SLOT_BANDS = ("band_morning", "band_afternoon", "band_overnight")

SLOT_BAND_LABELS = (
	("band_morning", "9 AM - 1 PM"),
	("band_afternoon", "1 PM - 6 PM"),
	("band_overnight", "6 PM - 9 AM"),
)

#: The grid the raise-a-ticket page pre-populates, and the most rows an
#: amendment may post. Seven is what the controller generates; the ceiling
#: is twice that so a customer offering a fortnight is not an error, while a
#: posted array of ten thousand rows is.
SLOT_DAYS = 7
MAX_SLOT_ROWS = 14

MAX_ATTACHMENTS = 10
MAX_DC_ACCESS_LENGTH = 140


# -- small shared helpers -------------------------------------------------


def _field_options(fieldname):
	options = frappe.get_meta(TICKET).get_field(fieldname).options or ""
	return [option for option in options.split("\n") if option]


def status_options():
	return _field_options("status")


def severity_options():
	return _field_options("severity")


def action_options():
	return _field_options("action_required")


def _require_choice(fieldname, value, default=None):
	value = cstr(value).strip() or default
	if value not in _field_options(fieldname):
		frappe.throw(
			_("{0} is not a valid {1}.").format(value, _(frappe.get_meta(TICKET).get_label(fieldname)))
		)
	return value


def _require_text(value, label):
	value = cstr(value).strip()
	if not value:
		frappe.throw(_("{0} is required.").format(label))
	return value


def method_path(fn):
	"""The dotted path a browser posts to, derived from the function rather
	than typed into a template. A rename then breaks the import here
	instead of silently producing a dead button."""
	return f"{fn.__module__}.{fn.__name__}"


def _fmt_date(value):
	return format_date(value) if value else ""


def _fmt_datetime(value):
	return format_datetime(value) if value else ""


def _ensure_owned_ticket(name, customer):
	"""Ownership, checked from the denormalised `customer` column before
	the document is loaded or locked -- so an action aimed at a name that
	is not this customer's never touches the row at all.

	A name that does not exist is refused the same way one belonging to
	someone else is, for the reason portal.get_owned_doc gives: a
	different answer for the two cases turns an action endpoint into an
	existence oracle over a sequential naming series. The isinstance guard
	matters too -- a JSON request body can put a dict where a name is
	expected, and frappe.db.get_value would read that as a filter."""
	denied = frappe.PermissionError(_("This ticket is not available to your account."))

	if not name or not isinstance(name, str):
		raise denied

	if frappe.db.get_value(TICKET, name, "customer") != customer.name:
		raise denied


def _ensure_owned_doc(doc, customer):
	"""Re-checked after the row is locked, against the loaded document
	rather than the pre-lock read."""
	if doc.customer != customer.name:
		raise frappe.PermissionError(_("This ticket is not available to your account."))


# -- the preferred-slot grid ----------------------------------------------


def default_slot_grid():
	"""Section 4: the grid is pre-populated with the next seven days, all
	three bands ticked, exactly as the Desk form generates them.

	Same window as ACSmartHandsRequest.generate_preferred_slots -- starting
	the day after creation -- so the form a customer fills in and the rows
	a ticket gets when the form posts nothing are the same seven days.
	Authcor's reasoning stands: a customer who never sees the grid would be
	offering availability they never chose."""
	start = add_days(getdate(), 1)
	return [
		{
			"slot_date": str(add_days(start, offset)),
			"band_morning": 1,
			"band_afternoon": 1,
			"band_overnight": 1,
		}
		for offset in range(SLOT_DAYS)
	]


def slot_grid_for(doc):
	"""The ticket's current grid, shaped for the amendment form."""
	return [
		{
			"slot_date": str(getdate(row.slot_date)),
			"slot_label": _fmt_date(row.slot_date),
			"band_morning": cint(row.band_morning),
			"band_afternoon": cint(row.band_afternoon),
			"band_overnight": cint(row.band_overnight),
		}
		for row in doc.preferred_slots
	]


def parse_slot_grid(raw, required=False):
	"""Turn a posted grid into AC Preferred Slot rows, or throw.

	Returns an empty list when nothing was posted and nothing was required,
	which leaves before_insert's own seven-day generation to run."""
	if raw is None or raw == "" or raw == []:
		if required:
			frappe.throw(_("Choose at least one date and time band."))
		return []

	rows = frappe.parse_json(raw) if isinstance(raw, str) else raw
	if not isinstance(rows, list) or not rows:
		frappe.throw(_("The preferred slots were not sent in a form this page understands."))
	if len(rows) > MAX_SLOT_ROWS:
		frappe.throw(_("Offer at most {0} dates.").format(MAX_SLOT_ROWS))

	today = getdate()
	parsed = []
	seen = set()

	for row in rows:
		if not isinstance(row, dict) or not row.get("slot_date"):
			frappe.throw(_("Every preferred slot needs a date."))
		try:
			slot_date = getdate(row["slot_date"])
		except Exception:
			frappe.throw(_("{0} is not a date.").format(cstr(row["slot_date"])))
		if slot_date < today:
			frappe.throw(_("{0} is in the past.").format(_fmt_date(slot_date)))
		if slot_date in seen:
			frappe.throw(_("{0} was offered twice.").format(_fmt_date(slot_date)))
		seen.add(slot_date)
		parsed.append(
			{"slot_date": slot_date, **{band: 1 if cint(row.get(band)) else 0 for band in SLOT_BANDS}}
		)

	if not any(any(row[band] for band in SLOT_BANDS) for row in parsed):
		frappe.throw(_("Tick at least one time band on at least one date."))

	parsed.sort(key=lambda row: row["slot_date"])
	return parsed


# -- the list page --------------------------------------------------------


def list_portal_tickets(customer, status=None, page=1, page_size=PAGE_SIZE):
	"""Section 4: newest first, paginated, filterable by status.

	`customer` is passed explicitly on top of frappe.get_list's own User
	Permission enforcement. get_list would scope this anyway -- the filter
	is the second mechanism, written here so the scoping is visible in the
	query rather than only in configuration.

	Pages are detected by asking for one row more than fits rather than by
	a second count query."""
	page = max(1, cint(page) or 1)
	page_size = max(1, cint(page_size) or PAGE_SIZE)

	status = cstr(status).strip() or None
	if status and status not in status_options():
		# An unrecognised status is dropped rather than queried: the filter
		# comes off the query string, and the only honest answer to a
		# status that does not exist is the unfiltered list.
		status = None

	filters = {"customer": customer.name}
	if status:
		filters["status"] = status

	rows = frappe.get_list(
		TICKET,
		filters=filters,
		fields=["name", "subject", "data_center", "severity", "status", "creation"],
		order_by="creation desc",
		limit_start=(page - 1) * page_size,
		limit_page_length=page_size + 1,
	)

	has_next = len(rows) > page_size

	return {
		"rows": [
			{
				"name": row.name,
				"subject": row.subject,
				"data_center": row.data_center,
				"severity": row.severity,
				"status": row.status,
				"status_class": STATUS_BADGE_CLASS.get(row.status, "grey"),
				"created": _fmt_datetime(row.creation),
			}
			for row in rows[:page_size]
		],
		"status": status,
		"status_options": status_options(),
		"page": page,
		"has_next": has_next,
		"has_prev": page > 1,
	}


# -- the raise-a-ticket page ----------------------------------------------


def get_service_area_tree(customer):
	"""Country -> city -> data centre, limited to this customer's service
	areas, for the cascading selects.

	Built from the three existing whitelisted helpers rather than a fresh
	query, so the "a service-area row with no data centre authorises the
	whole city" rule lives in exactly one place. Rendered into the page as
	data: the cascade then needs no round trip, which is also why no
	request on this page ever carries a customer."""
	tree = []
	for country in sorted(filter(None, get_allowed_countries(customer.name))):
		cities = []
		for city in sorted(filter(None, get_allowed_cities(customer.name, country))):
			data_centers = sorted(filter(None, get_allowed_data_centers(customer.name, city)))
			if data_centers:
				cities.append({"name": city, "data_centers": data_centers})
		if cities:
			tree.append({"name": country, "cities": cities})
	return tree


def _validate_service_area(customer, country, city, data_center):
	"""Section 7: a data centre outside the customer's service areas is
	rejected. Checked at every level, because a valid city under the wrong
	country, or a valid data centre under the wrong city, is just as much
	a mismatch as an unknown name."""
	if country not in get_allowed_countries(customer.name):
		frappe.throw(_("{0} is not one of your service areas.").format(country))
	if city not in get_allowed_cities(customer.name, country):
		frappe.throw(_("{0} is not one of your service areas.").format(city))
	if data_center not in get_allowed_data_centers(customer.name, city):
		frappe.throw(_("{0} is not one of your service areas.").format(data_center))


def _attach_uploaded_files(doc, attachments):
	"""Link files the browser uploaded to the new ticket.

	The doctype's own `attachments` field is a single Attach, and section 4
	asks for several files, so portal uploads land as ordinary Frappe
	document attachments -- File rows pointing at the ticket, which is what
	the Desk sidebar already reads. No schema change.

	The browser uploads through frappe's own upload_file with no
	doctype/docname, which needs nothing beyond the File create grant every
	signed-in user has, and sends the File names here. Re-pointing them at
	the ticket happens server-side and only for files this very session
	uploaded and has not already attached to something: a File name from
	anywhere else is refused rather than quietly linked, which is what
	stops this from being a way to read another document's attachment by
	adopting it.

	frappe.db.set_value rather than a File save on purpose -- File.validate
	re-checks write permission on the target document, and a client role
	has read only."""
	if not attachments:
		return

	names = attachments
	if isinstance(names, str):
		names = frappe.parse_json(names) if names.strip().startswith("[") else [names]
	if not isinstance(names, list):
		frappe.throw(_("The attachments were not sent in a form this page understands."))
	if len(names) > MAX_ATTACHMENTS:
		frappe.throw(_("Attach at most {0} files.").format(MAX_ATTACHMENTS))

	for name in names:
		if not isinstance(name, str) or not name:
			frappe.throw(_("The attachments were not sent in a form this page understands."))

		row = frappe.db.get_value(
			"File",
			name,
			["owner", "attached_to_doctype", "attached_to_name", "is_folder"],
			as_dict=True,
		)
		if not row or row.is_folder:
			frappe.throw(_("That upload could not be found. Try attaching the file again."))
		if row.owner != frappe.session.user:
			raise frappe.PermissionError(_("That upload does not belong to your account."))
		if row.attached_to_doctype or row.attached_to_name:
			frappe.throw(_("That upload is already attached to something else."))

		frappe.db.set_value(
			"File",
			name,
			{"attached_to_doctype": doc.doctype, "attached_to_name": doc.name},
			update_modified=False,
		)


@frappe.whitelist(methods=["POST"])
def create_portal_ticket(
	country=None,
	city=None,
	data_center=None,
	subject=None,
	description=None,
	severity=None,
	action_required=None,
	action_other_details=None,
	target_equipment=None,
	engineers_required=None,
	external_reference=None,
	preferred_slots=None,
	attachments=None,
):
	"""Section 4/6: build the document server-side from a declared
	parameter list.

	This signature *is* the field list. frappe.call hands a whitelisted
	method only the arguments it declares, so a browser posting `customer`,
	`status`, or any of the eleven performance fields is posting into
	nothing -- there is no **kwargs here to catch them, deliberately.
	`customer` comes from the session and from nowhere else.

	Inserted without ignore_permissions: who may raise a ticket is a role
	question the Role Permissions Manager already answers (Client L2
	raises, Client L1 views), and bypassing it here would quietly overrule
	that. before_insert's seven-day slot generation and the controller's
	own validation run exactly as they do for a Desk insert."""
	customer = get_portal_customer()

	country = _require_text(country, _("Country"))
	city = _require_text(city, _("City"))
	data_center = _require_text(data_center, _("Data Center"))
	_validate_service_area(customer, country, city, data_center)

	doc = frappe.new_doc(TICKET)
	doc.customer = customer.name
	doc.country = country
	doc.city = city
	doc.data_center = data_center
	doc.subject = _require_text(subject, _("Subject"))
	doc.description = _require_text(description, _("Description"))
	doc.severity = _require_choice("severity", severity, default="P4")
	doc.action_required = _require_choice("action_required", action_required, default="Smart Hands")
	doc.action_other_details = cstr(action_other_details).strip() or None
	doc.target_equipment = cstr(target_equipment).strip() or None
	doc.external_reference = cstr(external_reference).strip() or None
	doc.engineers_required = max(1, cint(engineers_required) or 1)

	slots = parse_slot_grid(preferred_slots)
	if slots:
		doc.set("preferred_slots", slots)

	doc.insert()

	_attach_uploaded_files(doc, attachments)

	# before_insert has already stamped the performance block by this
	# point, so the response has to be filtered -- see readable_response.
	return readable_response(doc)


# -- the detail page ------------------------------------------------------


def build_portal_ticket_view(doc):
	"""Every value the detail page renders, named one at a time.

	Section 6: render from a permlevel-0 projection, never the whole
	document. `doc` does not reach Jinja -- this dict does."""
	# Shown from confirmed_date/confirmed_time, which are already local to
	# the site, rather than from the UTC confirmed_datetime -- and labelled
	# with the timezone so there is no ambiguity about whose clock it is.
	confirmed_display = ""
	if doc.confirmed_date and doc.confirmed_time:
		confirmed_display = f"{_fmt_date(doc.confirmed_date)} {doc.confirmed_time}"
		if doc.site_timezone:
			confirmed_display += f" ({doc.site_timezone})"

	view = {
		"name": doc.name,
		"subject": doc.subject,
		"status": doc.status,
		"status_class": STATUS_BADGE_CLASS.get(doc.status, "grey"),
		"severity": doc.severity,
		"country": doc.country,
		"city": doc.city,
		"data_center": doc.data_center,
		"site_timezone": doc.site_timezone,
		"action_required": doc.action_required,
		"action_other_details": doc.action_other_details,
		"target_equipment": doc.target_equipment,
		"external_reference": doc.external_reference,
		"engineers_required": cint(doc.engineers_required),
		"created": _fmt_datetime(doc.creation),
		"confirmed_display": confirmed_display,
		"is_confirmed": bool(doc.confirmed_datetime),
		"dc_access_ticket_number": doc.dc_access_ticket_number,
		"profile_released": bool(doc.engineer_profile_released),
		# Text Editor content: the only HTML on the page that did not come
		# from a template. Frappe already sanitises Text Editor fields on
		# save (BaseDocument._sanitize_content); sanitised again on the way
		# out, because this is the one value the template cannot escape and
		# still render as the customer typed it.
		"description_html": sanitize_html(doc.description or ""),
	}

	view["timeline"] = _build_timeline(doc, confirmed_display)
	view["engineers"] = [
		{
			"full_name": frappe.db.get_value("User", row.engineer, "full_name") or row.engineer,
			"assignment_type": row.assignment_type,
			"claimed": _fmt_datetime(row.claimed_at),
		}
		for row in doc.assigned_engineers
	]
	view["slots"] = slot_grid_for(doc)
	view["slot_band_labels"] = SLOT_BAND_LABELS
	view["proof_images"] = (
		[
			{"image": row.image, "image_type": row.image_type, "caption": row.caption}
			for row in doc.proof_of_work
		]
		if doc.status in PROOF_VISIBLE_STATUSES
		else []
	)
	view["attachments"] = frappe.get_all(
		"File",
		filters={"attached_to_doctype": TICKET, "attached_to_name": doc.name},
		fields=["file_name", "file_url"],
		order_by="creation asc",
	)
	view["work"] = _build_work_summary(doc.name)
	view["pending_count_request"] = frappe.db.get_value(
		"AC Engineer Count Request",
		{"ticket": doc.name, "status": "Pending"},
		["name", "current_count", "requested_count"],
		as_dict=True,
	)
	view["can_amend_slots"] = not doc.confirmed_datetime and doc.status not in FINAL_STATUSES
	view["can_enter_dc_access"] = bool(doc.engineer_profile_released)
	view["can_change_count"] = doc.status not in COUNT_CHANGE_BLOCKED_STATUSES

	# Section 4: the count-change actions call the methods Phase 3b already
	# built -- increase_engineer_count for an increase, create_count_request
	# for a decrease, withdraw_count_request to take a pending one back --
	# rather than portal-specific wrappers. Both already derive the customer
	# from the ticket and gate on _ensure_can_request_count_change, which is
	# exactly the check a portal caller needs.
	view["methods"] = {
		"amend_slots": method_path(amend_preferred_slots),
		"set_dc_access": method_path(set_dc_access_ticket_number),
		"increase_count": method_path(increase_engineer_count),
		"decrease_count": method_path(create_count_request),
		"withdraw_count": method_path(withdraw_count_request),
	}

	return view


def _build_timeline(doc, confirmed_display):
	"""Section 4: status and timeline. Built from permlevel-0 timestamps
	only; the severity-history rows are read field by field, because that
	child table carries two internal columns alongside the three a customer
	may see."""
	timeline = [{"label": _("Raised"), "at": _fmt_datetime(doc.creation)}]

	for row in doc.severity_history:
		timeline.append(
			{
				"label": _("Severity changed from {0} to {1}").format(row.old_severity, row.new_severity),
				"at": _fmt_datetime(row.changed_at),
			}
		)

	if doc.confirmed_datetime:
		timeline.append({"label": _("Visit confirmed"), "at": confirmed_display})
	if doc.profile_released_on:
		timeline.append(
			{"label": _("Engineer details released"), "at": _fmt_datetime(doc.profile_released_on)}
		)
	if doc.completed_on:
		timeline.append({"label": _("Work completed"), "at": _fmt_datetime(doc.completed_on)})

	return timeline


def _build_work_summary(ticket_name):
	"""Hours per engineer with a total, and submitted expense claims with
	their receipts.

	Reuses notifications.context.build_ticket_closed_context -- the
	explicit permlevel-0 field list Phase 5 built for the closure email,
	which is exactly the projection section 6 asks this page to render
	from. One helper, one field list, two surfaces."""
	summary = build_ticket_closed_context(ticket_name)
	return {
		"engineer_hours": summary["engineer_hours"],
		"total_hours": flt(summary["total_hours"]),
		"expenses": [
			{
				"category": claim["category"],
				"description": claim["description"],
				"amount": fmt_money(claim["amount"], currency=claim["currency"]),
				"receipt": claim.get("receipt"),
			}
			for claim in summary["expenses"]
		],
		"total_expenses": flt(summary["total_expenses"]),
	}


# -- section 4: the ticket actions ----------------------------------------


@frappe.whitelist(methods=["POST"])
def amend_preferred_slots(ticket=None, slots=None):
	"""Section 4: amend preferred slots while unconfirmed.

	Takes the ticket lock the same way claim_ticket and
	increase_engineer_count do, and for the same kind of reason: Ops
	confirming a slot and the customer rewriting the grid are the two
	halves of one race, and a confirmation landing on a band that has just
	been unticked is exactly what validate_confirmed_slot exists to
	prevent."""
	customer = get_portal_customer()
	# Ownership before anything else: a caller with no business on this
	# ticket should not get a validation error about their payload, and
	# should not have the payload parsed on their behalf either.
	_ensure_owned_ticket(ticket, customer)
	rows = parse_slot_grid(slots, required=True)

	def _attempt():
		doc = frappe.get_doc(TICKET, ticket, for_update=True)
		_ensure_owned_doc(doc, customer)

		if doc.status in FINAL_STATUSES:
			frappe.throw(_("Preferred slots cannot be changed once the ticket is {0}.").format(doc.status))
		if doc.confirmed_datetime:
			frappe.throw(
				_("The visit is already confirmed, so the preferred slots can no longer be changed.")
			)

		doc.set("preferred_slots", rows)
		doc.save(ignore_permissions=True)
		# Commits inside the lock window on purpose: _run_with_lock_retry rolls back and
		# re-acquires on a deadlock, so an attempt either commits or leaves nothing behind.
		frappe.db.commit()  # nosemgrep
		return doc

	doc = _run_with_lock_retry(_attempt)
	# A permlevel-0 acknowledgement, not the document: a whitelisted
	# method's return value is serialised into the response body without
	# any field-level filtering, so returning `doc` here would hand the
	# customer the performance block the templates are careful not to show.
	return {"ticket": doc.name, "slots": slot_grid_for(doc)}


@frappe.whitelist(methods=["POST"])
def set_dc_access_ticket_number(ticket=None, dc_access_ticket_number=None):
	"""Section 4: enter the DC access ticket number once engineer details
	have been released -- the number the data centre issues back once the
	customer has filed the access request with the engineer's details."""
	customer = get_portal_customer()
	_ensure_owned_ticket(ticket, customer)

	if not isinstance(dc_access_ticket_number, str):
		frappe.throw(_("Enter the data centre access ticket number."))
	number = dc_access_ticket_number.strip()
	if not number:
		frappe.throw(_("Enter the data centre access ticket number."))
	if len(number) > MAX_DC_ACCESS_LENGTH:
		frappe.throw(
			_("The data centre access ticket number cannot be longer than {0} characters.").format(
				MAX_DC_ACCESS_LENGTH
			)
		)

	def _attempt():
		doc = frappe.get_doc(TICKET, ticket, for_update=True)
		_ensure_owned_doc(doc, customer)

		if not doc.engineer_profile_released:
			frappe.throw(_("Engineer details have not been released for this ticket yet."))

		doc.dc_access_ticket_number = number
		doc.save(ignore_permissions=True)
		# Commits inside the lock window on purpose: _run_with_lock_retry rolls back and
		# re-acquires on a deadlock, so an attempt either commits or leaves nothing behind.
		frappe.db.commit()  # nosemgrep
		return doc

	doc = _run_with_lock_retry(_attempt)
	return {"ticket": doc.name, "dc_access_ticket_number": doc.dc_access_ticket_number}
