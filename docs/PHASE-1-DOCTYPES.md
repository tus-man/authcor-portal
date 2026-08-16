# Phase 1 — Masters & Identity

Field-level specification for building in Desk. Build in the order given: each DocType links to ones above it, and Frappe won't let you create a Link field pointing at a DocType that doesn't exist yet.

**Module** for every DocType below: `Authcor`

**Before you start:** confirm developer mode is on, or none of this reaches your repo.
```
bench --site dev.localhost console
frappe.get_system_settings("developer_mode")
```

---

## 1. AC City

Naming: **By fieldname** → `city_name`

| Fieldname | Label | Type | Options | Reqd | List View | Notes |
|---|---|---|---|---|---|---|
| `city_name` | City Name | Data | | ✓ | ✓ | |
| `country` | Country | Link | `Country` | ✓ | ✓ | Frappe core DocType — do not create your own |
| `timezone` | Timezone | Data | | ✓ | ✓ | IANA name, e.g. `Asia/Singapore` |
| `is_active` | Active | Check | | | | Default `1` |

**Timezone must be an IANA name, never an offset.** `Asia/Singapore`, `Europe/Amsterdam`, `America/New_York`. Offsets like `+08:00` break across daylight saving, and the symptom in Phase 3 is SLA deadlines that drift by an hour twice a year — painful to trace back to here.

Add a `validate()` check that rejects anything not in `zoneinfo.available_timezones()`. Cheap now, saves a data-entry bug later.

**Naming caveat:** naming by `city_name` means two cities with the same name collide. If Authcor ever operates in two places called London, name the second one explicitly (`London (CA)`). Not worth engineering around until it happens.

---

## 2. AC Data Center

Naming: **By fieldname** → `data_center_name`

| Fieldname | Label | Type | Options | Reqd | List View |
|---|---|---|---|---|---|
| `data_center_name` | Data Center Name | Data | | ✓ | ✓ |
| `city` | City | Link | `AC City` | ✓ | ✓ |
| `address` | Address | Small Text | | | |
| `communication_email` | Communication Email | Data | `Email` | | |
| `access_notes` | Access Notes | Text | | | |
| `is_active` | Active | Check | | | |

`communication_email` comes from the review meeting — one contact address per data centre.

Setting **Options** to `Email` on a Data field gives you format validation and a mailto link for free. Same trick works for `Phone` and `URL`.

---

## 3. Child tables

All four need **Is Child Table** ticked in DocType settings. Child tables have no naming — they're always owned by a parent row.

### AC Customer Service Area

| Fieldname | Label | Type | Options | Reqd | List View |
|---|---|---|---|---|---|
| `country` | Country | Link | `Country` | ✓ | ✓ |
| `city` | City | Link | `AC City` | ✓ | ✓ |
| `data_center` | Data Center | Link | `AC Data Center` | | ✓ |

Leave `data_center` blank to mean *all* data centres in that city. This table drives the cascading Country → City → Data Center filter on the ticket form in Phase 2.

### AC Customer User

| Fieldname | Label | Type | Options | Reqd | List View |
|---|---|---|---|---|---|
| `user` | User | Link | `User` | ✓ | ✓ |
| `authorizer_level` | Level | Select | `L1 Authorizer`<br>`L2 Authorizer` | ✓ | ✓ |
| `is_active` | Active | Check | | | ✓ |

Select options go one per line in the Options box.

### AC Customer Engineer

| Fieldname | Label | Type | Options | Reqd | List View |
|---|---|---|---|---|---|
| `engineer` | Engineer | Link | `User` | ✓ | ✓ |

Authcor engineers authorised to serve this customer. Drives pool visibility in Phase 2 — an engineer only sees tickets for customers they're listed against.

### AC Customer Lead Head

| Fieldname | Label | Type | Options | Reqd | List View |
|---|---|---|---|---|---|
| `user` | User | Link | `User` | ✓ | ✓ |
| `designation` | Designation | Select | `Lead`<br>`Head` | ✓ | ✓ |

Not roles — Authcor engineers designated as the communication contacts for this customer. They receive notification copies on created, claimed, SLA breach, and closed.

---

## 4. AC Customer

Naming: **By fieldname** → `customer_name`

| Fieldname | Label | Type | Options | Reqd | List View |
|---|---|---|---|---|---|
| `customer_name` | Customer Name | Data | | ✓ | ✓ |
| `account_code` | Account Code | Data | | | ✓ |
| `service_level` | Service Level | Select | `Premium`<br>`Standard`<br>`Basic`<br>`P4-Scheduled` | ✓ | ✓ |
| `is_active` | Active | Check | | | ✓ |
| `default_country` | Default Country | Link | `Country` | | |
| `default_city` | Default City | Link | `AC City` | | |
| `service_areas` | Service Areas | Table | `AC Customer Service Area` | | |
| `portal_users` | Portal Users | Table | `AC Customer User` | | |
| `authorised_engineers` | Authorised Engineers | Table | `AC Customer Engineer` | | |
| `leads_and_heads` | Leads & Heads | Table | `AC Customer Lead Head` | | |

`service_level` is the field every SLA lookup depends on. It belongs here, on the customer — not on the ticket. Severity goes on the ticket.

Set `account_code` unique via the field's **Unique** checkbox if Authcor uses codes as identifiers.

Use Section Break and Column Break fields to group these sensibly — the form is long enough to need it. Sections roughly: Details / Defaults / Service Areas / People.

---

## 5. AC Engineer Profile

Naming: **By fieldname** → `user`

| Fieldname | Label | Type | Options | Reqd | List View | Permlevel |
|---|---|---|---|---|---|---|
| `user` | User | Link | `User` | ✓ | ✓ | 0 |
| `employee_id` | Employee ID | Data | | | ✓ | 0 |
| `phone` | Phone | Data | `Phone` | ✓ | | 0 |
| `telegram_chat_id` | Telegram Chat ID | Data | | | | 0 |
| `is_active` | Active | Check | | | ✓ | 0 |
| `id_type` | ID Type | Select | `NRIC`<br>`FIN` | ✓ | | **1** |
| `id_number` | ID Number | **Password** | | ✓ | | **1** |
| `id_expiry` | ID Expiry | Date | | | | **1** |

**Tick Unique on `user`** — one profile per engineer.

### The three sensitive fields

`id_number` uses the **Password** fieldtype. In Frappe this stores the value encrypted in a separate auth table rather than in plaintext in `tabAC Engineer Profile`. It is not a password — the fieldtype is simply Frappe's mechanism for encrypted-at-rest storage. Read it back with `get_decrypted_password()`.

**Permlevel 1** on all three. Permlevel is Frappe's field-level permission tier: fields at level 0 are visible to anyone with read access on the document; level 1 requires an explicit grant in Role Permissions Manager. Set it in the field's Properties panel.

Set both from the moment the fields exist. Never store an NRIC in plaintext, even in dev — the habit is what matters.

**`id_expiry` applies to FIN only.** NRICs don't expire; FINs do, because they're tied to a work pass. Add a `validate()` rule requiring expiry when `id_type` is FIN. In Phase 5 this feeds a scheduled check that warns before a pass lapses, since an engineer with an expired FIN can't be dispatched.

See `docs/PLAN.md` §9 before writing anything that reads or transmits this field.

---

## 6. Roles

Create eight Role records at `/app/role/new`. The critical setting is **Desk Access**:

| Role | Desk Access |
|---|---|
| Client L1 Authorizer | ✗ |
| Client L2 Authorizer | ✗ |
| Operations L1 Authorizer | ✓ |
| Operations L2 Authorizer | ✓ |
| Operations L3 Authorizer | ✓ |
| Admin L1 | ✓ |
| Admin L2 | ✓ |
| Admin L3 | ✓ |

**Client roles must not have Desk access.** Users without it are *Website Users* — they can reach portal pages but not `/app`. Authcor's customers should never see Desk, and this checkbox is the mechanism. It also keeps them out of the System User seat count, which matters if Authcor ever moves to Frappe Cloud.

Two things this doesn't cover, by design. **Lead and Head are not roles** — they're the child table on AC Customer. And the **L1/L2/L3 naming is confusing**: L1 means most senior on the Operations and Admin side but primary contact on the Client side, while L3 is the most junior engineer. Authcor signed off on keeping it; expect to explain it to their staff.

---

## 7. Permissions

Do this only after all DocTypes exist.

**Role permissions** — `/app/role-permission-manager`. Set create/read/write/delete per role per DocType. Grant **permlevel 1** on AC Engineer Profile only to Operations and Admin roles, never to Client roles.

**Customer isolation** — User Permissions, at `/app/user-permission`. For each client portal user, create a User Permission linking them to their AC Customer record. Frappe then filters every DocType with a Customer link field automatically.

Page permission for users at base level read.

### Test isolation adversarially

Role permissions and User Permissions are separate mechanisms, and it is entirely possible for a list view to look correctly filtered while direct record access still works.

1. Create two AC Customers and a portal user for each
2. Log in as customer A's user
3. Confirm the list view shows only A's records
4. **Then type customer B's record URL directly** — `/app/ac-customer/<name of B>`
5. You should get a permission error, not the record

Step 4 is the test that matters. Skipping it is how data isolation bugs reach production.

---

## Closing the phase

```bash
cd /workspace/development/frappe-bench/apps/authcor
git status
```

You should see new folders under `authcor/authcor/doctype/`. If not, developer mode wasn't on and nothing was written to disk.

Commit the DocTypes separately from any controller code, push, and check CI goes green — that proves everything installs cleanly on a fresh site, which is the rehearsal for the VPS deploy.

**Deferred to Phase 3:** `AC Business Hours` and `AC Holiday Calendar`, plus the link fields on AC City that point at them. Adding Link fields to an existing DocType is trivial; the sequencing just keeps this phase focused.