# Phase 7 — Customer Portal

The customer-facing surface. Everything built so far runs in Desk, which clients cannot reach — this is what makes the system usable by Authcor's customers.

Server-rendered Jinja pages in the app, using Frappe's existing portal mechanism. No build step, no separate API surface, and the permission model already built applies directly.

---

## 1. Why Jinja and not a Vue SPA

A Vue frontend would look slicker. It would also mean designing an API for every interaction, learning Frappe UI, and adding a build pipeline — on top of a backend that is already finished.

Jinja portal pages reuse what exists. A page template reads documents through the ordinary ORM, so User Permissions, permlevel and the visibility hooks all apply without a line of new permission code. Forms post to whitelisted methods that already exist.

The cost is a plainer interface and full page loads on navigation. For a B2B portal where customers raise a handful of tickets a month, that is the right trade.

---

## 2. Routing and access

Portal pages live in `authcor/www/portal/`. Frappe maps the directory to URLs automatically: `authcor/www/portal/tickets.html` serves `/portal/tickets`.

Each page is a `.html` template plus a `.py` file exporting `get_context(context)`.

### Every page must do three things

```python
def get_context(context):
    context.no_cache = 1
    if frappe.session.user == "Guest":
        raise frappe.Redirect  # with redirect_location set to /login
    customer = get_portal_customer()   # raises if the user isn't a portal user
    ...
```

**`get_portal_customer()`** is the single gate. It resolves `frappe.session.user` to the `AC Customer` they belong to via `portal_users`, and throws `PermissionError` if there is no match. Every page calls it; no page queries by customer name taken from the URL.

That last point matters. **Never take a customer identifier from the request.** The customer is derived from the session, always. A page that accepts `?customer=` is one typo away from being an enumeration tool.

### Detail pages

Fetch by name, then verify ownership:

```python
doc = frappe.get_doc("AC Smart Hands Request", frappe.form_dict.name)
if doc.customer != customer.name:
    raise frappe.PermissionError
```

The User Permission would catch this anyway. Check explicitly regardless — defence in depth, and it makes the intent readable.

### Website Settings

Set the portal home for Website Users to `/portal` so sign-in lands them in the right place.

---

## 3. Layout and brand

One base template, `authcor/templates/portal_base.html`, extended by every page.

### Palette

| Token | Hex | Use |
|---|---|---|
| `--brand-red` | `#ED1C24` | Logo, accents, active nav |
| `--action-red` | `#C41018` | Buttons, links — 6.1:1 on white |
| `--deep-red` | `#B3121A` | Hover |
| `--ink` | `#1A1A1A` | Body text |
| `--grey` | `#6B7280` | Secondary text |
| `--border` | `#E5E7EB` | Rules, card edges |
| `--surface` | `#F9FAFB` | Page background |

The logo red is `#ED1C24` — sampled from the file, not the `#ef4444` initially supplied, which is a different (softer) red. White text on the logo red gives 4.38:1, just under the accessibility threshold, which is why buttons use `#C41018` instead.

Dark header carrying the white-and-red logo, white content cards on the grey surface, red reserved for actions.

### Status colours

Status badges need their own muted palette so they don't fight the brand red:

| Status | Treatment |
|---|---|
| Open, Partially Claimed | Amber — awaiting action |
| Claimed, Scheduled | Blue — in hand |
| In Progress | Brand red — active now |
| Completed, Closed | Green |
| Cancelled | Grey |

Plain CSS in one stylesheet. No framework.

**Mobile matters here.** Customers check ticket status on a phone. Single-column below 768px, tables collapsing to stacked cards.

---

## 4. Pages

### `/portal` — Dashboard

Open tickets, recent shipments, and a section for anything needing the customer's input: tickets awaiting slot confirmation, tickets where engineer details have been released but no DC access number has been entered, pending engineer count requests.

That third section is the point of the page. Everything else is navigation.

### `/portal/tickets` — List

Filterable by status, sorted newest first, paginated at 20. Columns: number, subject, data centre, severity, status, created.

**No SLA fields.** They are permlevel 1 and must not appear.

### `/portal/tickets/new` — Raise a ticket

Fields: country, city, data centre (cascading, filtered by service area), subject, severity, action required, target equipment, description, attachments, engineers required.

**The preferred-slot grid is pre-populated** with the next seven days, all three bands ticked, exactly as in Desk. Authcor's reasoning stands: a customer who never sees the grid would be offering availability they never chose.

Severity defaults to P4.

Submits to a whitelisted `create_portal_ticket()` that builds the document server-side. **Do not let the browser post arbitrary fields into a new document** — the method accepts a known list and sets `customer` from the session.

### `/portal/tickets/<name>` — Detail

Status and timeline. Assigned engineers by name, with a link to the engineer details page. Hours worked per engineer with a total. Expense claims with amounts and receipts. Proof-of-work images once completed.

Actions available depending on state:

- **Amend preferred slots** while unconfirmed
- **Enter DC access ticket number** once engineer details are released
- **Request engineer count change** — calls the existing `increase_engineer_count` or `create_count_request`

No SLA information anywhere on this page.

### `/portal/tickets/<name>/engineers` — Engineer details

**The NRIC/FIN disclosure view.** Authcor's decision, replacing the email.

Shows each assigned engineer's name, employee ID, phone, email, and ID type plus number — for engineers currently assigned to this ticket only.

Four things this page must do:

1. Verify the ticket belongs to the session's customer
2. Show only currently assigned engineers — a removed engineer's details disappear, which is what resolves the earlier open question about retraction
3. Read the number with `get_decrypted_password`, server-side, rendered into the page — never exposed through an API the browser could call directly
4. **Write an `AC ID Disclosure Log` row per view**, with `channel = Portal`

Point 4 changes the log's meaning: previously one row per send, now one row per view. That is a stronger record, and it is the reason Authcor wanted this moved off email.

A short line on the page explaining the data is provided for data centre access clearance, under PDPA.

### `/portal/shipments` — List

Number, subject, carrier, tracking, expected delivery, status.

### `/portal/shipments/new` — Raise a shipment

Cascading location, carrier, tracking number, expected delivery, RMA flag, warehouse storage, package description, attachments.

Same server-side construction rule as tickets.

### `/portal/shipments/<name>` — Detail

Status, carrier tracking deep link, receipt photograph and actual delivery once received, storage location once stored.

---

## 5. Changing the disclosure email

Once the portal view exists, `disclose_engineer_profile` changes: the email becomes a notification with a link to `/portal/tickets/<name>/engineers`, and no longer contains the ID.

**Both halves ship together.** The email must not point at a page that does not exist, and the page must not exist without the email directing people to it.

Phase 2b was written so this is one function. The disclosure log continues to record the release event; the portal page adds a row per view on top.

---

## 6. Security checklist

The portal is the first part of this system reachable by people outside Authcor. Every item below should be verified by hand, not just by test.

| Risk | Control |
|---|---|
| Customer identifier from the request | Never. Derived from session via `get_portal_customer()` |
| Another customer's ticket by URL | Explicit ownership check on every detail page, plus User Permissions |
| Arbitrary field injection on create | Whitelisted methods accept a known field list; `customer` set server-side |
| SLA data leaking to customers | Permlevel 1 — never referenced in portal templates |
| Rate or billable amount leaking | Render from the permlevel 0 helper, never the whole document |
| NRIC exposed via API | Decrypted server-side in `get_context` only; no whitelisted method returns it |
| Removed engineer still visible | Page reads current assignments on each load |
| XSS from user content | Jinja autoescaping on; never `| safe` on anything a user typed |
| Guest access | Session check on every page |

---

## 7. Tests

- An unauthenticated request to any portal route redirects to login
- A user with no `portal_users` membership gets `PermissionError`
- Customer A's user cannot load customer B's ticket detail by URL
- Customer A's user cannot load customer B's shipment detail by URL
- The ticket list shows only that customer's tickets
- `create_portal_ticket` sets `customer` from the session and ignores a customer supplied in the payload
- `create_portal_ticket` rejects a data centre outside the customer's service areas
- A new portal ticket generates seven preferred-slot rows
- The engineers page shows only currently assigned engineers
- The engineers page writes a disclosure log row with `channel = Portal`
- An engineer removed from a ticket no longer appears on that page
- No portal template references an SLA field — grep the templates as a test
- The ticket detail renders hours but no rate

The cross-customer pair is the one that matters most. It is the same two-mechanism problem as everywhere else, now reachable from the public internet.

---

## 8. Build order

1. `portal_base.html`, the stylesheet, `get_portal_customer()`, and the session guard
2. Tickets list and detail — read-only first
3. Raise a ticket, including the slot grid
4. Ticket actions: slot amendment, DC access number, count change
5. Engineer details page and the disclosure log change
6. Shipments list, detail, create
7. Dashboard
8. The disclosure email change — **ships with step 5**

Steps 1–3 give something demonstrable. Step 5 is the piece Authcor explicitly asked for.

---

## 9. Notes for Authcor

- **Client users are created by Authcor admins in Desk.** There is no self-registration. Confirmed as intended.
- **L1 user management deferred to v2**, with feedback. Admins add client users in Desk meanwhile.
- The portal shows no SLA information at all, per their instruction that performance data stays internal.