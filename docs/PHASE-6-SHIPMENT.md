# Phase 6 — Shipment Requests

Customers notify Authcor that hardware is being sent to a data centre. Authcor records receipt and, where asked, places it in storage.

---

## 1. Why this is a separate DocType

We considered folding shipments into `AC Smart Hands Request` as an action type. Authcor's description ruled it out: shipments need **no engineers, no claiming, no preferred slots, and no arrival SLA.**

Sharing the ticket DocType would mean every one of those mechanisms carrying an "unless this is a shipment" branch — slot generation, claim buttons, pool visibility, SLA policy application in `before_insert`. Conditional field visibility hides the fields; it doesn't stop the logic running. A separate DocType is simpler and matches the original requirements document.

**Consequence:** `action_required` on the smart hands ticket drops to two options — `Smart Hands` and `Others`. "Shipment and Storage" is removed, since shipments are raised on their own form. Update `docs/CHANGE-SET-POST-DEMO.md` §3 accordingly.

---

## 2. AC Shipment Request

Naming: **By Naming Series** → `AC-SHP-.YYYY.-.#####`

### Customer & destination

| Fieldname | Label | Type | Options | Reqd | Read Only |
|---|---|---|---|---|---|
| `naming_series` | Series | Select | `AC-SHP-.YYYY.-.#####` | | |
| `customer` | Customer | Link | `AC Customer` | ✓ | |
| `country` | Country | Link | `Country` | ✓ | |
| `city` | City | Link | `AC City` | ✓ | |
| `data_center` | Data Center | Link | `AC Data Center` | ✓ | |

Location fields cascade and filter by the customer's service areas, exactly as on the ticket. **Reuse the three existing whitelisted methods** — `get_allowed_countries`, `get_allowed_cities`, `get_allowed_data_centers`. They're already permission-gated and tested; don't write new ones.

### Shipment detail

| Fieldname | Label | Type | Options | Reqd |
|---|---|---|---|---|
| `subject` | Subject | Data | | ✓ |
| `carrier` | Carrier | Select | `DHL`<br>`FedEx`<br>`UPS`<br>`TNT`<br>`Others` | ✓ |
| `carrier_other` | Carrier Name | Data | | |
| `tracking_number` | Tracking Number | Data | | |
| `expected_delivery` | Expected Delivery | Date | ✓ |
| `is_rma` | RMA | Check | | |
| `package_description` | Package Contents | Small Text | | ✓ |
| `package_count` | Number of Packages | Int | | |
| `external_reference` | Customer Reference | Data | | |
| `notes` | Notes | Text Editor | | |
| `attachments` | Attachments | Attach | | |

Carrier list per the review meeting: local courier and hand carry removed, `Others` with a details field added. `carrier_other` is conditionally required on `Others` — Depends On **and** `validate()`, same pattern as `action_other_details`.

`tracking_number` is optional, not required. A customer arranging a shipment may not have it yet, and blocking the record until they do means Authcor learns about the delivery late.

### Storage

| Fieldname | Label | Type | Reqd | Depends On |
|---|---|---|---|---|
| `store_in_warehouse` | Store in Warehouse | Check | | |
| `storage_location` | Storage Location | Data | | `eval:doc.store_in_warehouse` |
| `stored_on` | Stored On | Datetime | | `eval:doc.store_in_warehouse` |

`storage_location` is free text — a shelf or bay reference. Not worth modelling as a DocType unless Authcor asks for warehouse inventory, which is well beyond this scope.

### Receipt & status

| Fieldname | Label | Type | Options | Read Only |
|---|---|---|---|---|
| `status` | Status | Select | `Open`<br>`Received`<br>`Closed`<br>`Cancelled` | ✓ |
| `actual_delivery` | Actual Delivery | Datetime | | ✓ |
| `received_by` | Received By | Link → `AC Engineer Profile` | | |
| `receipt_proof` | Receipt Proof | Table → `AC Proof Image` | | |

Default `status` to `Open`.

`received_by` is editable — like `completed_by` on the ticket, whoever records receipt may not be who physically took the delivery.

**`receipt_proof` reuses the existing `AC Proof Image` child table.** No new DocType needed.

### Cross-link

| Fieldname | Label | Type | Options |
|---|---|---|---|
| `related_ticket` | Related Smart Hands Ticket | Link | `AC Smart Hands Request` |

**One field, on the shipment only.** Frappe's Connections panel shows linked shipments on the ticket side automatically, so there's no reciprocal field to keep in sync — one source of truth, no possibility of the two disagreeing.

This handles the real case: a smart hands job waiting on two separate shipments means two shipment records both pointing at the same ticket, and both appear in the ticket's Connections panel.

Filter the link so it only offers tickets belonging to the same customer.

---

## 3. Lifecycle

```
Open  →  Received  →  Closed
              ↓
          Cancelled
```

| Transition | Who | Gate |
|---|---|---|
| → `Received` | Ops, Admin | Stamps `actual_delivery`; requires at least one `receipt_proof` image |
| → `Closed` | Ops, Admin | Requires `storage_location` if `store_in_warehouse` is ticked |
| → `Cancelled` | Ops, Admin | Any time before `Closed` |

**Warehouse storage is a checkbox, not a state.** Authcor confirmed the flow is Open → Received → Closed regardless; storage is an attribute of what happened, not a separate stage.

**Receipt proof mirrors the smart hands rule** — Authcor wanted a photograph proving the physical work was done, and receiving a package is the equivalent act. *This is our inference from the smart hands requirement, not something Authcor stated for shipments. Flag it for confirmation.*

`stored_on` stamps when `store_in_warehouse` is ticked and the status reaches `Received`.

---

## 4. No SLA

Shipments carry no response or onsite target — nobody is being dispatched, and the matrix has no row for them.

This is the clearest argument for the separate DocType: `AC Smart Hands Request` applies an SLA policy unconditionally in `before_insert` and would need that suppressed for shipments.

`expected_delivery` is the customer's estimate of when the carrier will deliver, not a commitment by Authcor. It should not be presented as a deadline anywhere.

**Possible later addition, not now:** a scheduled check flagging shipments still `Open` several days past `expected_delivery`, so Authcor can chase. Useful, but it's operational tooling rather than a requirement — note it for v2.

---

## 5. Carrier tracking links

Build a deep link from `carrier` + `tracking_number` and render it as a button or link on the form.

The agreed v1 approach: no API, no cost, no integration. One click takes the user to the carrier's own tracking page, which is live and authoritative.

Store the URL patterns in a small map in code rather than on the DocType — they change occasionally and a code change is easier to review than a data edit.

Live status polling remains v2/v3, and only if a free tier proves sufficient.

---

## 6. Permissions

| Role | Read | Write | Create | Delete |
|---|---|---|---|---|
| Client L1/L2 Authorizer | ✓ | | ✓ | |
| Operations L3 | ✓ | ✓ | ✓ | |
| Operations L2 | ✓ | ✓ | ✓ | |
| Operations L1 | ✓ | ✓ | ✓ | |
| Admin L1 | ✓ | ✓ | ✓ | ✓ |
| Admin L2 | ✓ | ✓ | ✓ | |
| Admin L3 | ✓ | ✓ | | |

Clients create and read; they don't edit after submission. If they need a change, Authcor makes it — the same shape as the engineer count request.

**Customer isolation is automatic** — `customer` is a Link field, so the existing User Permissions on `AC Customer` filter this DocType with no extra work. Verify it anyway with the direct-URL test.

**No permlevel fields on this DocType.** Nothing here is internal-only; the customer sees the whole record.

---

## 7. Notifications (Phase 5)

Recorded here so triggers are settled before templates are written:

- **Created** → Ops for that customer's city, plus the customer's Leads and Heads
- **Received** → customer, with the receipt photograph
- **Closed** → customer, with storage location if applicable

The Received notification is the one customers will care about most — it's confirmation their hardware arrived safely, with photographic evidence.

---

## 8. Tests

- Cascading location filters respect the customer's service areas
- `carrier_other` required when carrier is `Others`, rejected otherwise
- `Received` without receipt proof is rejected
- `Closed` with `store_in_warehouse` ticked but no `storage_location` is rejected
- `Closed` without warehouse storage succeeds with no location
- `actual_delivery` stamps on entry to `Received`
- `stored_on` stamps only when the warehouse flag is set
- `related_ticket` cannot be set to another customer's ticket
- Customer isolation: a client user cannot read another customer's shipment, by list **or** direct access
- Carrier deep link builds correctly for each carrier, and is absent when there's no tracking number

The isolation pair is the one that matters. Same two-mechanism check as everywhere else — list filtering and direct record access resolve through different code paths.

---

## 9. Open with Authcor

1. **Receipt photograph mandatory?** Inferred from the smart hands proof-of-work rule, not stated for shipments.
2. **Outbound shipments.** Authcor said shipments always come *to* them. If hardware is ever returned — a failed RMA going back to the vendor — this model doesn't cover it. Worth confirming that's genuinely out of scope rather than unmentioned.
3. **Carrier list.** DHL, FedEx, UPS, TNT, Others is our proposal. The review meeting only said what to remove.
4. **Does a shipment ever become a smart hands job automatically?** Hardware arrives, gets stored, and someone later needs it racked. Currently that's a new ticket the customer raises, cross-linked manually. A "create smart hands request from this shipment" action would be convenient — worth asking whether that reflects how they actually work.