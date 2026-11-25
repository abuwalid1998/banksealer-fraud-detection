# BankSealer Enterprise: architecture and operations

## Services

```
                 ┌───────────────────────── docker compose ─────────────────────────┐
 browser ──8080──▶ frontend (nginx + React console) ──/api──▶ api (FastAPI, 2 workers) │
                 │                                              │  REST + JWT + RBAC   │
 core banking ──8088 (HTTP webhook)──┐                          ▼                      │
 branch feeds ──9009 (TCP JSON-lines)┤                    PostgreSQL 16                │
 files / exports (shared /data) ─────┴─▶ worker ◀──────────▶  configuration database  │
                 │                      channels → preparation → BankSealer engine    │
                 │                      → scores, reasons, PDF reports, profiles      │
 mock-feed (profile "demo") ──HTTP──▶ worker:8088                                      │
                 └──────────────────────────────────────────────────────────────────────┘
```

| Service | Image | Role |
|---------|-------|------|
| `db` | postgres:16-alpine | Configuration database and operational data (volume `pgdata`) |
| `api` | banksealer/backend | REST API (`/api/*`, OpenAPI docs at `/api/docs`), JWT authentication, roles, audit log, on-demand reports |
| `worker` | banksealer/backend | Runs the channels, data preparation, training, scoring, auto-reports and commands |
| `frontend` | banksealer/console | React console served by nginx; proxies `/api` to the api service |
| `mock-feed` | banksealer/backend | Optional (`--profile demo`): sends 45 days of history, then a live stream with injected fraud, to the HTTP webhook |

The api and worker share one image and the `appdata` volume (`/data`), which holds:

* `reports/`: generated PDFs
* `uploads/`: uploaded files
* `incoming/`: drop folder for the CSV and SQLite channels

## Configuration database

The configuration lives in PostgreSQL and is edited from the console or the API. On the very
first start it is seeded from `config/enterprise.yaml`.

| Table | Content |
|-------|---------|
| `settings` | One row per section: `thresholds` (defaults), `engine` (profile weights, learning), `local`, `temporal`, `global` (algorithm parameters), `app` (auto-reports) |
| `banks` | Banks/companies with their threshold **overrides** (only the keys that differ from the defaults) |
| `channels` | Channel type, connector options, **field mapping**, **preparation rules**, enabled flag, version |
| `users` | Console users with role `viewer`, `analyst` or `admin` (PBKDF2 password hashes) |

Operational tables:

* `transactions`, `scores`: reasons, explanation and analyst status
* `customer_profiles`: learned summaries published by the worker
* `preparation_errors`: rejected records with the reason
* `reports`
* `commands`: work requested by the api for the worker
* `engine_status`, `channel_status`: runtime state published by the worker
* `model_blobs`: the trained engine, so a restarted worker resumes immediately
* `audit_log`: every login, configuration change, decision, export and download

### How changes reach the worker

* **Thresholds, engine settings, banks:** the worker computes a configuration version every
  second and hot-reloads when it changes. Saving thresholds or banks also queues a `reevaluate`
  command, so the decision on every scored transaction is recomputed with the new thresholds.
  Profile scores are stored, so nothing is rescored.
* **Channels:** every save increments `channels.version`. The worker stops and restarts that
  channel with the new options, mapping and rules. Enabling or disabling starts or stops it.
* **Algorithm hyper-parameters** (HBOS bins, DBSCAN eps, ...) take effect at the next training:
  *Engine & training › Retrain*.

## Channels and data preparation

| Type | Purpose | Notes |
|------|---------|-------|
| `http` | Webhook: `POST /transactions` with a JSON object, a list, or `{"transactions": [...]}` | Optional bearer `token`. `{"history": true, ...}` or header `X-BankSealer-History: true` marks training data |
| `tcp` | JSON-lines stream | A line with `{"history": true, "transactions": [...]}` is training data |
| `csv` | CSV or `.jsonl` file in `/data/incoming`, followed like `tail -f` | |
| `sqlite` | Polls a table for new rows | |
| `mock` | In-process simulator | |

Each channel runs its own preparation pipeline:

1. **Field mapping**: source field → BankSealer field. Unmapped fields go through aliases
   (`customer_id`, `beneficiary_iban`, `ip_address`, ...).
2. **Defaults**: constant values for missing fields.
3. **Parsing**: timestamps (auto, or the channel's `timestamp_format`), amounts (`1.234,50 EUR`,
   times `amount_scale` for cents).
4. **Type normalisation**: synonyms plus the channel's `type_mapping`.
5. **Validation**: required fields, `min_amount`/`max_amount`, `allowed_types`, IBAN for transfers.
6. **Enrichment**: IBAN normalisation and country, `cc_asn` from `ip_country` + `asn`,
   `default_bank`, `default_currency`.
7. **De-duplication**: per channel in memory, and against the database across restarts.

The *Data preparation* page is a workbench: load a channel's mapping and rules, paste sample
records (CSV, JSON or JSON lines), see every accepted or rejected record, and save the
configuration back to the channel. Rejected records are stored with their raw content and reason.

The seed contains a *Legacy card system* channel as an example of a foreign schema:

* amounts in cents
* `dd/mm/yyyy HH:MM` dates
* `CARD_LOAD`/`TOPUP` codes
* fixed bank

## Training lifecycle

1. History arrives (webhook/TCP history batches, an uploaded file in *Engine & training*, or
   the mock simulator). It is stored with `is_training = true`. Three seconds after the last
   batch, or when live data arrives, the worker fits the three profiles on all training rows.
2. Without any history, the first `min_training_transactions` live transactions are used for
   training.
3. Live transactions are scored and persisted. Only non-suspicious ones update the profiles.
4. When an analyst marks a transaction **legitimate**, a `learn` command adds it to the
   customer's profile.
5. *Retrain* refits on history plus all legitimate scored transactions. The trained model is
   saved to `model_blobs` every 2 minutes and after each fit.

## Security

* JWT bearer tokens (`JWT_SECRET`, expiry `JWT_EXPIRE_MINUTES`) and PBKDF2-SHA256 password hashing.
* Roles:
  * `viewer`: read-only
  * `analyst`: review, reports, uploads, retraining
  * `admin`: channels, banks, thresholds, configuration import/export, users, purge
* Audit log of logins (including failed ones), configuration and channel changes with
  before/after values, analyst decisions, exports and report downloads.
* Containers run as an unprivileged user. nginx sets security headers.
* Change the defaults in `.env` before any shared deployment: `ADMIN_PASSWORD`, `JWT_SECRET`,
  `POSTGRES_PASSWORD`.

## Operations

```bash
docker compose -f docker-compose.enterprise.yml --profile demo up -d --build    # start everything, including the mock feed
```
```bash
docker compose -f docker-compose.enterprise.yml logs -f worker                  # follow channel / training / scoring activity
```
```bash
docker compose -f docker-compose.enterprise.yml stop mock-feed                  # stop the demo feed
```
```bash
docker compose -f docker-compose.enterprise.yml down                            # stop (data kept in volumes)
```
```bash
docker compose -f docker-compose.enterprise.yml down -v                         # stop and delete all data
```

Feeding the channels from outside Docker:

```bash
python mock_channel/generate_transactions.py --days 0 --output tcp --port 9009 --live --rate 5
```
```bash
curl -X POST localhost:8088/transactions -H 'Content-Type: application/json' \
  -d '{"user_id":"C00001","timestamp":"2026-11-16 10:30:00","tx_type":"transfer","amount":250,"iban":"IT60X0542811101000000123456","ip":"79.151.2.3","cc_asn":"IT-AS3269","bank_id":"BANK001"}'
```

Building behind a proxy with TLS inspection: put the proxy's CA certificate (PEM) somewhere in
the project and set `CORPORATE_CA_FILE=./path/to/ca.pem` in `.env`. It is passed to pip and npm
as a BuildKit secret and is never stored in the images.

## Tests

```bash
pip install -r requirements.txt -r requirements-server.txt httpx
```
```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q
```

`tests/test_enterprise.py` runs the API and the worker in-process on SQLite. It covers:

* channel configuration and the simulator
* training and scoring
* dashboard, detail and reasons
* CSV export and PDF reports
* analyst feedback and learning
* per-bank thresholds with re-evaluation
* the preparation preview with the legacy rules
* file upload with rejected records
* role enforcement and the audit log
