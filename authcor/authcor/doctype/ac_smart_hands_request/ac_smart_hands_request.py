# Copyright (c) 2026, TUSGLOBAL TECHNOLOGIES PVT LTD and contributors
# For license information, please see license.txt

import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate, now_datetime


def resolve_owning_slot(confirmed_date, confirmed_time):
	"""Given a confirmed date and a time-of-day, return (owning_date,
	band_fieldname): the AC Preferred Slot row and band checkbox that
	moment falls within.

	band_overnight runs 18:00 on a slot's date through 08:59 the following
	morning, so a confirmed time before 09:00 belongs to the *previous*
	day's overnight row, not the confirmed date's own row.
	"""
	if datetime.time(9, 0) <= confirmed_time < datetime.time(13, 0):
		return confirmed_date, "band_morning"
	if datetime.time(13, 0) <= confirmed_time < datetime.time(18, 0):
		return confirmed_date, "band_afternoon"
	if confirmed_time >= datetime.time(18, 0):
		return confirmed_date, "band_overnight"
	return confirmed_date - datetime.timedelta(days=1), "band_overnight"


def combine_local_time_to_utc(local_date, local_time, timezone_name):
	"""Combine a local date + time-of-day in the given IANA timezone into
	the equivalent naive UTC datetime, for storing in confirmed_datetime."""
	local_dt = datetime.datetime.combine(local_date, local_time, tzinfo=ZoneInfo(timezone_name))
	return local_dt.astimezone(datetime.UTC).replace(tzinfo=None)


def _ensure_customer_access(customer):
	if not frappe.has_permission("AC Customer", doc=customer):
		frappe.throw(_("Not permitted to access this customer."), frappe.PermissionError)


@frappe.whitelist()
def get_allowed_countries(customer):
	_ensure_customer_access(customer)
	return frappe.get_all(
		"AC Customer Service Area",
		filters={"parent": customer},
		pluck="country",
		distinct=True,
	)


@frappe.whitelist()
def get_allowed_cities(customer, country):
	_ensure_customer_access(customer)
	return frappe.get_all(
		"AC Customer Service Area",
		filters={"parent": customer, "country": country},
		pluck="city",
		distinct=True,
	)


@frappe.whitelist()
def get_allowed_data_centers(customer, city):
	_ensure_customer_access(customer)
	rows = frappe.get_all(
		"AC Customer Service Area",
		filters={"parent": customer, "city": city},
		fields=["data_center"],
	)
	if any(not row.data_center for row in rows):
		# A row for this city leaves data_center blank -- that authorises
		# every data centre in the city, not just the ones listed.
		return frappe.get_all("AC Data Center", filters={"city": city}, pluck="name")
	return list({row.data_center for row in rows if row.data_center})


class ACSmartHandsRequest(Document):
	def before_insert(self):
		self.generate_preferred_slots()

	def validate(self):
		self.validate_engineers_required()
		self.validate_action_other_details()
		self.validate_confirmed_slot()
		self.stamp_completion()

	def generate_preferred_slots(self):
		"""(a) Seven date rows for the next 7 days, starting the day after
		creation, all three bands ticked. Skipped if slots were already
		supplied (e.g. an API-created ticket), so they don't get
		overwritten."""
		if self.preferred_slots:
			return

		start = add_days(getdate(), 1)
		for offset in range(7):
			self.append(
				"preferred_slots",
				{
					"slot_date": add_days(start, offset),
					"band_morning": 1,
					"band_afternoon": 1,
					"band_overnight": 1,
				},
			)

	def validate_engineers_required(self):
		"""(b) engineers_required >= 1. non_negative on the field already
		blocks negatives client-side and in core Document validation; this
		catches zero, which non_negative allows."""
		if self.engineers_required < 1:
			frappe.throw(_("Engineers Required must be at least 1."))

	def validate_action_other_details(self):
		"""(b) Depends On / Mandatory Depends On on action_other_details are
		client-side only and don't apply to API writes -- enforce the same
		rule here."""
		if self.action_required == "Others" and not self.action_other_details:
			frappe.throw(_("Other Details is required when Action Required is Others."))

	def validate_confirmed_slot(self):
		"""(c) Given confirmed_date and confirmed_time, check the chosen
		moment falls inside a band the customer left ticked, and compute
		confirmed_datetime (UTC) from it. Skipped until Ops has actually
		confirmed a slot."""
		if not (self.confirmed_date and self.confirmed_time):
			return

		confirmed_date = getdate(self.confirmed_date)
		confirmed_time = datetime.datetime.strptime(self.confirmed_time, "%H:%M").time()
		owning_date, band_fieldname = resolve_owning_slot(confirmed_date, confirmed_time)

		slot_row = next(
			(row for row in self.preferred_slots if getdate(row.slot_date) == owning_date),
			None,
		)
		if not slot_row or not slot_row.get(band_fieldname):
			frappe.throw(
				_("The confirmed time {0} on {1} does not fall inside a time band the customer offered.").format(
					self.confirmed_time, self.confirmed_date
				)
			)

		timezone_name = frappe.db.get_value("AC City", self.city, "timezone")
		self.confirmed_datetime = combine_local_time_to_utc(confirmed_date, confirmed_time, timezone_name)

	def stamp_completion(self):
		"""(e) Stamp completed_on/completed_by the first time status becomes
		Completed; clear both on any transition away from it. The "only
		when currently empty" guard on the Completed branch is what makes
		re-editing an already-completed ticket a no-op instead of
		overwriting the original timestamp."""
		if self.status == "Completed":
			if not self.completed_on:
				self.completed_on = now_datetime()
			if not self.completed_by:
				# Resolve explicitly via the `user` field rather than
				# assuming AC Engineer Profile's name coincides with
				# frappe.session.user -- that's true today because the
				# doctype is named `field:user`, but not guaranteed.
				engineer_profile = frappe.db.get_value(
					"AC Engineer Profile", {"user": frappe.session.user}, "name"
				)
				if engineer_profile:
					self.completed_by = engineer_profile
		else:
			self.completed_on = None
			self.completed_by = None
