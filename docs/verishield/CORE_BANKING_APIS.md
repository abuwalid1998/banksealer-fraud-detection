# Core banking APIs – IT Admin guide

Everything VeriShield does **on** the bank's core systems – release or reject a held payment, freeze a card,
reverse a transaction, block an account, read a balance – goes through operations that the IT Admin configures in
**Administration › Core Banking APIs**. No code changes are needed when the bank changes its core system or its API.

```
 VeriShield flow / AML user            Core Banking APIs registry                        Bank core system
 ────────────────────────────          ───────────────────────────────────────           ─────────────────
 held payment: release / reject  ─►    purpose RELEASE_PAYMENT / REJECT_PAYMENT ─┐
 customer says "not me" (card)   ─►    purpose FREEZE_CARD                       ├─►  operation (API)  ─► HTTPS ─►  T24 / Flexcube /
 AML user: "Core banking action" ─►    purpose REVERSE_TRANSACTION … (approval)  │    + connection                  card system …
                                                                                  └─► call log (masked)
```

## 1. Connections

A connection is *where* and *how* VeriShield reaches a system.

| Setting | Notes |
|---|---|
| Base URL | `https://core.bank.local/api/v1`. `mock://core` is a built-in simulator for demos and template tests. |
| Authentication | **API key** (header and optional prefix), **Bearer token**, **Basic** (username + password), **OAuth2 client credentials** (token URL, client id, scope – tokens are cached until they expire), **HMAC signature** (`HMAC-SHA256(secret, timestamp + "." + body)` in a signature header, with a timestamp header). |
| Secret | Set with the *Secret* button; stored encrypted in the vault and never shown again. |
| Mutual TLS | Client certificate and key files on the server (PEM). |
| Certificates | Bank CA bundle file; certificate verification can be switched off only for test systems (shown as a warning). |
| Timeout / retries | Retries on network errors and HTTP 5xx, with the **same idempotency key** on every attempt. |
| Default headers | Sent with every operation of the connection (`name = value`, templates allowed). |

*Test* checks that the system answers (and that an OAuth2 token can be obtained).

## 2. Purposes

A purpose says what an operation is **for**. VeriShield's own flows use the built-in purposes:

| Purpose | Used by VeriShield | Default approval |
|---|---|---|
| `RELEASE_PAYMENT`, `REJECT_PAYMENT` | release / reject of a held payment (analyst, manager, customer confirmation) | – |
| `FREEZE_CARD`, `UNFREEZE_CARD` | customer denied a card payment, customer / AML card freeze | – |
| `BLOCK_CARD_PERMANENT` | manual (lost / stolen) | four eyes |
| `REVERSE_TRANSACTION` | manual rollback / recall of an executed transaction | four eyes |
| `BLOCK_ACCOUNT`, `UNBLOCK_ACCOUNT`, `PLACE_HOLD` | manual | four eyes |
| `BLOCK_DIGITAL_BANKING` | manual | – |
| `GET_TRANSACTION_STATUS`, `GET_BALANCE` | inquiries | – |

Built-in purposes can be edited (label, description, inputs, defaults) and reset; the bank can add its own purposes
(e.g. `NOTIFY_BRANCH`) with their own inputs.

## 3. Operations (APIs)

An operation is *one* API call for a purpose.

| Setting | Example |
|---|---|
| Purpose, connection, method | `REJECT_PAYMENT`, *T24 production*, `POST` |
| Path | `/payments/{{transaction_id}}/reject` (placeholders are URL-encoded) |
| Query / headers | `branch = {{branch\|default:HQ}}`, `X-Requested-By = {{requested_by}}` |
| Body type | JSON, FORM (JSON object → form fields), XML (text template, values XML-escaped), NONE |
| Body template | `{"paymentId": "{{transaction_id}}", "amountMinor": "{{amount\|cents}}", "comment": "{{reason}}"}` |
| Inputs | `name \| label \| required/optional \| example` – VeriShield fills transaction, customer, card, amount and currency fields from its records |
| Output mapping | `reference = result.ref`, `status = result.code`, `message = error.text` (JSON paths like `items[0].id`, XML paths like `Body/Response/Ref`) |
| Success rule | HTTP codes (e.g. 200, 201) **and** optionally a mapped field equal to one of the values (e.g. `status` = `DONE`, `OK`) |
| Idempotency header | `Idempotency-Key` – value `VS-<call id>`, identical on retries |
| Approval / roles | manual requests need a second person; roles allowed to request it |
| Workspaces | *Live* and/or *Demo*; several operations may serve one purpose – the lowest **priority** number wins |

### Template placeholders

Inputs plus: `idempotency_key`, `request_id`, `institution`, `workspace`, `requested_by`, `approved_by`, `now`,
`purpose`. Filters: `cents`, `int`, `float`, `str`, `upper`, `lower`, `date`, `last4`, `digits`, `default:<value>`.
In a JSON body a string that is exactly one placeholder (`"{{amount}}"`) keeps the value's type.

### Testing

*Test* opens a panel with sample inputs: **Dry run** shows the exact request (masked) without sending anything;
**Send for real** calls the system (use a test system or test data). Saving an operation creates a new version.

## 4. Requests, approvals and the call log

* AML Managers and the Chief AML request operations from a transaction or customer (*Core banking action*) or
  from the *Request an operation* tab. Operations marked for approval wait in *Approvals* until **another person**
  approves; the request is then executed.
* After success VeriShield updates its own records: a reversal marks the transaction `REVERSED`; account blocks and
  digital-banking suspensions are noted on the customer; card controls record the core's reference.
* Every call is logged with the masked request, HTTP status, duration, attempts, mapped result and the people
  involved, and is written to the audit trail (and to the SIEM when forwarding is on). A failed release / reject
  opens an operations incident.

## 5. Going live

1. Add the production connection, store its secret, *Test* it.
2. *Copy* the example operation for each purpose you use, point it to the new connection, adapt path, body, output
   mapping and success rule to the bank's API specification.
3. Dry-run, then send for real against the core's test environment.
4. Tick **Use in the Live workspace** (the examples on `mock://core` are Demo only).
