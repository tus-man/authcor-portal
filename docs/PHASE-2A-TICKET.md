# Phase 2a — Smart Hands Request

The ticket DocType, its child tables, and form behaviour. Pool visibility, claiming, and the race condition are **Phase 2b** — don't build them yet.

Build order: child tables first (the parent's Table fields need them to exist), then the ticket, then controller logic.

---

## 1. AC Preferred Slot (child table)

Tick **Is Child Table**.

| Fieldname | Label | Type | Reqd | List View |
|---|---|---|---|---|
| `slot_date` | Date | Date | ✓ | ✓ |
| `band_morning` | 9AM–1PM | Check | | ✓ |
| `band_afternoon` | 1PM–6PM | Check | | ✓ |
| `band_overnight` | 6PM–9AM | Check | | ✓ |

Three bands tiling 24 hours. **`band_overnight` runs 18:00 on `slot_date` through 09:00 the following day** — that asymmetry is the source of the trickiest validation in this phase.

---

## 2. AC Ticket Engineer (child table)

| Fieldname | Label | Type | Options | Reqd | List View |
|---|---|---|---|---|---|
| `engineer` | Engineer | Link | `User` | ✓ | ✓ |
| `assignment_type` | Type | Select | `Primary`<br>`Support` | ✓ | ✓ |
| `claimed_at` | Claimed At | Datetime | | | ✓ |
| `added_by` | Added By | Link | `User` | | |

Populated by claim logic in Phase 2b. Create the table now so the parent's Table field has a target.

`added_by` distinguishes a self-claim from an admin assignment — useful in the audit trail.

---

## 3. AC Proof Image (child table)

| Fieldname | Label | Type | Options | Reqd |
|---|---|---|---|---|
| `image` | Image | Attach Image | | ✓ |
| `image_type` | Type | Select | `Before`<br>`After`<br>`Other` | ✓ |
| `caption` | Caption | Data | | |

A child table rather than repeated Attach fields, because the review notes call for multiple images and the count isn't fixed.

---

## 4. AC Smart Hands Request

### Naming

Naming Rule: **By "Naming Series" field**

Auto Name: `naming_series:` — the fieldname only, not the pattern. The pattern lives on a field:

| Fieldname | Label | Type | Options | Hidden |
|---|---|---|---|---|
| `naming_series` | Series | Select | `AC-SH-.YYYY.-.#####` | ✓ |

Place it first in the field order. That produces `AC-SH-2026-00001`.

The pattern goes on the field rather than straight into Auto Name because Frappe's **Naming Series** admin tool only works with a `naming_series` field. When Authcor goes live and wants ticket numbers to start at 1000 rather than 1, that tool is how it's done.

Set **Title Field** to `subject`. Without it, list views and Link fields show `AC-SH-2026-00001` and nothing else.

### Customer & location

| Fieldname | Label | Type | Options | Reqd | Notes |
|---|---|---|---|---|---|
| `customer` | Customer | Link | `AC Customer` | ✓ | |
| `service_level` | Service Level | Data | | | Read Only. `fetch_from`: `customer.service_level` |
| `country` | Country | Link | `Country` | ✓ | Filtered by service area |
| `city` | City | Link | `AC City` | ✓ | Filtered by country + service area |
| `data_center` | Data Center | Link | `AC Data Center` | ✓ | Filtered by city + service area |
| `site_timezone` | Timezone | Data | | | Read Only. `fetch_from`: `city.timezone` |

**`fetch_from` is worth knowing** — set it in the field's properties as `linkfield.targetfield` and Frappe pulls the value automatically whenever the link changes. No code. Mark such fields Read Only so nobody edits a copy of someone else's data.

`service_level` is denormalised onto the ticket deliberately. If a customer upgrades from Basic to Premium next year, existing tickets keep the level they were raised under, and SLA history stays truthful.

### Request detail

| Fieldname | Label | Type | Options | Reqd |
|---|---|---|---|---|
| `subject` | Subject | Data | | ✓ |
| `severity` | Severity | Select | `P1`<br>`P2`<br>`P3`<br>`P4` | ✓ |
| `action_required` | Action Required | Select | see below | ✓ |
| `action_other_details` | Other Details | Data | | |
| `target_equipment` | Target Equipment | Small Text | | |
| `description` | Description | Text Editor | | ✓ |
| `attachments` | Attachments | Attach | | |
| `external_reference` | Customer Reference | Data | | |

**Set `severity` default to `P4`.** Every ticket starts scheduled-priority unless raised otherwise; Ops escalates on review.

`action_required` options — the review notes only say to *add* break-fix and Others, so the original list isn't in either document. Proposed starting set, **needs confirmation from Authcor**:

```
Remote Hands
Eyes & Hands
Break-fix
Installation
Decommission
Cabling
Media Handling
Others
```

`action_other_details` needs `eval:doc.action_required=="Others"` in **both** boxes on the field's properties panel:

- **Depends On** — controls visibility
- **Mandatory Depends On** — controls whether it's required

With only the second set, the field shows on every ticket and is merely conditionally required.

The JS linter attached to those boxes will flag `eval:` as a stray label with a missing semicolon. Ignore it — `eval:` is Frappe's own prefix and gets stripped before evaluation; the linter doesn't know that.

Enforce the same rule in `validate()` regardless. Both Depends On settings are client-side and don't apply to API writes.

`external_reference` covers doc 1 §4: team managers raising tickets on a customer's behalf using their own internal numbers.

### Engineers

| Fieldname | Label | Type | Options | Reqd | Default |
|---|---|---|---|---|---|
| `engineers_required` | Engineers Required | Int | | ✓ | `1` |
| `assigned_engineers` | Assigned Engineers | Table | `AC Ticket Engineer` | | |

Add a `validate()` rule requiring `engineers_required >= 1`. Frappe Int fields accept 0 and negatives.

### Scheduling

| Fieldname | Label | Type | Options |
|---|---|---|---|
| `preferred_slots` | Preferred Slots | Table | `AC Preferred Slot` |
| `confirmed_date` | Confirmed Date | Date | |
| `confirmed_time` | Confirmed Time | Select | `00:00` … `23:30` in 30-min steps (48 options) |
| `confirmed_datetime` | Confirmed (UTC) | Datetime | Read Only |
| `confirmed_engineer` | Confirmed Engineer | Link | `User` |

`confirmed_time` as a Select rather than a Time field is what delivers the review note's 30-minute dropdown. Options, one per line:

```
00:00  00:30  01:00  01:30  02:00  02:30  03:00  03:30
04:00  04:30  05:00  05:30  06:00  06:30  07:00  07:30
08:00  08:30  09:00  09:30  10:00  10:30  11:00  11:30
12:00  12:30  13:00  13:30  14:00  14:30  15:00  15:30
16:00  16:30  17:00  17:30  18:00  18:30  19:00  19:30
20:00  20:30  21:00  21:30  22:00  22:30  23:00  23:30
```

(Shown in columns for brevity — enter them one per line.)

No blank first option. An empty entry would make midnight indistinguishable from "not yet chosen," and since the field is unset until Ops confirms, those two states must stay distinct.

The stored value is a **string**, not a Time. It needs parsing before any arithmetic.

`confirmed_datetime` is computed in `validate()` from date + time interpreted in `site_timezone`, then stored as UTC. **This is the field SLA calculations and notifications should use** — never the local date and time fields, which are display inputs.

### Status, completion & DC access

| Fieldname | Label | Type | Options | Read Only |
|---|---|---|---|---|
| `status` | Status | Select | `New`<br>`In Pool`<br>`Partially Claimed`<br>`Claimed`<br>`Scheduled`<br>`In Progress`<br>`Completed`<br>`Cancelled` | |
| `completed_by` | Completed By | Link | `AC Engineer Profile` | |
| `completed_on` | Completed On | Datetime | | ✓ |
| `dc_access_ticket_number` | DC Access Ticket Number | Data | | |
| `engineer_profile_released` | Profile Released | Check | | ✓ |
| `profile_released_on` | Released On | Datetime | | ✓ |

Set `status` default to `New`, and **In List View**.

`completed_by` and `completed_on` both get **Depends On** `eval:doc.status=="Completed"`.

**`completed_by` is editable; `completed_on` is not.** They record different kinds of fact. Who did the work is a claim about the world that a human may legitimately correct — a team lead closing a ticket on an engineer's behalf must be able to attribute it correctly. When the record changed is a system observation, and making it editable would let completion timestamps be backdated, which matters once SLA reporting and monthly invoicing depend on them.

`completed_by` links to `AC Engineer Profile` rather than `User`, which constrains it at the schema level — work can only be attributed to a dispatchable engineer, and the rule holds for API writes as well as the form. Since that DocType is named by its `user` field, the stored value *is* an email address, but code comparing it to `frappe.session.user` should resolve the profile explicitly rather than relying on that coincidence.

### Proof of work

| Fieldname | Label | Type | Options |
|---|---|---|---|
| `proof_of_work` | Proof of Work | Table | `AC Proof Image` |

---

## 5. Controller logic

Written by Claude Code, not in Desk. Four pieces, in increasing difficulty.

### a. Slot auto-generation — `before_insert`

Create seven `AC Preferred Slot` rows for the next 7 days with all three bands ticked. The customer removes what doesn't suit; the default case needs no input.

Use `before_insert` rather than `after_insert` so the rows save with the parent in one write.

Only generate when `preferred_slots` is empty — a ticket created via API with slots supplied shouldn't have them overwritten.

### b. Engineers required validation — `validate`

Reject `engineers_required < 1`. Require `action_other_details` when `action_required == "Others"`.

### c. Confirmed slot validation — `validate`

The hard one. Given `confirmed_date` and `confirmed_time`, check the chosen moment falls inside a band the customer left ticked.

**The overnight band crosses midnight.** A confirmed time of 07:00 on 10 May belongs to the *9 May* `band_overnight` row, not 10 May's. So:

- 09:00–12:59 → `band_morning` on `confirmed_date`
- 13:00–17:59 → `band_afternoon` on `confirmed_date`
- 18:00–23:59 → `band_overnight` on `confirmed_date`
- 00:00–08:59 → `band_overnight` on **`confirmed_date` − 1 day**

Write the date-and-band resolution as a standalone function taking a date and a time and returning `(owning_date, band_fieldname)`. Test it independently — especially the boundaries 08:59/09:00, 12:59/13:00, 17:59/18:00, 23:59/00:00. Off-by-one errors here surface as Ops being told a slot the customer offered is unavailable.

Same 24-hour-day convention as the shift scheduling project; different domain, identical shape.

### d. Cascading location filter — client script

Country → City → Data Center, each filtered by the selected customer's `service_areas` child table.

This can't be done with a plain Link filter, because the constraint lives in a child table on a different document. The pattern is a whitelisted server method returning permitted values for the current customer, called from `frm.set_query()` in the client script.

Clear the downstream fields when an upstream one changes, or you'll get a city that doesn't belong to the selected country.

### e. Completion stamping — `validate`

When `status` first becomes `Completed`:

- Set `completed_on` to now
- Default `completed_by` to the `AC Engineer Profile` belonging to `frappe.session.user`, if one exists — leave blank if not, since not every user who can close a ticket is an engineer

On any transition **away** from `Completed`, clear both fields. A ticket reopened because the work wasn't right should not retain a stale completion record; the values must always describe the current completion, not an earlier one.

Only stamp when the value is currently empty, so an edit to an already-completed ticket doesn't overwrite the original timestamp.

---

## 6. Testing

**In Desk:** create a ticket and confirm seven slot rows appear pre-ticked; change the customer and confirm the location dropdowns re-filter; try a confirmed time in an unticked band and confirm rejection; try 07:00 against an overnight band ticked the *previous* day and confirm acceptance.

**In tests:** the slot resolver's boundaries, `engineers_required` validation, and `confirmed_datetime` conversion for a non-UTC city — a Singapore ticket at 09:00 local should store 01:00 UTC. Also: completing a ticket stamps `completed_on`; reopening it clears both completion fields; re-editing an already-completed ticket doesn't overwrite the original timestamp.

---

## Deferred

- **Phase 2b:** pool visibility, claim logic, multi-engineer allocation, engineer profile release
- **Phase 3:** SLA computed fields (`first_response_at`, `has_met_sla`, idle time), severity change history
- **Phase 6:** `linked_shipment` field

## Open with Authcor

The `action_required` option list above is proposed, not specified. Confirm before the client starts entering real tickets — changing Select options later means migrating existing data.