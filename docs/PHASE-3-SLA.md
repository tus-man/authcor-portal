# Phase 3 — SLA Engine

Business hours, holiday calendars, SLA policies, computed performance fields, and severity escalation.

The failures in this phase are quiet ones: a deadline an hour off, or a breach recorded against Authcor that was actually the customer's delay. Nothing crashes. It just produces wrong numbers on an invoice.

---

## 1. The three timestamps

Settled with Authcor:

| Event | Definition | When stamped |
|---|---|---|
| **First response** | An engineer claims the ticket | First row appended to `assigned_engineers` |
| **Confirmation** | An engineer sets the confirmed date and time | `confirmed_datetime` first populated |
| **Idle time** | Claim → confirmation | Derived from the two above |

**Idle time is deliberately *not* time-in-pool.** Pool time is already first response time, and it's Authcor's own dispatch delay — recording it twice under two names would double-count Authcor's fault. Idle time measures the window where an engineer is assigned and ready but the appointment can't be settled, typically because the customer hasn't confirmed a slot or supplied the DC access ticket number. That is the customer-caused delay, and it's what defends a missed onsite target in a billing dispute.

---

## 2. AC Business Hours (child table)

Tick **Is Child Table**. Rows hang off `AC City`.

| Fieldname | Label | Type | Options | Reqd |
|---|---|---|---|---|
| `day` | Day | Select | `Monday` … `Sunday` | ✓ |
| `start_time` | Start | Time | | ✓ |
| `end_time` | End | Time | | ✓ |

One row per working day. Days with no row are non-working.

Add to `AC City`:

| Fieldname | Label | Type | Options |
|---|---|---|---|
| `business_hours` | Business Hours | Table | `AC Business Hours` |
| `holiday_calendar` | Holiday Calendar | Link | `AC Holiday Calendar` |

---

## 3. AC Holiday Calendar

Naming: **By fieldname** → `calendar_name`

| Fieldname | Label | Type | Options | Reqd |
|---|---|---|---|---|
| `calendar_name` | Calendar Name | Data | | ✓ |
| `country` | Country | Link | `Country` | |
| `year` | Year | Int | | ✓ |
| `holidays` | Holidays | Table | `AC Holiday` | |

### AC Holiday (child table)

| Fieldname | Label | Type | Reqd |
|---|---|---|---|
| `holiday_date` | Date | Date | ✓ |
| `description` | Description | Data | |

Calendars are per-year because public holidays change annually. Linking the calendar from the city rather than the country lets two cities in one country diverge — which happens more than you'd expect.

---

## 4. AC SLA Policy

Naming: **By fieldname** → `policy_name`

| Fieldname | Label | Type | Options | Reqd |
|---|---|---|---|---|
| `policy_name` | Policy Name | Data | | ✓ |
| `service_level` | Service Level | Select | `Premium`<br>`Standard`<br>`Basic`<br>`P4-Scheduled` | ✓ |
| `severity` | Severity | Select | `P1`<br>`P2`<br>`P3`<br>`P4` | ✓ |
| `response_minutes` | Response Target (min) | Int | | ✓ |
| `onsite_minutes` | Onsite Target (min) | Int | | |
| `onsite_next_business_day` | Onsite = Next Business Day | Check | | |
| `business_hours_only` | Business Hours Only | Check | | |
| `is_active` | Active | Check | | |

Add a unique constraint on `service_level` + `severity` — either via `validate()` or by naming the record `{service_level}-{severity}`. Two active policies for the same pair means an ambiguous lookup.

### The records to create

Sixteen rows. `business_hours_only` ticked on Basic only.

| Service level | Sev | Response (min) | Onsite (min) | NBD |
|---|---|---|---|---|
| Premium | P1 | 60 | 180 | |
| Premium | P2 | 120 | 360 | |
| Premium | P3 | 180 | 1440 | |
| Premium | P4 | 360 | — | |
| Standard | P1 | 60 | 240 | |
| Standard | P2 | 120 | — | ✓ |
| Standard | P3 | 240 | 2160 | |
| Standard | P4 | 480 | — | |
| Basic | P1 | 120 | 480 | |
| Basic | P2 | 240 | — | ✓ |
| Basic | P3 | 480 | 2880 | |
| Basic | P4 | 960 | — | |
| P4-Scheduled | P1 | — | — | ✓ |
| P4-Scheduled | P2 | — | — | ✓ |
| P4-Scheduled | P3 | — | — | ✓ |
| P4-Scheduled | P4 | — | — | ✓ |

**P4 rows carry a response target but no onsite target.** For scheduled work the onsite obligation is the confirmed slot, not a duration from creation — arrival is measured against the agreed appointment.

The P4-Scheduled rows all behave identically because that service level only ever covers planned work; severity is effectively ignored for those customers.

**Basic's targets are in business minutes, not wall-clock.** 960 minutes of an 8-hour working day is two working days, not sixteen hours.

---

## 5. Business-hours arithmetic

The core of this phase, and the part to build and test in isolation before wiring it to anything.

Two pure functions, module-level, no document context:

**`add_business_minutes(start_utc, minutes, city) -> datetime`**
Advances a UTC timestamp by N *working* minutes, skipping non-working hours, non-working days, and holidays. Used to compute deadlines for `business_hours_only` policies.

**`next_business_day_end(start_utc, city) -> datetime`**
Returns the end of the next working day. Used for `onsite_next_business_day`.

For policies without `business_hours_only`, deadlines are plain wall-clock addition — no calendar involved.

### Requirements

- Convert UTC to the city's local timezone using `ZoneInfo`, do the arithmetic locally, convert back. Same pattern as `combine_local_time_to_utc` in Phase 2a.
- If the start falls outside working hours, the clock begins at the next working period's start — not immediately.
- Consult `holiday_calendar` for the year in question. **A missing calendar must not silently mean "no holidays"** — log a warning, because a missing 2027 calendar would otherwise produce quietly wrong deadlines all year.
- A city with no `business_hours` rows and a `business_hours_only` policy is a configuration error. Raise rather than guessing.

### Tests

These are pure functions, so test them directly and thoroughly:

- Start mid-working-day, add less than the remaining time → same day
- Start mid-working-day, add more than remaining → rolls to the next working day
- Start on a Saturday → begins Monday morning
- Start on a holiday → skips it
- Spanning a weekend and a holiday together
- Start before opening → begins at opening
- Start after closing → begins next working day
- A DST transition inside the window (Amsterdam, March) → correct local wall-clock result

---

## 6. Computed fields on the ticket

All Read Only. Group them in a "Performance" section.

| Fieldname | Label | Type |
|---|---|---|
| `sla_policy` | SLA Policy | Link → `AC SLA Policy` |
| `response_due` | Response Due | Datetime |
| `onsite_due` | Onsite Due | Datetime |
| `first_response_at` | First Response At | Datetime |
| `first_response_minutes` | First Response (min) | Int |
| `confirmed_at` | Confirmed At | Datetime |
| `confirmation_minutes` | Confirmation (min) | Int |
| `idle_minutes` | Idle Time (min) | Int |
| `actual_onsite_at` | Actual Onsite At | Datetime |
| `has_met_response_sla` | Met Response SLA | Select — `Yes`/`No`/`Pending` |
| `has_met_onsite_sla` | Met Onsite SLA | Select — `Yes`/`No`/`N/A`/`Pending` |

**Store these rather than computing on read.** SLA policies change; the historic verdict must not change with them. This is the same reasoning behind denormalising `service_level` onto the ticket in Phase 2a.

**`sla_policy` is a link to the resolved policy record**, captured at creation. If Authcor revises Premium P1 next year, this ticket still points at the policy it was judged under.

`actual_onsite_at` has no automatic source — nothing in the system observes an engineer arriving. Options: leave it manual for now, or derive it from the transition to `In Progress`. **Confirm with Authcor before building.**

### Verdicts

`has_met_response_sla`: `Pending` until claimed, then `Yes`/`No` against `response_due`.

`has_met_onsite_sla`: `N/A` when the policy has no onsite target (all P4 rows). Otherwise `Pending` until `actual_onsite_at` is set, then compared to `onsite_due`.

Separating them matters — a ticket can meet response and miss arrival, and one combined flag would hide which.

---

## 7. Severity escalation

### AC Severity History (child table)

| Fieldname | Label | Type | Options | Reqd |
|---|---|---|---|---|
| `changed_at` | Changed At | Datetime | | ✓ |
| `changed_by` | Changed By | Link | `User` | ✓ |
| `old_severity` | From | Select | `P1`…`P4` | ✓ |
| `new_severity` | To | Select | `P1`…`P4` | ✓ |
| `old_response_due` | Previous Response Due | Datetime | | |
| `old_verdict` | Verdict At Change | Data | | |

Add `severity_history` (Table) to the ticket.

### On severity change — `validate`

Authcor's decision: **the clock restarts from the moment of escalation.**

1. Append a history row capturing the old severity, its `response_due`, and the verdict at that instant
2. Resolve the new SLA policy for `service_level` × new severity
3. Recompute `response_due` and `onsite_due` **from now**, not from creation
4. Reset `has_met_response_sla` to `Pending` if not yet responded

Preserving the old verdict is what stops escalation being used to erase a breach. Without it, a ticket that blew its P4 target could be bumped to P1 and appear clean.

**Only Ops and Admin roles may change severity after creation.** The customer sets it at creation; changing it afterwards is a dispatch decision.

---

## 8. Breach detection — scheduled job

Runs every 15 minutes.

Find tickets where `response_due` has passed and `has_met_response_sla` is `Pending` → set `No`. Same for onsite.

Why a job rather than computing on read: the verdict must be recorded when it happens, not inferred later. It's also the hook Phase 5 uses for escalation alerts.

Register in `hooks.py` under `scheduler_events`. Remember background job failures surface only in **Error Log**.

---

## 9. Manual verification

- Create a Premium P1 ticket → `response_due` is 60 minutes out
- Create a Basic P3 ticket on a Friday afternoon → `onsite_due` lands the following week, not Sunday
- Claim it → `first_response_at` stamps, `first_response_minutes` populates, verdict flips to `Yes`
- Set a confirmed slot → `confirmed_at` stamps and `idle_minutes` equals the gap since claim
- Escalate P4 → P1 → history row written with the old verdict, `response_due` recomputed from now
- Create a ticket in an Amsterdam data centre and check the deadline against local wall-clock time

---

## Open with Authcor

1. **What sets `actual_onsite_at`?** Manual entry, or the transition to `In Progress`? Without an answer the onsite verdict never resolves.
2. **Holiday calendars for which countries?** Someone has to enter the actual dates before Basic-tier SLAs mean anything. Singapore at minimum; every country with a data centre in scope eventually.
3. **Do business hours vary by city or only by country?** The model supports per-city; confirm that's wanted before entering data.