# Phase 3b — Engineer Count Change

Closes the open question from `docs/PHASE-2B-CLAIM.md`: can `engineers_required` be revised after engineers have claimed, and who decides who leaves?

**Authcor's answer:** increases are open and need no approval; decreases require an admin to approve and to choose who is removed; neither is permitted once work is `In Progress`.

---

## 1. The asymmetry

Increases and decreases are different operations and take different paths.

| | Increase | Decrease |
|---|---|---|
| Approval | None | Admin required |
| Effect | Reopens the ticket to more claimers | Someone must be removed |
| Who decides | Customer alone | Admin picks the engineer(s) |
| Path | Direct edit to `engineers_required` | Request → approve → removals |

An increase harms nobody: capacity goes up, status drops from `Claimed` back to `Partially Claimed`, the ticket returns to the pool. A decrease means telling an assigned engineer their job is cancelled, and that's Authcor's call — the customer says how many they need, Authcor says which people.

---

## 2. AC Engineer Count Request

New DocType. Naming: **By Naming Series** → `AC-ECR-.YYYY.-.#####`

| Fieldname | Label | Type | Options | Reqd | Read Only |
|---|---|---|---|---|---|
| `ticket` | Ticket | Link | `AC Smart Hands Request` | ✓ | |
| `customer` | Customer | Link | `AC Customer` | ✓ | ✓ |
| `current_count` | Current Count | Int | | ✓ | ✓ |
| `requested_count` | Requested Count | Int | | ✓ | |
| `reason` | Reason | Small Text | | | |
| `status` | Status | Select | `Pending`<br>`Approved`<br>`Rejected`<br>`Withdrawn` | ✓ | |
| `requested_by` | Requested By | Link | `User` | ✓ | ✓ |
| `requested_on` | Requested On | Datetime | ✓ | ✓ |
| `decided_by` | Decided By | Link | `User` | | ✓ |
| `decided_on` | Decided On | Datetime | | ✓ |
| `decision_note` | Decision Note | Small Text | | | |

`customer` and `current_count` are stamped from the ticket at creation. `current_count` is a snapshot rather than a live fetch — it records what the customer was looking at when they asked, which matters if the count changed in between.

**Permissions:** Client L1/L2 create and read their own (User Permissions scope it). Admin L1/L2/L3 and Ops L1 read and write. Ops L2/L3 read only — engineers should be able to see that a reduction is pending on a ticket they're assigned to.

Records are never deleted. A rejected or withdrawn request is part of the history.

---

## 3. Validation on creation

Reject with a clear message when:

- `requested_count` equals `current_count` — nothing to do
- `requested_count < 1`
- Ticket status is `In Progress`, `Completed`, or `Cancelled` — **Authcor's rule: no changes once work has started**
- A `Pending` request already exists for this ticket — one at a time, to keep the decision unambiguous
- `requested_count > current_count` — increases don't go through this DocType at all (see §4)

The `In Progress` block is the important one. Engineers may already be on site or travelling; the count is settled at that point.

---

## 4. Increases — no request needed

A whitelisted method, `increase_engineer_count(ticket, new_count)`.

Callable by the customer's own portal users, Ops L1, and Admins. Takes the ticket lock, same as claiming.

1. Reject if `new_count <= engineers_required` — this path is increases only
2. Reject if status is `In Progress`, `Completed`, or `Cancelled`
3. Set `engineers_required`
4. Recompute status: `Partially Claimed` if assigned engineers are now fewer than required
5. Commit

No approval, no record beyond the ticket's own change log — `track_changes` on the ticket captures the before and after.

Note the status recomputation matters: a `Claimed` ticket going from 2 to 3 must return to `Partially Claimed`, or it stays locked and no one can claim the third slot.

---

## 5. Decisions

Two whitelisted methods on the request, restricted to Admin L1/L2/L3 and Ops L1.

### `reject_count_request(request, note)`

Sets `status` to `Rejected`, stamps `decided_by` and `decided_on`. The ticket is untouched.

### `approve_count_request(request, engineers_to_remove, note)`

`engineers_to_remove` is a list — the admin's explicit choice.

Under the ticket lock:

1. Re-validate: the request is still `Pending`, the ticket is still not `In Progress`
2. Reject unless `len(engineers_to_remove)` exactly equals `current_assigned − requested_count`. **The admin must account for every removal** — no silent partial application.
3. Reject if any named engineer isn't actually assigned
4. Remove them, reusing the existing `remove_engineer` logic rather than duplicating it
5. Set `engineers_required` to `requested_count`
6. Recompute status
7. Stamp `decided_by`, `decided_on`, set `status` to `Approved`
8. Commit

### Ownership after removal

Already handled. `remove_engineer` from Phase 2b promotes the earliest-claimed Support to Primary when the Primary is removed, which is exactly Authcor's rule: the earliest claimer among those remaining becomes the owner.

**Reuse that path — don't reimplement it.** If approval writes its own removal logic, the promotion rule exists in two places and will eventually disagree with itself.

### Removing more than one

When several are removed at once, apply them one at a time through the same path so promotion resolves correctly at each step, rather than removing in bulk and then trying to work out who should own the ticket.

---

## 6. Withdrawal

`withdraw_count_request(request)` — the requesting customer sets `Pending` → `Withdrawn`.

Only the customer's own users, only while `Pending`. Lets them cancel without needing an admin to reject.

---

## 7. Notifications

Wire into Phase 5 rather than building now, but the events are:

- Request created → Ops L1 and the customer's Lead/Head
- Approved → customer, plus each removed engineer
- Rejected → customer

**Removed engineers must be told.** Someone who claimed a job and is no longer on it needs to know, and there's no other signal that would reach them.

---

## 8. UI

Buttons on the ticket, in the existing client script:

- **Request Engineer Change** — customer portal users. Opens a small dialog for the new count and a reason. Routes to `increase_engineer_count` directly if higher, or creates a request if lower. **Phase 7** — the customer portal doesn't exist yet, so this entry point isn't built. Built now: the same routing, as a Desk-only **Change Engineer Count** button restricted to Admin L1/L2/L3 and Ops L1 (`COUNT_CHANGE_ROLES` in `ac_smart_hands_request.js`). The decrease path calls a new whitelisted `create_count_request(ticket, requested_count, reason)` rather than `frappe.client.insert` directly, because section 2's permission table only grants Client L1/L2 Create on this doctype — Admin/Ops L1 don't hold it. `create_count_request` gates on the same role/customer check as `increase_engineer_count` (`_ensure_can_request_count_change`, renamed from `_ensure_can_increase_count` since both directions now share it) and inserts with `ignore_permissions=True`, same pattern as `add_engineer`/`remove_engineer` bypassing fine-grained doc permissions after their own role check. Phase 7's portal button can call the same method once it exists.
- **Approve / Reject** — on the request form, for Admins and Ops L1. Approve opens a multi-select of currently assigned engineers, constrained to exactly the number that must go. Built as a `MultiCheck` dialog field, populated by fetching the ticket's `assigned_engineers` via `frappe.model.with_doc` — not from this request's own `current_count` snapshot, since assignments can have moved since the request was raised. The required-removal count (and `approve_count_request` itself) is still computed from the live ticket, so a stale selection just fails server-side with a clear error.

A pending request should be visible on the ticket itself — an indicator or a linked-document panel — so an engineer opening their ticket can see a reduction is under consideration. Built as a dashboard headline alert on the ticket form, linking to the pending `AC Engineer Count Request`.

---

## 9. Tests

- Increase from 2 to 3 on a `Claimed` ticket → status returns to `Partially Claimed`, no request record
- Increase attempted on an `In Progress` ticket → rejected
- Decrease request created → validation passes, ticket unchanged until approval
- Decrease request on an `In Progress` ticket → rejected at creation
- Second `Pending` request on the same ticket → rejected
- Approve with the wrong number of engineers named → rejected
- Approve naming an engineer who isn't assigned → rejected
- Approve removing the Primary → earliest remaining Support becomes Primary
- Approve removing two at once from three → correct survivor is Primary
- Reject → ticket completely unchanged
- Withdraw by the requesting customer → `Withdrawn`; by another customer → denied
- Approve a request whose ticket moved to `In Progress` after it was raised → rejected at approval time

That last one is the race worth covering: validation at creation isn't enough, because the ticket can change state while the request sits pending.

---

## Open with Authcor

**Disclosure of removed engineers.** The customer already received the NRIC/FIN of an engineer who is no longer attending. Phase 2b deliberately does not delete disclosure log rows — the disclosure happened and the record stands — but Authcor may want the customer notified that a previously disclosed engineer is no longer assigned, so the data isn't used for a DC access request that shouldn't proceed.

Likely resolved by the v2 move to portal-based disclosure, where a revoked engineer's details simply stop being visible. Note it, decide later.