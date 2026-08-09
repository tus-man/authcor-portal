# Phase 2b — Pool, Claim & Profile Release

The dispatch layer. Three concerns: who can *see* a ticket, who can *take* it, and what gets disclosed when they do.

This is the hardest phase in the build. Two things in it — the permission split and the claim race — are the kind that pass a casual test and fail in production.

---

## 1. Visibility is two mechanisms, not one

Frappe resolves list queries and direct record access through **different code paths**. Restricting one does not restrict the other.

| Mechanism | Governs | Hook |
|---|---|---|
| `permission_query_conditions` | List views, reports, `get_list`, link searches | returns a SQL `WHERE` fragment |
| `has_permission` | Opening a single record, `get_doc`, API reads | returns `True` / `False` |

**Both must be implemented, and they must agree.** Implementing only the first produces a system where an engineer's list looks correctly filtered but typing another customer's ticket URL loads it fine. That is the exact failure the Phase 1 isolation test was designed to catch, and it applies here with more force because tickets carry customer data.

Register both in `hooks.py`:

```python
permission_query_conditions = {
    "AC Smart Hands Request": "authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request.get_permission_query_conditions",
}

has_permission = {
    "AC Smart Hands Request": "authcor.authcor.doctype.ac_smart_hands_request.ac_smart_hands_request.has_permission",
}
```

Write the predicate **once**, as a shared helper, and have both hooks express it. If the SQL fragment and the boolean check are written independently they will drift, and the drift will be silent.

### The predicate

| Role | Sees |
|---|---|
| Admin L1, Admin L2 | Everything — return no restriction |
| Admin L3 | Everything the hook allows; scoping comes from their User Permissions |
| Operations L1 | All tickets — coordinates across teams and cities |
| Operations L2, L3 | Tickets for customers they are listed against in `AC Customer.authorised_engineers`, **plus** any ticket they are already assigned to |
| Client roles | Their own customer — handled by User Permissions, no hook logic needed |

The second clause on Ops L2/L3 matters: an engineer must not lose sight of a ticket because their authorisation for that customer was later removed while they were still working it.

### SQL injection

The condition is a raw SQL string. The user identifier goes into it.

**Escape it with `frappe.db.escape(user)` — never f-string or concatenate it in.** The value comes from the session rather than a request parameter, so this isn't directly attacker-controlled today, but a query-condition hook is a permanent piece of infrastructure and the habit needs to be right from the start.

---

## 2. AC ID Disclosure Log

New DocType. Create in Desk before writing the claim logic.

Naming: **By Naming Series** → `AC-DISC-.YYYY.-.#####`

| Fieldname | Label | Type | Options | Reqd |
|---|---|---|---|---|
| `ticket` | Ticket | Link | `AC Smart Hands Request` | ✓ |
| `engineer` | Engineer | Link | `AC Engineer Profile` | ✓ |
| `customer` | Customer | Link | `AC Customer` | ✓ |
| `disclosed_to` | Disclosed To | Data | `Email` | ✓ |
| `disclosed_on` | Disclosed On | Datetime | | ✓ |
| `channel` | Channel | Select | `Email`<br>`Portal` | ✓ |

Permissions: **read for Admin L1 and L2 only.** Nobody needs write — rows are created by code.

Why this exists now, when the disclosure channel is only email: Authcor confirmed NRIC/FIN goes to the customer, and PDPA obligations attach to that disclosure regardless of transport. The log is what turns "we think we sent it to the right people" into a defensible record. It costs almost nothing to write and cannot be reconstructed after the fact.

It also makes the v2 switch to a portal view cheap — change what the disclosure function emits and set `channel` to `Portal`. Nothing else moves.

---

## 3. Claim logic

A whitelisted method, `claim_ticket(ticket)`.

### Sequence

1. Resolve the caller's `AC Engineer Profile`; reject if none, or if inactive
2. **Acquire an exclusive row lock on the ticket**
3. Re-read `assigned_engineers` — after the lock, never before
4. Reject if the caller is already assigned
5. Reject if `len(assigned_engineers) >= engineers_required`
6. Reject if the caller is not in that customer's `authorised_engineers`
7. Append a row: `assignment_type` = `Primary` when the list was empty, otherwise `Support`; stamp `claimed_at`; set `added_by` to the caller
8. Set status: `Claimed` if the required count is now met, else `Partially Claimed`
9. Disclose the engineer's profile to the customer, and write the disclosure log row
10. Commit

### The lock

Steps 2–3 are the whole point. A read-then-write without a lock lets three engineers pass the count check simultaneously on a two-slot ticket and all three get in.

The lock must be a `SELECT ... FOR UPDATE` on the ticket row, held for the rest of the transaction. Frappe offers this in more than one form — grep `apps/frappe/frappe/database/` for `for_update` and use whichever the framework provides rather than hand-writing SQL if there's a supported path.

**The count check must happen after the lock, not before.** Checking first and locking second is the same bug with extra steps.

### Ordering note on step 6

Authorisation is checked *after* the lock rather than before, so it can't be read from a stale state. Cheap to do inside the lock; the lock is held for microseconds either way.

### FIN expiry

Before disclosing, check the engineer's `id_type` and `id_expiry`. An engineer whose FIN expires before the confirmed slot cannot be dispatched, and Authcor should learn that at claim time rather than at the data centre door.

Reject the claim, with a message naming the expiry date.

### Disclosure

Assemble what the customer receives in **one function**, called from nowhere else. It reads the NRIC/FIN with `get_decrypted_password("AC Engineer Profile", name, "id_number")`, builds the message, sends it, and writes the log row.

Recipients: the customer's `portal_users`, plus their `leads_and_heads` per the notification rules.

Keeping this in a single function is what makes the v2 portal switch a contained change rather than an audit of six notification templates.

---

## 4. Add and remove engineers

Two more whitelisted methods, for Admins and Operations L1/L2 only.

**`add_engineer(ticket, engineer)`** — bypasses the claim queue. Same lock, same capacity check, same disclosure. `added_by` records the admin, not the engineer, which is what distinguishes an assignment from a self-claim in the audit trail.

**`remove_engineer(ticket, engineer)`** — needs an explicit rule for one case:

> If the engineer being removed is `Primary` and Support engineers remain, promote the earliest-claimed Support to Primary.

Without that, a ticket can end up with only Support engineers and no owner. Status also has to recompute — removing an engineer from a `Claimed` ticket drops it back to `Partially Claimed` and returns it to the pool.

The disclosure log is **not** deleted when an engineer is removed. The disclosure happened; the record of it stands.

---

## 5. Tests

### What can be tested straightforwardly

- An engineer sees pool tickets for authorised customers and not others
- **The same predicate through both paths**: `get_list` and a direct `get_doc` on a ticket outside the engineer's scope. This is the test that catches the two-mechanism problem.
- First claimer gets `Primary`, second gets `Support`
- Claiming a full ticket is rejected
- Claiming twice as the same engineer is rejected
- An unauthorised engineer is rejected
- Status moves `In Pool` → `Partially Claimed` → `Claimed` as slots fill
- Removing the Primary promotes the earliest Support
- Removing an engineer from a full ticket returns it to `Partially Claimed`
- A disclosure log row is written per claim, with the right customer and recipient
- An engineer with an expired FIN cannot claim

### The race condition — be honest about this

Sequential tests prove the **count check**, not the **lock**. Calling `claim_ticket` twice in a row will pass whether or not `FOR UPDATE` is present, because there's no concurrency.

Genuinely testing the lock needs two threads on separate database connections hitting the method simultaneously. That's worth writing once — but if it proves flaky in CI, the fallback is to verify the lock by reading the SQL and reasoning about it, and say so plainly rather than pretending a sequential test covers it.

A test that appears to cover concurrency but doesn't is worse than no test, because it stops anyone looking.

---

## 6. Manual verification

Two browsers, two engineer users authorised for the same customer.

- Raise a ticket with `engineers_required = 2`
- Claim as engineer A → status `Partially Claimed`, A is Primary
- Confirm engineer A's details reached the customer, and a disclosure log row exists
- Claim as engineer B → status `Claimed`, B is Support
- Attempt a third claim → rejected
- Log in as an engineer *not* authorised for that customer and try the ticket URL directly → denied
- Remove A as an admin → B becomes Primary, status returns to `Partially Claimed`

That last one and the direct-URL check are the two worth doing by hand even though tests cover them.

---

## Deferred

- **Phase 3:** SLA clocks, escalation on unfilled P1s, severity change history
- **Phase 5:** Telegram alerts on new pool tickets, the notification templates behind disclosure
- **v2:** portal-based disclosure replacing email

## Open with Authcor

Can `engineers_required` be revised downward after engineers have claimed — and if so, who gets dropped? Currently unspecified. Simplest defensible rule: it cannot be reduced below the number already assigned; an admin must remove engineers first.