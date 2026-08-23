# Phase 5 — Notifications & Escalation

Email for everyone, Telegram for engineers, WhatsApp deferred to v2 pending Authcor's decision on WABA ownership and cost.

---

## 1. Channel abstraction — build this first

Every notification answers three questions, and they should be answered in three separate places:

| Question | Answered by |
|---|---|
| **When** does this fire? | Document event hooks |
| **Who** should hear about it? | A recipient resolver per event |
| **How** does it reach them? | The channel layer |

Keeping these apart is what makes WhatsApp a later addition rather than a rewrite. When it lands, only the channel layer changes — the events and recipients are already settled.

```
notify(event_key, doc, context) 
    → resolve recipients for this event
    → for each recipient, pick their channels
    → render per channel
    → enqueue delivery
    → log the attempt
```

**Deliver in the background, never inline.** `frappe.enqueue` the send. A slow SMTP server must not hold up a ticket save, and a failed send must not roll back the transaction that triggered it.

**Fire after the transaction commits, not during validation.** Notifications belong in `on_update` / `after_insert`, gated on `has_value_changed("status")` where relevant. Sending from `validate()` means emailing about a change that might not save.

---

## 2. AC Notification Log

Naming: **By Naming Series** → `AC-NOT-.YYYY.-.#####`

| Fieldname | Label | Type | Options |
|---|---|---|---|
| `event_key` | Event | Data | |
| `reference_doctype` | Reference Type | Link → `DocType` | |
| `reference_name` | Reference | Dynamic Link → `reference_doctype` | |
| `channel` | Channel | Select | `Email`<br>`Telegram`<br>`WhatsApp` |
| `recipient` | Recipient | Link → `User` | |
| `recipient_address` | Address | Data | |
| `status` | Status | Select | `Queued`<br>`Sent`<br>`Failed`<br>`Skipped` |
| `error` | Error | Small Text | |
| `sent_on` | Sent On | Datetime | |

Read for Admin L1/L2 only. All rows written by code.

Three reasons this earns its place: it answers "did the customer get told?" without guessing; it's the dedupe key that stops a double-save sending twice; and when Authcor asks why an engineer missed a P1 alert, it's the difference between an answer and a shrug.

**Dedupe rule:** before sending, check for an existing `Sent` row with the same `event_key` + `reference_name` + `recipient` + `channel`. Skip if found, logging `Skipped`.

---

## 3. Events

### Ticket events

| Event | Recipients | Channels |
|---|---|---|
| `ticket_created` | Ops engineers authorised for that customer + city; customer's Leads & Heads | Email, Telegram |
| `ticket_claimed` | Customer portal users; Leads & Heads | Email |
| `engineer_profile_released` | Customer portal users | Email |
| `slot_confirmed` | Customer portal users; Leads & Heads | Email |
| `ticket_in_progress` | Customer portal users | Email |
| `ticket_completed` | Customer portal users; Leads & Heads | Email |
| `ticket_closed` | Customer portal users; Leads & Heads | Email |
| `severity_escalated` | Assigned engineers; Leads & Heads | Email, Telegram |
| `sla_breached` | Per escalation rules (§5) | Email, Telegram |
| `engineer_removed` | The removed engineer | Email, Telegram |
| `count_request_raised` | Ops L1; Leads & Heads | Email |
| `count_request_decided` | Customer portal users; affected engineers | Email |

**`ticket_closed` carries the work summary** — hours per engineer with a total, and expense claims with amounts. Authcor corrected this from `Completed`: at completion the hours may not be logged, which is exactly what `Closed` guarantees.

No rates, no billable amounts. The permlevel model already enforces this on the record; the template must respect it too — **render from a function that only reads permlevel 0 fields**, rather than passing the whole document to Jinja where a future edit could expose a rate.

**`engineer_profile_released`** is the NRIC/FIN disclosure. It already exists in Phase 2b and already writes to the disclosure log — this phase moves it onto the common notification path without changing what it sends. It becomes a link-only notification in Phase 7 when the portal view lands.

### Shipment events

| Event | Recipients | Channels |
|---|---|---|
| `shipment_created` | Ops for that customer + city; Leads & Heads | Email |
| `shipment_received` | Customer portal users; Leads & Heads | Email |
| `shipment_closed` | Customer portal users | Email |

`shipment_received` includes the receipt photograph. It's the message customers will care about most — confirmation their hardware arrived, with evidence.

### Scheduled

| Event | Trigger | Recipients | Channels |
|---|---|---|---|
| `unlogged_hours` | Daily — ticket `Completed` >24h ago with an assigned engineer who has no submitted time entry | That engineer | Email, Telegram |
| `fin_expiring` | Weekly — engineer's `id_expiry` within 60 days | Engineer; Ops L1 | Email |
| `shipment_overdue` | Daily — still `Open` >3 days past `expected_delivery` | Ops L1 | Email |

`unlogged_hours` is not in Authcor's requirements — it comes from the reference system, and it protects invoiced revenue. Keep the tone light; it's a reminder, not a reprimand.

`fin_expiring` matters because an engineer with a lapsed work pass can't be dispatched. The claim path already blocks it; this gives warning before it becomes a problem.

---

## 4. Recipient resolution

One function per event, returning `User` records. Centralising it means recipient rules can be read in one place rather than inferred from templates.

Three helpers do most of the work:

**`get_customer_contacts(customer)`** — portal users from `AC Customer.portal_users`.

**`get_leads_and_heads(customer)`** — from `AC Customer.leads_and_heads`. Authcor confirmed these are copied on created, claimed, SLA breach, and closed. Applying them more broadly than that risks the noise problem that makes people filter notifications into a folder they never open.

**`get_pool_engineers(customer, city)`** — engineers in `AC Customer.authorised_engineers` with an active profile.

**Never notify the actor.** An engineer who claims a ticket doesn't need an email telling them they claimed it. Filter `frappe.session.user` out of every recipient list.

---

## 5. AC Escalation Rule

Admin-configurable, per doc 1 §6.

| Fieldname | Label | Type | Options |
|---|---|---|---|
| `rule_name` | Rule Name | Data | |
| `trigger` | Trigger | Select | `Unclaimed`<br>`Response SLA Breach`<br>`Onsite SLA Breach`<br>`Unconfirmed` |
| `severity` | Applies To Severity | Select | `Any`<br>`P1`<br>`P2`<br>`P3`<br>`P4` |
| `service_level` | Applies To Service Level | Select | `Any`<br>`Premium`<br>`Standard`<br>`Basic`<br>`P4-Scheduled` |
| `threshold_minutes` | Threshold (min) | Int | |
| `notify_roles` | Notify Roles | Table MultiSelect → role child table | |
| `notify_leads_heads` | Also Notify Leads & Heads | Check | |
| `channels` | Channels | Select | `Email`<br>`Email + Telegram` |
| `is_active` | Active | Check | |

Example: trigger `Unclaimed`, severity `P1`, threshold 15 → notify Operations L1. That's doc 1's stated rule, expressed as data rather than code.

**"Unclaimed" means not fully staffed.** A ticket needing three engineers with one claimed is still an unmet dispatch — check assigned count against `engineers_required`, not against zero.

Evaluated by the existing 15-minute breach job. Each fired rule writes a notification log row, which is also what stops it firing every 15 minutes forever.

---

## 6. Email

**Templates as Jinja files in the app**, under `authcor/templates/emails/`, not as `Email Template` records. They're version-controlled, reviewable in a diff, and deploy with the code. Records in the database drift between environments and can't be code-reviewed.

One base layout plus one template per event. Keep them plain — a table of hours renders reliably; a designed HTML email does not.

Every email needs a plain-text alternative. Some data centre staff read mail on locked-down clients.

**Subject lines carry the ticket number first**: `[AC-SH-2026-00042] Engineer assigned — Reseat failed SFP`. Customers filter and search on these.

Sending uses `frappe.sendmail` with `now=False` so it queues. In dev everything lands in Mailpit.

---

## 7. Telegram

Engineers only. Free, immediate, no template approval — which is why it stays in v1 while WhatsApp waits.

### Onboarding

The Bot API cannot message a user who hasn't started a conversation with the bot. So each engineer needs a one-time link:

1. Engineer opens `AC Engineer Profile` and clicks **Connect Telegram**
2. System generates a single-use token, stores it with a short expiry
3. Engineer is sent to `https://t.me/<bot>?start=<token>`
4. They press Start; Telegram calls the webhook with the token and their `chat_id`
5. The webhook validates the token, writes `telegram_chat_id`, burns the token

**The webhook is `allow_guest` and therefore internet-facing.** Three things it must do: validate Telegram's secret token header, treat the token as single-use, and never trust any field in the payload beyond `chat_id` and the start parameter. This is the only unauthenticated endpoint in the application — worth reading carefully rather than accepting on trust.

### Configuration

`AC Notification Settings` — Single DocType.

| Fieldname | Type | Notes |
|---|---|---|
| `telegram_enabled` | Check | Master switch |
| `telegram_bot_token` | **Password** | Encrypted at rest |
| `telegram_webhook_secret` | **Password** | Encrypted at rest |
| `notifications_enabled` | Check | Global kill switch |

`Password` fieldtype for both secrets — never in `site_config.json`, never in the repo.

**`notifications_enabled` matters more than it looks.** A misconfigured rule that starts emailing every customer needs a single switch to stop it, without a deployment.

### Messages

Short. `[AC-SH-2026-00042] P1 unclaimed 15m — Equinix SG3, Meridian Cloud` plus a link. Telegram is a nudge to open the system, not a place to read a report.

---

## 8. WhatsApp — deferred to v2

Authcor leans toward WhatsApp replacing Telegram for engineers. Deferred pending their decision on account ownership and cost.

**What it will require**, so the conversation is informed:

- **Template pre-approval.** Every message type needs a template submitted to Meta and approved before it can send. Approval takes hours to days; rejections happen over wording.
- **Template formatting rules.** Body text cannot start or end with a variable and needs static closing text.
- **The 24-hour window.** Outside a customer-initiated conversation, only templates can be sent. Since notifications are all business-initiated, everything is a template.
- **A WABA.** Either Authcor's own with business verification, or via TUS Global as Tech Provider.
- **Per-conversation charges**, unlike Telegram.

**The honest recommendation:** for internal engineer alerts, Telegram does the job free and instantly, while WhatsApp adds approval friction and per-message cost for the same outcome. WhatsApp's real advantage is reaching people who already live in it — which is an argument for *customer* messaging, not engineer alerts.

Worth putting that to Authcor before they commit.

The channel abstraction in §1 means adding it later is a new channel implementation plus templates, not a rework.

---

## 9. Tests

- Each event resolves the documented recipient set
- The acting user is excluded from their own event's recipients
- Dedupe: firing the same event twice sends once and logs `Skipped`
- `ticket_closed` renders hours and expenses but **no rate or billable amount** — the permlevel boundary, in template form
- A failed send logs `Failed` and does not raise into the calling transaction
- `notifications_enabled = 0` suppresses everything
- Escalation rule matching: severity and service level filters, `Any` wildcards
- "Unclaimed" counts partially-staffed tickets as unmet
- An escalation rule fires once, not every 15 minutes
- Telegram onboarding: valid token links the chat, expired token rejected, reused token rejected
- Webhook rejects a request with a bad secret header
- `unlogged_hours` finds the right engineers and skips those who logged

The rate-leak test is the one that matters most. Templates are the easiest place for an internal figure to escape, because Jinja doesn't enforce permlevel — only the code passing data into it does.

---

## 10. Build order

1. Channel abstraction and `AC Notification Log`
2. `AC Notification Settings` with the kill switch
3. Email templates and the recipient resolvers
4. Wire ticket events, then shipment events
5. `AC Escalation Rule` and its evaluation in the existing breach job
6. Scheduled jobs: unlogged hours, FIN expiry, overdue shipments
7. Telegram: settings, onboarding, webhook, send
8. v2: WhatsApp as a third channel

Steps 1–4 are most of the value. If time runs short, stopping after 4 leaves a working email notification system.

---

## Open with Authcor

1. **Should engineers get email as well as Telegram, or Telegram only?** Both risks notification fatigue; Telegram alone risks missed alerts if someone hasn't onboarded.
2. **Escalation rules** — doc 1 gives one example (P1 unclaimed 15 minutes → Team Lead). What's the full set they actually want?
3. **`fin_expiring` at 60 days** — our number, not theirs.
4. **`shipment_overdue` at 3 days** — also ours.
5. **WhatsApp**, per §8.