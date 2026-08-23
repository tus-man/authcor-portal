# Change Set — Post-Demo Feedback

Changes arising from Authcor's answers to the walkthrough questions. Several are corrections to assumptions we made; two move work into Phase 7.

Work through the Desk changes first (§9), then hand the code changes to Claude Code as one pass.

---

## 1. Ticket lifecycle — the significant change

Authcor has defined three distinct end states where we had one.

| Status | Meaning |
|---|---|
| `Open` | In the pool, unclaimed *(renamed from `In Pool`)* |
| `Partially Claimed` | Some but not all engineer slots filled |
| `Claimed` | Fully staffed |
| `Scheduled` | Visit date and time confirmed |
| `In Progress` | **Engineer has arrived at the data centre and started work** |
| `Completed` | **Work finished, engineer has left the site** |
| `Closed` | **All ticket documentation complete** |
| `Cancelled` | — |

The reasoning: engineers often write up details at the end of a shift. Without a separate `Closed` state, tickets would sit in `In Progress` long after the engineer had left, which misrepresents both the work and the SLA.

### This resolves an open question

`In Progress` now formally means **arrival on site**. Our assumption in Phase 3 — that the transition to In Progress marks arrival — turns out to be correct, and is now Authcor's stated definition rather than our guess.

So `actual_onsite_at` stamps on entry to `In Progress`, and `has_met_onsite_sla` measures against it. That assumption can be struck from the list.

### Gates on the two end states

**Entering `Completed` requires at least one proof-of-work image.**
Already built. Authcor confirmed it stays here — the photograph is how both Authcor and the customer know the physical work was actually done.

**Entering `Closed` requires:**
- At least one **submitted** time entry (draft entries don't count)
- Proof of work still present
- Everything else optional

Enforce both in `validate()` with clear messages naming what's missing. A ticket that can't close should say *why* it can't close.

### Renaming `In Pool` → `Open`

Change the Select options in Desk, then update every code reference. Grep for the string — it appears in status transition logic, the count-change blocked-status set, the pool visibility predicate, and a number of tests.

Existing dev tickets carry the old value. Since this is dev-only data, update in place:

```python
frappe.db.sql("UPDATE `tabAC Smart Hands Request` SET status='Open' WHERE status='In Pool'")
frappe.db.commit()
```

Before production, this becomes a proper migration patch.

### `Closed` blocks engineer count changes

Add it to `COUNT_CHANGE_BLOCKED_STATUSES` alongside `In Progress`, `Completed` and `Cancelled`.

---

## 2. Performance fields hidden from customers

Authcor: customers should not see the SLA performance block.

**Move to permlevel 1:**

`sla_policy` · `response_due` · `onsite_due` · `first_response_at` · `first_response_minutes` · `confirmed_at` · `confirmation_minutes` · `idle_minutes` · `actual_onsite_at` · `has_met_response_sla` · `has_met_onsite_sla`

**Keep at permlevel 0 — the customer must still see these:**

`total_hours` · `total_expenses`

That distinction matters. Hours worked and expense totals are exactly what Authcor wants emailed to the customer on closure. They currently sit in the same Performance section as the SLA fields, so it would be easy to move the whole block by mistake.

**Consider splitting the section in Desk** — "Work Summary" for the two customer-visible rollups, "SLA Performance" for the eleven internal ones. Same permlevels, but the form makes the boundary obvious to anyone maintaining it later.

Then grant permlevel 1 read on `AC Smart Hands Request` to Operations L1/L2/L3 and Admin L1/L2/L3. Not to Client roles.

---

## 3. Action Required — reduced to three

```
Smart Hands
Shipment and Storage
Others
```

Our eight-option list was invented; this is Authcor's actual taxonomy. `action_other_details` keeps its existing conditional-required behaviour on `Others`.

**Worth raising with Authcor:** "Shipment and Storage" as a ticket action overlaps with the separate Shipment Request form in Phase 6. Is a shipment ticket a smart-hands ticket with this action set, or a Shipment Request record, or does one create the other? The requirements describe cross-linking between two forms, which suggests they're distinct — but this option muddies that. Clarify before building Phase 6.

Existing dev tickets carry retired values; update or delete them.

---

## 4. Expense approval removed

Our assumption was wrong. Authcor doesn't want an approval step — claims reach the customer once the engineer submits with a receipt.

**Status becomes:**

| Status | Meaning |
|---|---|
| `Draft` | Engineer still entering |
| `Submitted` | Complete with receipt — **visible to the customer** |
| `Reimbursed` | Customer has paid |

Remove `Approved` and `Rejected` from the options.

**Remove:** `approve_claim`, `reject_claim`, and the Approve/Reject buttons. Keep `mark_reimbursed` — Authcor still needs to track payment.

**Fields `approved_by`, `approved_on`, `rejection_reason`** are now unused. Leave them in place for now rather than deleting — if Authcor changes their mind after seeing it in use, re-enabling is trivial, whereas re-adding deleted fields means another schema pass. Hide them from the form.

**The visibility hook changes** from `Approved`/`Reimbursed` to `Submitted`/`Reimbursed`. Both the `permission_query_conditions` and `has_permission` sides — they share a helper, so it should be one edit.

Receipt stays mandatory. That's now the only gate before a cost becomes visible to the customer, so it matters more than it did.

---

## 5. Preferred slots visible before saving

**Current behaviour:** the seven days generate in `before_insert`, so the customer sees them only after saving.

**Required behaviour:** the grid is already populated with the next seven days, all bands ticked, the moment the customer opens a new ticket form.

Authcor's reasoning is sound: a customer who never reopens a saved ticket would have offered availability they never saw. "We didn't select those dates" is a dispute worth designing out.

**Implementation:** client-side, in the form's `onload` for new documents. Add seven rows to `preferred_slots` with all three band checkboxes ticked.

**Keep the server-side `before_insert` generation** as a fallback for API-created tickets, guarded on the table being empty — which it will be for API creation and won't be for form creation. The two must not both fire and produce fourteen rows.

Dates are "the seven days after today," so they don't depend on any other field being filled — the grid can populate before the customer has chosen a customer or city.

---

## 6. Email hours and expenses on closure

**Trigger: entering `Closed`**, not `Completed`. Authcor corrected this — at `Completed` the hours may not be logged yet, which is precisely what `Closed` exists to guarantee.

Contents: time entries per engineer with a total, and expense claims with amounts and receipts. No rates, no billable amounts.

This is Phase 5 work. Recorded here so the trigger point is settled before templates get written.

---

## 7. Moved into Phase 7 (customer portal)

Both need the portal to exist.

**Portal-based ID disclosure.** Authcor accepted our v2 recommendation and wants it in v1. The engineer's NRIC/FIN is shown in an authenticated portal view scoped to that customer and ticket, rather than emailed. The email becomes a notification with a link.

Phase 2b was built so this is a change to one function — `disclose_engineer_profile` — plus a portal page. The disclosure log gains `channel = Portal` rows.

This also resolves the open question about removed engineers: with portal disclosure, a removed engineer's details simply stop being visible, so there's nothing to retract.

**L1 Authorizer user management.** A client L1 can request that a user be added or removed; any Authcor admin approves. Same request-and-approve shape as `AC Engineer Count Request`, so the pattern is already established.

**Phase 7 is now materially larger** than the original plan. It was already the biggest remaining piece; these two additions make it more so. Worth flagging when timelines are next discussed.

---

## 8. Assumptions now resolved

Struck from the list in `PHASE-4-TIME-EXPENSE.md` §8 and the walkthrough's Part 4:

| Question | Authcor's answer |
|---|---|
| What marks arrival on site? | Transition to `In Progress` — our assumption confirmed |
| Ticket statuses | `Open` replaces `In Pool`; `Closed` added |
| Action Required options | Three, as above |
| Expense approval | Not wanted — removed |
| NRIC disclosure channel | Portal view, in v1 |
| L1 user management | Confirmed, deferred to portal |

**Still open:**

- Expense categories — our list is still a guess
- Whether engineers should see each other's time entries — currently yes
- Which countries need holiday calendars
- Whether business hours vary by city or only by country
- Role naming (L1/L2/L3 inversion between Client and Operations)
- Whether "Shipment and Storage" as an action overlaps the Shipment Request form (§3)

---

## 9. Desk changes — do these first

1. `AC Smart Hands Request` → `status`: rename `In Pool` to `Open`, add `Closed` after `Completed`
2. `AC Smart Hands Request` → `action_required`: replace options with the three above
3. `AC Smart Hands Request`: set permlevel 1 on the eleven SLA fields in §2 — **leaving `total_hours` and `total_expenses` at 0**
4. Optionally split the Performance section into "Work Summary" and "SLA Performance"
5. Role Permissions Manager → `AC Smart Hands Request`: add permlevel 1 read rows for Ops L1/L2/L3 and Admin L1/L2/L3
6. `AC Expense Claim` → `status`: remove `Approved` and `Rejected`
7. `AC Expense Claim`: hide `approved_by`, `approved_on`, `rejection_reason`

Then commit the schema on its own before the code changes.

---

## 10. Code changes — for Claude Code

1. Status rename: every reference to `In Pool` → `Open`, including tests
2. `Closed` added to the lifecycle; add to `COUNT_CHANGE_BLOCKED_STATUSES`
3. `validate()` gate: entering `Closed` requires ≥1 **submitted** time entry and proof of work present
4. Confirm the existing proof-of-work gate on `Completed` still holds
5. Expense claim: remove `approve_claim` / `reject_claim` and their buttons; visibility hook changes to `Submitted`/`Reimbursed`
6. Client script: populate `preferred_slots` on new-form load; keep `before_insert` as an API fallback guarded on the table being empty
7. Update the dev data with the SQL in §1

### Tests to add

- Closing without a submitted time entry is rejected
- Closing with only a *draft* time entry is rejected
- Closing with a submitted entry and proof succeeds
- Completing without proof of work is still rejected
- A client role sees an expense claim at `Submitted` (previously `Approved`)
- A client role does **not** see `response_due` or `has_met_response_sla` — the new permlevel boundary
- A client role **does** still see `total_hours` and `total_expenses`

Those last two are the pair that matters. They're the boundary Authcor just asked for, and getting one right while getting the other wrong is the easy failure.