# Phase 4 — Time & Expense Capture

Engineers log hours worked and out-of-pocket expenses. Customers see both — hours and expense amounts — but never rate or billable value.

**Scope is capture only.** Rate cards, billable amount calculation, invoicing, and the weekly/monthly CSV and PDF reports are v2, per Authcor's decision. What's built here is the data those will eventually consume.

---

## 1. Visibility, and why permlevel does the work

Authcor's rule from the review meeting: the customer sees the hours an engineer clocked and the expense figures they'll be reimbursing. They do not see rates or computed amounts — those arrive on the monthly invoice, not in the portal.

Frappe's **permlevel** handles this on one document rather than two. Fields at permlevel 0 are visible to anyone with read access; permlevel 1 fields need an explicit grant. Same record, two audiences.

| Permlevel 0 (customer-visible) | Permlevel 1 (internal only) |
|---|---|
| Engineer, dates, start/end times | Rate category |
| Computed hours | Rate applied |
| Expense category, amount, receipt | Billable amount |

The v2 rate engine adds fields at permlevel 1 only. Nothing about customer visibility changes when it lands.

---

## 2. AC Time Entry

Naming: **By Naming Series** → `AC-TE-.YYYY.-.#####`

Standalone DocType, not a child table of the ticket. Month-end reporting needs "all billable minutes across all customers in July grouped by category," and that query is far cleaner against a top-level DocType. It also lets an engineer log time from a list view without opening the ticket.

| Fieldname | Label | Type | Options | Reqd | Read Only | Permlevel |
|---|---|---|---|---|---|---|
| `naming_series` | Series | Select | `AC-TE-.YYYY.-.#####` | | | 0 |
| `ticket` | Ticket | Link | `AC Smart Hands Request` | ✓ | | 0 |
| `customer` | Customer | Link | `AC Customer` | ✓ | ✓ | 0 |
| `engineer` | Engineer | Link | `AC Engineer Profile` | ✓ | | 0 |
| `work_date` | Work Date | Date | ✓ | | 0 |
| `start_time` | Start Time | Datetime | ✓ | | 0 |
| `end_time` | End Time | Datetime | ✓ | | 0 |
| `duration_minutes` | Duration (min) | Int | | ✓ | 0 |
| `duration_display` | Duration | Data | | ✓ | 0 |
| `notes` | Notes | Small Text | | | 0 |
| `rate_category` | Rate Category | Select | `Standard`<br>`After Hours`<br>`Weekend`<br>`Holiday` | | ✓ | **1** |

`customer` is stamped from the ticket. Denormalised so month-end queries don't have to join through the ticket.

**Start and end times rather than a typed duration.** Authcor's choice, and the right one: computed hours can be audited, overlapping entries can be detected, and the record shows when work actually happened rather than what someone asserted afterwards. The cost is more typing at 3am, which the UI should mitigate (see §6).

`duration_display` holds a human-readable form — `7h 30m` — because raw minutes read badly on a customer-facing screen. Computed alongside `duration_minutes`.

`rate_category` is **derived, not typed.** Compute it in `validate()` from `start_time` against the site's business hours and holiday calendar — the same machinery Phase 3 built. An engineer entering 03:00 on a Sunday shouldn't have to remember it's a weekend rate. It sits at permlevel 1 because it's a billing input, and it exists now (rather than in v2) because deriving it at entry time is correct and deriving it retrospectively is not — business hours may have changed by then.

### Submittable

Set **Is Submittable** on this DocType.

A submitted document in Frappe is locked: it can be cancelled but not silently edited. That's exactly right for a billing record. Draft while the engineer is working on it, submitted when they're done, and after that any change leaves a trail.

Doc 1 §5 says team managers can edit time entries but only with L3 Admin approval and an audit log. That's a v2 amendment workflow — for now, submitted entries are corrected by cancelling and re-entering, which is auditable by construction.

### Validation

- `end_time` must be after `start_time`
- Reject a duration over 24 hours — almost certainly a date entry error
- Reject entries whose `start_time` is in the future
- **Warn on overlap:** the same engineer with another entry covering the same period. Warn rather than block — an engineer could legitimately be logging two tickets worked in parallel at the same site — but Ops should see it.
- The engineer must be assigned to the ticket, unless the caller is Ops L1 or an Admin logging on someone's behalf

### Permissions

| Role | Read | Write | Create | Submit | Cancel | Permlevel 1 |
|---|---|---|---|---|---|---|
| Client L1/L2 | ✓ | | | | | |
| Ops L3 (engineers) | ✓ | ✓ | ✓ | ✓ | | |
| Ops L2 | ✓ | ✓ | ✓ | ✓ | | |
| Ops L1 | ✓ | ✓ | ✓ | ✓ | ✓ | read |
| Admin L1 | ✓ | ✓ | ✓ | ✓ | ✓ | read + write |
| Admin L2 | ✓ | ✓ | ✓ | ✓ | ✓ | read + write |
| Admin L3 | ✓ | | | | | read |

Engineers can submit but not cancel — undoing a submitted billing record is a supervisor action.

Customer isolation comes from the existing User Permissions on `AC Customer`, since `customer` is a link field here.

**Engineers seeing each other's entries** is intentional: a Support engineer on a shared ticket should see the Primary's hours. If Authcor wants that narrowed, it's a `permission_query_conditions` hook — note it as a question rather than building it now.

---

## 3. AC Expense Claim

Naming: **By Naming Series** → `AC-EXP-.YYYY.-.#####`

| Fieldname | Label | Type | Options | Reqd | Read Only | Permlevel |
|---|---|---|---|---|---|---|
| `naming_series` | Series | Select | `AC-EXP-.YYYY.-.#####` | | | 0 |
| `ticket` | Ticket | Link | `AC Smart Hands Request` | ✓ | | 0 |
| `customer` | Customer | Link | `AC Customer` | ✓ | ✓ | 0 |
| `engineer` | Engineer | Link | `AC Engineer Profile` | ✓ | | 0 |
| `expense_date` | Expense Date | Date | ✓ | | 0 |
| `category` | Category | Select | see below | ✓ | | 0 |
| `description` | Description | Small Text | ✓ | | 0 |
| `amount` | Amount | Currency | ✓ | | 0 |
| `currency` | Currency | Link → `Currency` | ✓ | | 0 |
| `receipt` | Receipt | Attach | ✓ | | 0 |
| `status` | Status | Select | `Draft`<br>`Submitted`<br>`Approved`<br>`Rejected`<br>`Reimbursed` | ✓ | ✓ | 0 |
| `approved_by` | Approved By | Link → `User` | | ✓ | 0 |
| `approved_on` | Approved On | Datetime | | ✓ | 0 |
| `rejection_reason` | Rejection Reason | Small Text | | | 0 |

Categories, **proposed — needs Authcor's confirmation**:

```
Transport
Parking
Tolls
Meals
Accommodation
Parts & Consumables
Courier
Other
```

`currency` matters because Authcor dispatches globally — an engineer in Amsterdam claims euros. Conversion is a v2 problem alongside the rate engine; capture the currency now so the data is usable later.

**Receipt is mandatory.** Enforce in `validate()`, not just with the Reqd flag — an API write could otherwise bypass it.

### Approval

**This is an assumption, not an instruction from Authcor.** Doc 1 says engineers log expenses with mandatory receipts and those are visible to the customer, with no approval step described. An approval gate is proposed because the customer is reimbursing these and it's normal practice for the supplier to check before a cost reaches the client.

Flow: engineer creates `Draft` → submits → Ops L1 or Admin approves or rejects → `Reimbursed` set when payment is made.

**The customer sees `Approved` and `Reimbursed` claims only.** Draft, Submitted and Rejected are internal. That's a `permission_query_conditions` hook combined with the existing customer User Permissions — client roles see rows for their customer *and* in an approved state.

If Authcor says approval isn't wanted, removing it means dropping the status gate and letting the customer see everything with a receipt. Contained change.

Rejection requires a reason. An engineer whose out-of-pocket claim is refused needs to know why.

### Permissions

Same shape as Time Entry, with approval restricted to Ops L1 and Admin L1/L2. Engineers create and submit their own; they do not approve.

**Consider "Only If Creator" for Ops L3 here** — unlike time entries, there's no operational reason for one engineer to see another's expense claims, and these carry personal financial information. Ops L1 and above see everything for approval. This is the one place in the build where that flag is the right tool.

---

## 4. Ticket-side rollups

Read-only fields on `AC Smart Hands Request`, in the Performance section:

| Fieldname | Label | Type | Permlevel |
|---|---|---|---|
| `total_hours` | Total Hours | Float | 0 |
| `total_expenses` | Total Expenses | Currency | 0 |

Recompute on time entry and expense claim submit and cancel. Both permlevel 0 — the customer sees these.

`total_expenses` counts `Approved` and `Reimbursed` only, consistent with what the customer can see. Mixing in unapproved claims would show a total that doesn't match the rows beneath it.

**Per-engineer breakdown**, matching the reference system's layout: a table showing each engineer and their hours with a total. Build it as a dashboard section or a small report on the ticket rather than more stored fields — it's derived data and storing it invites drift.

---

## 5. The "unlogged hours" nudge

A scheduled job, daily. Find tickets `Completed` more than 24 hours ago with assigned engineers who have no submitted time entry, and email those engineers.

Not in Authcor's requirements. Taken from the reference system, which had exactly this. Worth building because these hours feed a monthly invoice — an engineer who forgets to log is revenue Authcor never bills for.

Keep the tone light. It's a reminder, not a reprimand.

---

## 6. UI

**On the ticket:** "Log Time" and "Add Expense" buttons for assigned engineers, opening a dialog prefilled with the ticket, customer, and the engineer's own profile. Fewer fields to fill at 3am.

**Default `end_time` to now and `start_time` to now minus an hour** in the dialog, so the common case is adjusting two prefilled values rather than typing two timestamps. This is the friction mitigation that makes start/end times tolerable in the field.

**On the expense claim:** Approve and Reject buttons for Ops L1 and Admin L1/L2, visible while `Submitted`. Reject requires a reason.

**Linked lists on the ticket** showing time entries and expense claims, so the whole picture is on one screen.

---

## 7. Tests

- Duration computed correctly from start and end
- `end_time` before `start_time` rejected
- Duration over 24 hours rejected
- Future `start_time` rejected
- Overlapping entries for the same engineer produce a warning, not a rejection
- Rate category derived correctly: a weekday afternoon is Standard, 03:00 is After Hours, Sunday is Weekend, a date in the holiday calendar is Holiday
- An engineer not assigned to the ticket is rejected; Ops L1 doing the same succeeds
- Submitted entries cannot be edited
- Expense without a receipt rejected
- A client role reading a time entry does **not** see `rate_category` — the permlevel 1 check
- A client role sees `Approved` expenses but not `Draft`, `Submitted`, or `Rejected`
- Rejection without a reason is rejected
- Ticket rollups update on submit and revert on cancel
- Rollup counts approved expenses only

The permlevel test is the one that matters most. It's the mechanism the whole visibility model rests on, and it has never been directly tested in this build.

---

## 8. Decisions made without Authcor's input

For the walkthrough. Each is a considered assumption, not a requirement.

1. **Expense approval workflow.** Doc 1 describes no approval step. Proposed because the customer reimburses these. If unwanted, drop the status gate.
2. **Expense categories.** The list in §3 is invented.
3. **Rate category derived at entry rather than in v2.** Deriving it later would use business hours that may have changed since.
4. **Engineers see each other's time entries but not each other's expense claims.** Operational visibility on one, financial privacy on the other.
5. **The unlogged-hours nudge.** Not requested; taken from the reference system.
6. **Currency captured per claim.** Conversion deferred; the field exists so the data is usable when it lands.
7. **Submitted entries corrected by cancel-and-reenter**, rather than doc 1's team-manager edit with L3 Admin approval. That amendment workflow is v2.

---

## Deferred to v2

Rate cards and rate resolution · billable amount calculation · currency conversion · weekly and monthly CSV/PDF reports to Accounts, Team Managers and Field Engineers · the team-manager time entry amendment workflow with L3 Admin approval and audit log.