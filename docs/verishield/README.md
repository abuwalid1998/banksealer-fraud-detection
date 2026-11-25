# VeriShield – implementation guide

*Transaction Intelligence. Trusted Decisions.*

VeriShield is the browser-based enterprise platform built on the BankSealer detection engine in this
repository. It implements the [Business Requirements Document](VERISHIELD_BUSINESS_REQUIREMENTS.md)
and the [solution & brand direction](VERISHIELD_SOLUTION_AND_BRANDING.pdf) as a single, easy-to-host
Python service: REST API + web UI + scoring engine, SQLite by default and PostgreSQL for production.

![Manager overview](screenshots/manager-overview.png)

---

## 1. Run it locally (2 minutes)

Requirements: Python 3.10+ (3.11–3.13 tested). No Node, no Qt, no database server.

```bash
./start.sh                 # macOS / Linux – creates .venv, installs, starts
start.bat                  # Windows
```

or manually:

```bash
pip install -r requirements-verishield.txt
python run_verishield.py   # http://localhost:8000   (API docs: /api/docs)
```

Options: `python run_verishield.py --port 9000`, `--host 0.0.0.0` (reach it from other machines),
`--no-demo` (no synthetic data). Data lives in `data/verishield/` (delete that folder for a clean start).

On the first start VeriShield creates the **Bank of Palestine** tenant, the bootstrap IT Admin, the four
other initial accounts and the **Demo workspace** (≈180 customers, 45 days of history, trained models,
recent decisions, alerts and sample cases). This takes a few seconds.

### Live data channels (also in Demo Mode)

Demo Mode prepares customers (KYC), a 45-day historical import and real models trained on it – **nothing is
pre-scored**. Channels are created tested but stopped; the **IT Admin starts them** in *Integrations & Channels*.
Every transaction a running channel delivers is scored by the engine, AML indicators and rules, stored, and written
to the **fraud engine results database**. Dashboards, lists, alerts and counters refresh on their own.

| Channel | How to send data |
|---|---|
| Payment gateway simulator | Realistic Palestinian retail / business activity (ILS, Sat–Thu working week, salary week, Jawwal / Ooredoo, PS IBANs with bank codes, named beneficiaries and purposes, cities, devices, ~3 % fraud scenarios). Streams from yesterday 00:00, catches up at 60× (`VERISHIELD_DEMO_STREAM_SPEED`) and then continues in real time – timestamps are never in the future |
| TCP JSON-lines feed | one JSON transaction per line to `127.0.0.1:9009` |
| HTTP webhook listener | `POST http://127.0.0.1:9010/transactions` with a JSON object or list |
| CSV drop file | append rows to `data/verishield/incoming/transactions.csv` |
| Fraud engine results database | outbound: writes each decision (scores, risk, AML signal, reasons, model / policy versions) to any SQLAlchemy database, e.g. the bank's PostgreSQL |
| Decision / events API | create an API key in *Users & Settings › API clients*, then call `/api/v1/decisions/evaluate` |

Feed extra traffic from a second terminal (start the TCP / HTTP channel first):

```bash
python mock_channel/generate_transactions.py --verishield --output tcp --port 9009
```

The primary fraud engine database (SQLite or PostgreSQL) is shown and can be changed (test → save → restart) under
*Integrations & Channels › Fraud engine database*. Only outbound payment actions are simulated in the Demo workspace.

### Sign-in accounts (test / pilot)

| Username | Password | Role |
|---|---|---|
| `1000` | `Admin` | IT Admin |
| `2000` | `Manager#2026` | AML Manager |
| `3001` | `Analyst1#2026` | AML Employee 1 |
| `3002` | `Analyst2#2026` | AML Employee 2 |
| `4000` | `Chief#2026` | Chief AML / Compliance |
| `5000` | `Ceo#2026` | Chief Executive Officer (read-only executive overview and board reports) |

In development / pilot installs these passwords stay valid – no first-login change (`VERISHIELD_DEMO_FIXED_PASSWORDS`,
default on outside production). With `VERISHIELD_ENV=production` only the bootstrap admin is created and every
temporary password must be changed at first sign-in. **Forgot password?** on the sign-in page sends a one-time code by
e-mail or SMS (in development, when no gateway is switched on, the code is shown on screen).

### Core banking APIs (IT Admin)

Every operation VeriShield performs on the bank's core systems - release / reject a held payment, freeze or unfreeze
a card, reverse a transaction, block an account, place a hold, suspend digital banking, read a balance or a
transaction status - is a configurable **operation** with a **purpose**, on a **connection** (URL, API key / bearer /
basic / OAuth2 / HMAC, mutual TLS, retries, idempotency). Request, response mapping and success rule are templates;
manual operations can require a second person's approval; every call is logged and audited. See
[CORE_BANKING_APIS.md](CORE_BANKING_APIS.md).

### What's new in 1.4

Before production
* **High availability** - several application servers share one PostgreSQL database. Every server scores
  decisions; a database lease elects one leader that runs the channels and background jobs and is taken over
  within 30 s if the leader stops. Servers absorb each other's learning and share signed model checkpoints; the
  audit chain is serialised with a row lock. *Operations › Cluster*; deployment guide in `deploy/ha/README.md`.
* **Security hardening** - Content-Security-Policy and further browser security headers, vault key ring with
  rotation and retirement, API-key rotation with a grace period, a scored **security posture** check for the IT
  Admin (*Users & Settings › Security posture*), protected `/metrics`, XML upload protection, URL scheme checks and
  HMAC-signed checkpoints. `deploy/security/scan.py` runs bandit + pip-audit before a release.

Compliance
* **Cash transaction reports (CTR)** - generated for every customer whose cash in or out reaches the reporting
  threshold on a business day, in one operation or aggregated; review, exemption with reason, submission
  reference, goAML-style XML / CSV / PDF export (*Regulatory Reports › Cash transaction reports*).
* **Regulator statistics** - periodic AML statistics return (transactions monitored, decisions, alerts, cases and
  handling time, STR, CTR, screening, confirmations, risk distribution, KYC reviews) on screen and as a PDF report.
* **Customer risk assessment (CRA)** - weighted factors (geography, customer type, activity, products,
  onboarding channel, PEP / sanctions, behaviour, adverse history incl. returned cheques, network risk, tenure),
  minimum levels, onboarding API (`POST /api/v1/customers/risk-assessment`), daily re-assessment, overrides,
  **four-eyes approval of downgrades**, history and an editable model (*Customer Reviews & Risk*).

Detection
* **Cheque fraud** - cheque deposits and own cheques are profiled by the engine; duplicate presentment, cheque-book
  sequence anomalies (stolen books), bounced-cheque drawers, post-dated / stale cheques and kiting are detected;
  returned cheques arrive as `cheque_return` events or are recorded on the *Cheques* page.
* **Network risk scoring** - rings of customers linked by transfer payees, devices and IPs; risk propagated from
  confirmed fraud, STR subjects, watch list and sanctions matches; live **mule-account score** of every payee in
  the decision (*Networks & Mule Accounts*).

Operations & customer experience
* **Monitoring & SIEM** - audit and security events forwarded in order to the SIEM (syslog UDP / TCP / TLS with CEF
  or JSON, or an HTTPS collector such as Splunk HEC), with a cursor so nothing is lost; business and cluster gauges
  on `/metrics`; Grafana dashboard and Prometheus alert rules in `deploy/monitoring/`.
* **In-app confirmation and card freeze** - held payments are confirmed in the bank's mobile app first (signed
  webhook, `POST /api/v1/confirmations/{id}/respond`), with SMS as fallback; a NO on a card payment freezes the card
  (command to the card system, and VeriShield declines the card while frozen); customers can freeze their own
  cards; the AML team releases bank freezes.

### What's new in 1.3

Detection
* **New transaction types** – card payments (POS / e-commerce, merchant, MCC, terminal), cash withdrawals (ATM /
  branch), cash deposits and incoming transfers, with their own profile features, rules (card testing, night-time ATM
  cash-out), AML typologies (rapid pass-through, many new originators) and demo data.
* **Sanctions / PEP screening** – import OFAC SDN, UN and EU consolidated lists or a CSV; fuzzy matching that also
  handles Arabic-script names; customers are screened on import and periodically, payments' beneficiaries before
  execution. Matches become alerts and are resolved as true / false positive.
* **Watch list** – accounts, devices, IP addresses and customers; a watched value holds the payment.
* **Digital-banking login risk** – new device, new country, impossible travel, failed logins and recent password
  resets feed the payment decision (login records arrive on the same channel as payments).
* **Learning from outcomes** – analyst dispositions, customer answers and fraud reports label transactions; a
  supervised model is trained on them and adds its probability to the decision (shadow first, then active).
* **Champion vs challenger** and a **what-if** strictness slider replaying recent decisions under other thresholds.

Investigation and compliance
* **Customer confirmation by SMS** – held payments ask the customer YES / NO; YES releases, NO rejects, watch-lists
  the payee and opens an urgent case. Branch / call-centre staff can record a **customer fraud report**.
* **Case automation** – critical alerts open cases automatically, assignment by least-loaded or round robin, SLA
  escalation; alert grouping and bulk closing.
* **Network graph** of customers linked by shared transfer payees, devices and IP addresses (mule rings) and a
  **customer 360 timeline**.
* **STR / SAR workflow** – draft from the case, approval by the AML Manager or Chief, record of the FFU reference,
  goAML-style XML and branded PDF export.
* **AI assistant** (optional, OFF by default) – case summaries and STR narrative drafts with Claude, identifiers
  masked before sending; without it a narrative is written from the case facts.
* **Periodic customer reviews** – scheduled from the risk rating (6 / 12 / 36 months) or triggered, with checklist,
  assignment and outcome.

Operations
* **Scheduled reports** e-mailed as PDF / Excel; **data-quality** checks per channel; **archiving** of old
  transactions; **value metrics** (fraud prevented, net benefit, cost per alert) and a **risk heat map** for the CEO.
* **Active Directory sign-in** with role mapping from AD groups (local accounts still work as a fallback).
* After upgrading, run *Demo › Reset* once to load the new demo data types.

### What's new in 1.2

* **IT Admin-started channels** and the **results database** writer (above); realistic gateway simulator.
* **Client-branded formal reports** – the institution's logo, name, colours, department and classification; no vendor
  header; document-control table, numbered sections, landscape pages for wide tables, sign-off table. Upload the logo
  in *Users & Settings › Report branding*. New board reports: board overview, transaction activity, customer portfolio.
* **E-mail & SMS** (IT Admin) – the bank's SMTP relay and HTTP SMS gateway, each ON / OFF, secrets in the vault, test
  send, outbox. Used for password codes, new-account credentials, case assignment and critical alerts.
* **Passwords** – forgot-password flow; IT Admin reset with a generated or chosen password, optional forced change,
  delivery by e-mail / SMS.
* **Customer Clusters** (AML Manager, Chief, CEO) – PCA maps of the k-means customer segments and of the engine's
  DBSCAN / CBLOF peer groups, segment profiles, risk mix and the most unusual customers.
* **Models** (IT Admin) – *Health & retraining* (model age, new data, unseen customers, under-trained profiles, score
  PSI drift, intervention drift, analyst feedback, amount drift, segmentation quality → OK / consider / retrain
  recommended, checked every 30 minutes with an operations incident), *Model settings* (profile weights, HBOS,
  temporal window, DBSCAN / CBLOF, k range), retrain with a training window and shadow / activate, and *How it was
  built* for every version (hyperparameters, data, features).
* **CEO role** (`5000`) with an institution-wide executive overview.
* **AI Agents (MCP)** – ON / OFF, allowed tools, access keys, a built-in HTTP endpoint `/mcp` (plus the stdio bridge),
  copy-ready Claude configuration and an audit of every agent call.

### A 10-minute tour

0. **1000 (IT Admin)** → *Integrations & Channels* → **Start streaming** (nothing is scored until a channel runs).
1. **2000 (Manager)** → *Overview*: alerts, held payments, analyst workload, 14-day outcomes.
2. *Alerts* → tick an alert → **Create case from selected** → assign to an analyst.
3. *Decision Console* → **Fill a risky example** → *Evaluate*; or inject a demo scenario
   (account takeover, velocity burst, structuring, shared counterparty …).
4. **3001 (Analyst)** in another browser/private window → *AML Cases* → start investigation, add notes /
   evidence / checklist → **Submit recommendation**.
5. Back as **2000** → open the case → **Record disposition** (e.g. *Release transaction*: in Demo Mode the
   bank is simulated) → *Investigation report* (branded PDF).
6. *Policies & Thresholds* → **New draft** → change a threshold → **Simulate impact** → **Publish**.
7. **1000 (IT Admin)** → *Integrations & Channels* → **Start streaming** on the payment gateway simulator and
   **Start writing results** on the results database; watch the counters move. *Users & Settings*: users, security policy, branding & logo, API keys,
   Demo Mode reset. *Operations & Audit*: health, incidents, hash-chained audit trail.
8. **4000 (Chief)** → executive oversight dashboard and reports. **5000 (CEO)** → executive overview, *Customer
   Clusters*, board reports.

Run the tests: `python -m pytest tests -q` (the VeriShield suite also runs against PostgreSQL with
`VERISHIELD_TEST_DATABASE_URL=postgresql+psycopg://user@host/db`).

---

## 2. Host it

| Option | Command |
|---|---|
| Docker (SQLite volume) | `docker compose up --build` → http://localhost:8000 |
| Docker + PostgreSQL | `docker compose -f docker-compose.postgres.yml up --build` |
| Single container | `docker build -t verishield . && docker run -p 8000:8000 -v vsdata:/data verishield` |
| Render.com | New → Blueprint → this repo/branch (uses `render.yaml`: Docker + managed PostgreSQL) |
| Any VM / PaaS | `pip install -r requirements-verishield.txt -r requirements-postgres.txt` then `python run_verishield.py` (honours `PORT`) |

Configuration is by environment variables – see [`.env.example`](../../.env.example). For a shared/hosted
instance set at least:

```
VERISHIELD_SECRET_KEY=<long random value, keep stable>   # signs sessions, encrypts the secrets vault
DATABASE_URL=postgresql://user:pass@host:5432/verishield  # recommended
VERISHIELD_ENV=production                                # hides dev accounts, requires MFA, binds 0.0.0.0
VERISHIELD_BOOTSTRAP_PASSWORD=<one-time value>           # instead of the default "Admin"
```

Put the service behind TLS (reverse proxy / platform load balancer). Health: `/health`, readiness:
`/ready`, Prometheus metrics: `/metrics`. Upgrades are forward-only and **never clear data** on start.

CLI helpers: `python -m verishield init | reset-demo | create-tenant CODE "Name" | create-api-key NAME
--scopes decisions:write,events:write --space LIVE | verify-audit`.

---

## 3. Architecture

```
web UI (vanilla JS SPA, no build)      MCP server (optional, stdio)      bank systems (API keys)
            \                                   |                               /
             +------------------- FastAPI  /api/v1  (OpenAPI at /api/docs) ---+
             |  auth · RBAC · tenant + space scoping · rate limits · audit (hash chain)
             |
             +-- decision orchestrator ── policy engine (versioned thresholds, rules, actions, fallback)
             |        |                └─ AML indicators (structuring, velocity, turnover, shared counterparty…)
             |        └── BankSealer engine (local HBOS · temporal · global DBSCAN/CBLOF) per tenant/space
             +-- cases & alerts · customers & segmentation (k-means cold start) · reports (PDF/CSV/XLSX)
             +-- connectors runtime (REST pull, TCP, HTTP listener, file drop, DB replica, demo stream)
             |
          SQLAlchemy ── SQLite (default) | PostgreSQL ;  data dir: models, evidence, reports, logo
```

| Package | Purpose |
|---|---|
| `verishield/app.py`, `api/` | FastAPI app, routers (`auth`, `admin`, `core`, `cases`, `governance`) |
| `verishield/models.py` | Data model (BRD §16): tenants, users, sessions, API clients, vault, connectors, dead letters, customers, segments, model versions, transactions, decisions, execution events, alerts, cases (links, events, notes, evidence), policy versions, report jobs, audit, incidents |
| `services/decision.py` | Synchronous decision + async ingestion, idempotency, reason codes, execution lifecycle, bank commands |
| `services/engine_registry.py` | BankSealer engine behind a service boundary; model registry, activate / shadow / rollback |
| `services/policy.py` | Policy documents, validation, rule evaluation, simulation |
| `services/aml.py` | AML unusual-activity indicators (kept separate from the fraud score) |
| `services/segmentation.py` | Data-quality report, onboarding segmentation, cold-start assignment |
| `services/cases.py` | Alert triage and case state machine |
| `services/connectors.py` | Connector registry, tests, mapping preview, secrets, runtime |
| `services/reports.py` | Report catalogue + tenant-branded PDF/CSV/XLSX |
| `services/demo.py` | Demo Mode seed, scenarios, stream, reset |
| `mcp_server.py` | Scoped MCP tools calling the same REST API |
| `web/` | Single-page UI (brand palette: Deep Navy, Teal, Signal Blue, Review Amber, Critical Red, Mist) |

**Tenants and spaces.** Every record carries `tenant_id` (always taken from the authenticated
credential). Operational records also carry `space` = `LIVE` or `DEMO`. Demo Mode switches the users'
workspace to the DEMO namespace; API keys are bound to the space they were created in, so toggling Demo
Mode never redirects a bank integration. Live channels run in both workspaces and score in real time; in the
Demo workspace only outbound payment actions are simulated. Demo reset deletes only DEMO rows and artifacts.

---

## 4. Decisions and the bank integration

```
POST /api/v1/decisions/evaluate          X-API-Key (scope decisions:write)
{"transaction_id":"TX-1","idempotency_key":"TX-1-v1","customer_id":"C00012",
 "transaction_type":"bank_transfer","amount":1250,"currency":"ILS","channel":"mobile_banking",
 "beneficiary_reference":"PS92…","beneficiary_country":"PS","ip_address":"213.6.10.20","ip_country":"PS"}
→ {"decision":"HOLD","execution_required":true,"fraud_score":0.82,"risk_level":"HIGH",
   "aml_signal_level":"ELEVATED","reason_codes":["NEW_BENEFICIARY","VELOCITY_ABOVE_PROFILE"],
   "policy_version":"BOP-POLICY-03","fraud_model_version":"BOP-FRAUD-02","expires_at":"…","correlation_id":"…"}
```

* Outcomes: **PASS / REVIEW / HOLD / DECLINE**, or **ERROR_POLICY** with the institution's fallback
  (`FAIL_OPEN`, `FAIL_CLOSED`, `FALLBACK_HOLD`) per product – never a silent fallback; an incident opens.
* Same idempotency key → same stored decision; same transaction id with another key → `409`.
* The bank reports reality via `POST /api/v1/transactions/{id}/execution-result`
  (`HELD`, `AUTHORISED`, `DECLINED`, `SETTLED`, `RECALLED`, `EXPIRED`); invalid transitions → `409`.
* Manager release/reject goes through an enabled *Bank execution adapter* connector (outbound webhook);
  in Demo Mode a simulator applies it.
* `POST /api/v1/transactions/events` (scope `events:write`) ingests batches for post-event monitoring
  (`mode: POST_EVENT`, labelled and never reported as blocked) or history (`mode: HISTORY`).
* Limited-history customers are capped at REVIEW unless a hard rule applies (no adverse decision from a
  segment or from missing history alone).

---

## 5. Requirement coverage (BRD §22)

| Area | Implemented in this branch |
|---|---|
| Deployment & tenants | One-command local start, Docker/Compose/Render, SQLite or PostgreSQL, non-destructive schema upgrade, `create-tenant` CLI, tenant isolation (tested) |
| Users | Bootstrap `1000/Admin` with forced change + profile; IT Admin creates 4–5 digit users with single-use expiring temporary passwords; lockout; role limits (1 Manager, 1 Chief); disable/unlock/reset; TOTP MFA (required in production or by policy); optional e-mail verification (SMTP) |
| RBAC | Backend permission matrix per BRD §6, explicit grantable extras for the Chief, masked IT views, analysts limited to assigned cases and linked context (tested) |
| Connectors | Registry with lifecycle, test connection, masked mapping preview, vault/env secret references, credential-expiry warnings, enable/disable/retry/retire, logs, counters, dead-letter queue with replay |
| Decisions | Synchronous API, idempotency, PASS/REVIEW/HOLD/DECLINE + ERROR_POLICY, versioned policy & model on every decision, execution lifecycle, post-event labelling (tested) |
| Customers & models | Data-quality report, CSV/JSON imports, k-means segmentation with silhouette & stability, cold-start assignment with basis/confidence, fraud model registry with shadow and rollback |
| AML workflow | Alert triage/close, related alerts, cases & referrals, assignment, checklist, notes, evidence with SHA-256 and type/size checks, recommendation, Manager disposition, return/reopen, chronology |
| Policies | Thresholds, actions, AML parameters, scoped rules, fallback, simulation, maker-checker, scheduled effective time, emergency rollback, diff |
| Reports | 14 report types, PDF with tenant logo/colours/header/footer/notice/numbering/watermark/sign-off, CSV, XLSX, role-scoped |
| Demo Mode | Separate Demo namespace with live channels and real-time scoring, banner, report watermark, simulated payment actions, scenario injector, audited reset that never touches LIVE (tested) |
| Operations | `/health`, `/ready`, `/metrics`, latency percentiles, incidents workflow, hash-chained tamper-evident audit with verification (tested) |
| MCP | `python -m verishield.mcp_server` with read-oriented tools via scoped API keys |

### Not included / to be engineered with the institution

These remain target-state items from the BRD and need institution infrastructure or decisions:

* Kafka / RabbitMQ / JMS / SFTP consumers – connectors can be registered, reachability-tested and
  monitored, but a broker/transfer client adapter must be added before enabling them.
* SSO/OIDC, HSM/KMS integration, antivirus scanning of evidence (basic type/size/executable checks only),
  object storage for evidence (local data directory today), automated retention purge.
* Horizontal scaling: model state is held per process – run one application instance per database
  (or add a shared model service) until load tests define the target architecture.
* Load, DR and penetration testing; calibration of thresholds on BOP-approved data. Demo figures are
  synthetic and are not evidence of production detection performance.
