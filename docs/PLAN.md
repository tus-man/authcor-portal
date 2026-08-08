# Authcor — Global Remote Hands & Logistics Portal
### Project plan v1 · Frappe Framework · Prepared for TUS Global

---

## 1. What this system is

Authcor dispatch engineers into **third-party data centres worldwide** on behalf of their customers. This is a **dispatch and field-service platform**, not a helpdesk.

Two consequences that shape every decision below:

- The SLA measures **response time** and **onsite arrival time**. There is no resolution SLA.
- Authcor do not own racks or equipment. There is no asset inventory. Location is `Country → City → Data Center`, and equipment is captured as free text on the ticket.

---

## 2. Stack & infrastructure

| Layer | Choice |
|---|---|
| Framework | Frappe Framework (Python) |
| Internal UI | Frappe Desk — auto-generated, no custom frontend in v1 |
| Customer UI | Frappe portal pages (Jinja templates) |
| Database | MariaDB (Frappe default) |
| Host | **New dedicated Contabo VPS** |
| Proxy / process mgmt | Nginx + supervisor (managed by Frappe bench) |
| Local development | frappe_docker |
| Version control | GitHub, private repo |
| App name | `authcor` · DocType prefix `AC` |

**Why a prefix.** DocType names are globally unique per site and become database table names (`tabAC Smart Hands Request`). Frappe's own apps do this — Helpdesk uses `HD Ticket`. Pick `AC` now; renaming later is painful.

**ERPNext is deliberately not installed.** Timesheet and Expense Claim are being custom-built instead. Pulling in ERPNext plus HR to get two DocTypes would add significant RAM overhead and an accounting model that isn't needed — especially with the rate engine deferred to v2.

---

## 3. Locked decisions

| Area | Decision |
|---|---|
| Login | Frappe built-in magic link for all users; password login toggleable per role via a settings DocType; L1 Admin enabled by default |
| Service level | Attribute of the **customer** — Premium / Standard / Basic / P4-Scheduled |
| Severity | Attribute of the **ticket** — P1 / P2 / P3 / P4, defaults to P4 |
| Severity ownership | Customer may set; Authcor Ops may override at any time |
| SLA on escalation | Clock **restarts** from moment of severity change; prior severity and outcome preserved in history |
| Statuses | `Completed` is the terminal state. No separate `Resolved`. |
| Financials | Capture (time + expense) in v1. Rate cards, invoicing, scheduled reports in v2. |
| Customer visibility | Sees hours worked and expense amounts. Never sees rate or computed billable amount. |
| Notifications | Telegram in v1. WhatsApp deferred to v2 pending client cost discussion. |
| Shipment tracking | Carrier deep-links in v1. API integration v2/v3, only if free tier suffices. |
| NRIC / FIN | Encrypted at rest, permission-restricted, access-logged. |
| Dependencies table | Out of scope. |

---

## 4. SLA matrix

Source: review meeting document (embedded image).

| Service level | Coverage | P1 resp/onsite | P2 | P3 | P4 |
|---|---|---|---|---|---|
| Premium | 24/7/365 | 1h / 3h | 2h / 6h | 3h / 24h | 6h response |
| Standard | 24/7/365 | 1h / 4h | 2h / NBD | 4h / 36h | 8h response |
| Basic | Local business hours | 2h / 8h | 4h / NBD | 8h / 48h (business days) | 16h response |
| P4-Scheduled | As requested | — | — | — | NBD response |

**Lookup rule is now uniform.** Every combination of service level and severity resolves to a row. No special-casing.

**P4 carries a response target but no onsite-arrival target.** For scheduled work the onsite obligation is the confirmed slot, not a duration — arrival is measured against the agreed appointment rather than a clock started at creation.

**Basic P4 = 16 hours of business time**, not 16 wall-clock hours. Under a local-business-hours coverage window that is roughly two working days. Same pause-and-resume logic as the rest of the Basic tier.

**Business-hours awareness.** The Basic tier's clock must pause outside local working hours, and `Next Business Day` / `48 hours (business days)` require a holiday calendar. Because deployments are global, business hours and holidays are configured **per city**, and all SLA arithmetic must be timezone-aware. Store timestamps in UTC; display in both data-centre and viewer timezone.

---

## 5. DocType model

### Configuration & masters

| DocType | Notes |
|---|---|
| `Country` | **Frappe core — reuse, do not rebuild** |
| `AC City` | Link to Country. Holds timezone, business hours, holiday calendar |
| `AC Data Center` | Link to City. Address + **communication email** |
| `AC Business Hours` | Weekday start/end per city |
| `AC Holiday Calendar` + `AC Holiday` (child) | Per country/city |
| `AC Customer` | Organisation, service level, prefilled default country/city |
| `AC Customer Service Area` (child) | Country / City / Data Center the customer is authorised for |
| `AC Customer User` (child) | Link to User + L1/L2 Authorizer flag |
| `AC Customer Engineer` (child) | Engineers authorised to serve this customer |
| `AC Customer Lead Head` (child) | Link to User + Lead/Head designation. Drives notification cc |
| `AC Engineer Profile` | User link, employee ID, phone, **NRIC/FIN (encrypted)**, pass expiry, Telegram chat ID |
| `AC Team` | Customer + City → engineer pool mapping |
| `AC SLA Policy` | Service level × severity → response mins, onsite mins, business-hours flag |
| `AC Escalation Rule` | Admin-configurable trigger → recipient |
| `AC Auth Settings` | Single DocType. Role allowlist for password login |

### Transactional

| DocType | Notes |
|---|---|
| `AC Smart Hands Request` | The main ticket. Includes `engineers_required` (Int, default 1) |
| `AC Preferred Slot` (child) | Date + three band checkboxes (morning / afternoon / overnight) |
| `AC Ticket Engineer` (child) | Assigned engineers. Engineer link, `assignment_type` (Primary / Support), claimed_at, added_by |
| `AC Severity History` (child) | Old severity, new severity, changed by, timestamp, SLA outcome at that point |
| `AC Shipment Request` | Cross-linked to Smart Hands, both directions optional |
| `AC Time Entry` | Submittable. Links to ticket + technician |
| `AC Expense Claim` | Mandatory receipt attachment |
| `AC Feedback` | Dual-sided — customer and engineer |

### Scheduling pattern

Three time bands, tiling the full 24 hours:

| Band | Window |
|---|---|
| Morning | 09:00 – 13:00 |
| Afternoon | 13:00 – 18:00 |
| Overnight | 18:00 – 09:00 **next day** |

**On ticket creation**, the system auto-generates seven date rows covering the next 7 days, with **all three bands ticked** on every row. The customer then amends — unticking bands or removing dates that don't suit. This means the common case (customer is flexible) requires zero input, and the constrained case is expressed by subtraction.

**On confirmation**, Ops selects a specific date and time in 30-minute increments. Validation must reject any time that does not fall inside a band the customer left ticked.

**The overnight band crosses midnight, and validation must account for it.** A confirmed time of 07:00 on 10 May sits inside the *9 May* overnight band, not the 10 May one. Resolve the confirmed datetime back to its owning business day before checking the ticked bands — the same 24-hour-day convention used in the shift scheduling project, applied here as a lookup offset.

**Detail to pin down:** whether the seven generated rows start on the creation date or the following day.

### Performance fields (computed and stored on the ticket)

`first_response_at`, `first_response_hours`, `confirmation_hours`, `idle_time_days`, `required_onsite_response`, `actual_onsite_response`, `has_met_sla`.

Store these rather than computing on read — SLA policies change over time, and the historic verdict must stay frozen.

**Idle time** is worth building even though it isn't in Authcor's spec: it measures time the ticket spent waiting on the customer to confirm a slot, and it is how a missed onsite target gets defended when the delay wasn't Authcor's fault.

---

## 6. Roles & permissions

Per requirements document §2B.

| Category | Role | Access |
|---|---|---|
| Client | L1 Authorizer | Primary contact. Manages L2 users, views all company tickets |
| Client | L2 Authorizer | Raises and views company tickets |
| Operations | L1 Authorizer | Multiple teams/cities. Edits time entries, creates internal tickets |
| Operations | L2 Authorizer | Engineer permissions + reassign within team |
| Operations | L3 Authorizer | Views assigned/pool tickets, claims, logs work and expenses |
| Admin | L1 Admin | Global super admin |
| Admin | L2 Admin | Regional lead. All tickets, manages L3 Admins |
| Admin | L3 Admin | Customer-specific lead. Approves Team Manager & Authorizer requests |

**Naming note to raise with Authcor.** `L1` means *most senior* on the Operations and Admin side but *primary contact* on the Client side, while `L3` means the most junior engineer. Their own staff will trip over this. Worth proposing plainer names before it is baked into the permission model.

**Lead and Head are not roles.** They are Authcor engineers designated per customer as the communication contacts, stored as a child table on `AC Customer`. They receive notification copies on ticket created, claimed, SLA breach, and closed.

### Frappe permission mechanics

| Requirement | Mechanism |
|---|---|
| Role-level create/read/write | Role Permissions Manager |
| Customer data isolation | User Permissions on the Customer link field |
| Pool visibility (my team's unclaimed + my assigned) | `permission_query_conditions` hook — custom SQL condition |
| Hide rate/amount from customers | **permlevel** — rate and amount at permlevel 1, granted only to Ops and Admin |
| NRIC/FIN access | permlevel + `Password` fieldtype (encrypts at rest in a separate table) |

---

## 7. Ticket lifecycle

```
New  →  In Pool  →  Partially Claimed  →  Claimed  →  Scheduled  →  In Progress  →  Completed
                                                            ↓
                                                        Cancelled
```

- On creation the ticket enters a pool determined by Customer + City, visible only to engineers authorised for that customer.
- The customer specifies `engineers_required` at creation, defaulting to 1.
- Any authorised engineer may claim while unfilled slots remain. **The first claimer becomes Primary and owns the ticket.** Subsequent claimers join as Support.
- `Partially Claimed` covers the window where some but not all slots are filled. The ticket locks and moves to `Claimed` only when the required count is reached.
- Admins and Operations L1/L2 may add engineers directly, bypassing the claim queue. Only Team Lead, Team Manager, or Admin may remove or swap an assigned engineer.
- Each engineer's profile is released to the customer as they join, so DC access requests can be filed incrementally rather than waiting for a full team.
- The Primary engineer is accountable for completion, though any assigned engineer may upload proof and log time.
- Completion requires at least one before/after proof image. Multiple images supported.

**Slot allocation is a race condition, and multi-engineer makes it harder.** With three slots and five engineers clicking at once, a read-then-write will overfill. Take a row lock on the parent ticket inside the transaction, recount assigned engineers, then insert — so allocation is serialised at the database level rather than in application logic.

**Escalation rules read the fill state, not a boolean.** "P1 unclaimed for 15 minutes" becomes "P1 not *fully staffed* for 15 minutes" — a ticket needing three engineers with one claimed is still an unmet dispatch.

**Customer-facing hours must aggregate per engineer.** Time entries already link technician plus ticket, so the data model handles it; the portal view needs a per-technician breakdown with a total, matching the reference system's layout.

---

## 8. Build order

### Phase 0 — Infrastructure and de-risking
Provision the new VPS, bench install, Nginx, SSL. Local dev environment via frappe_docker. Git repo and app scaffold.

Then, before anything else is built: **prove the login model works.** Magic link for all roles, per-role password toggle, and a break-glass admin account that is exempt **in code, not in configuration**. Test the lockout path on a throwaway site. If this fights back, it is far better to discover it now than after forty DocTypes depend on it.

Also in this phase: reliable transactional SMTP. With magic-link login, email is the single point of failure for all authentication. The VPS default mail setup is not sufficient.

### Phase 1 — Masters and identity
Country (core) → AC City → AC Data Center. Customer with all child tables. Engineer Profile with encrypted NRIC/FIN. All roles created and the permission matrix configured. **Test customer isolation deliberately** — log in as one customer and attempt to reach another's ticket by direct URL.

### Phase 2 — Core ticket
Smart Hands Request and preferred slots. Cascading Country → City → Data Center filtered by the customer's service area. Status workflow. Pool and claim, with the race condition handled. Multi-image proof of work, mandatory at completion.

### Phase 3 — SLA engine
Business hours, holiday calendars, SLA policies. Computed performance fields. Severity change triggering clock restart with history preserved. Scheduled job for breach detection.

### Phase 4 — Time and expense capture
Time Entry (submittable, technician auto-fetched from session). Expense Claim with mandatory receipt. permlevel configuration hiding rate and amount from the client.

### Phase 5 — Notifications
Email templates: created, claimed, engineer profile release, slot confirmed, SLA breach, completed. Lead/Head copied per the rules above. Telegram bot plus one-time engineer onboarding to capture chat IDs. Escalation rules and their scheduled job. Plus the "you haven't logged your hours" nudge — cheap to build, and it directly protects invoiced revenue.

### Phase 6 — Shipment requests
Shipment DocType, cross-link to Smart Hands, delivery address, carrier list (local courier and hand carry removed, Others with a details field added), summary table, carrier deep-links.

### Phase 7 — Customer portal
Portal pages for submission and ticket viewing. Multiple file attachments below description. Prefilled country and city.

### Phase 8 — Feedback and hardening
Dual-sided feedback. Security review. UAT with Authcor.

### Deferred to v2
Rate cards and rate resolution · billable amount calculation · weekly/monthly CSV and PDF reports · live carrier tracking · WhatsApp notifications · Team Manager time-entry edits with L3 Admin approval and audit log.

---

## 9. Security and compliance register

| Item | Handling |
|---|---|
| NRIC / FIN | PDPA-restricted. Encrypted at rest via `Password` fieldtype. permlevel-restricted. Access logged. Written purpose statement at collection. Engineer consent recorded. |
| FIN expiry | Only FINs expire, not NRICs. Scheduled check should alert before a work pass lapses — an engineer cannot be dispatched without a valid one. |
| Engineer profile release | **Confirmed: NRIC/FIN is disclosed to the customer** so they can file the DC access request. Email must carry a link only — the number itself is displayed in an authenticated portal view scoped to that ticket and that customer, never in the email body. Every view is logged (who, when, which engineer). |
| Disclosure scope | Just-in-time and minimal: released only on claim, only to the customer on that ticket, only for engineers actually assigned. |
| Engineer consent | Recorded at onboarding, with a written purpose statement covering disclosure to customers for data-centre access clearance. |
| Magic link tokens | Single-use, short expiry, rate-limited per email address. |
| Break-glass admin | Exempt from role password rules in code. Credentials in a password manager, never in the repo. |
| Customer isolation | Explicitly tested, not assumed. This is the core security requirement in the brief. |
| Secrets | Environment variables / `site_config.json`. Never committed. |

---

## 10. Open items for Authcor

All four earlier questions are now closed: P4 targets defined, statuses signed off, NRIC/FIN disclosure confirmed, role naming kept as-is.

Remaining, all minor:

1. Do the seven auto-generated slot rows begin on the ticket creation date or the following day?
2. Does the Basic tier's 16-hour P4 response target pause outside local business hours, consistent with the rest of that tier?
3. Once a customer has raised a ticket for three engineers, can Ops revise `engineers_required` downward after some have already claimed — and if so, who gets dropped?
4. PDPA sign-off on the disclosure design in §9 from whoever owns data protection at Authcor.