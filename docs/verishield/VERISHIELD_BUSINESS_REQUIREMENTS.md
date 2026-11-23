# VeriShield
## Business Requirements Document (BRD) and Enterprise Solution Specification

**Document version:** 1.0  
**Date:** 23 November 2026  
**Prepared for:** Bank of Palestine initial deployment; reusable for other financial institutions  
**Product category:** Fraud Risk, Transaction Monitoring, Customer Intelligence and AML Case Management  
**Status:** Proposed target-state requirements baseline  
**Source foundation:** [abuwalid1998/banksealer-fraud-detection](https://github.com/abuwalid1998/banksealer-fraud-detection)

> **Important status statement:** This document specifies the VeriShield product to be engineered. It does not claim that all described enterprise capabilities already exist in the BankSealer repository. The repository is the starting detection-engine implementation; the enterprise platform, integrations, access controls, decision enforcement, customer onboarding, AML case workflow and operational controls must be implemented and validated before production use.

---

## 1. Executive Summary

VeriShield is a configurable, plug-and-play platform for financial institutions that need to identify unusual transactions, prevent or pause risky payments when integrated with the institution's authorization systems, build customer behavioural intelligence, and manage AML investigations through a controlled workflow.

The initial deployment target is Bank of Palestine (BOP). The product must not be hard-coded for BOP: every customer institution must be onboarded as a tenant with its own connectors, data mappings, thresholds, models, users, branding, report templates, retention rules and operating policies.

The platform combines five capabilities:

1. **Real-time transaction intelligence:** evaluate a transaction using customer behaviour, recent activity, peer-group behaviour, configurable business rules and optional additional risk signals.
2. **Configurable transaction controls:** return an explicit `PASS`, `REVIEW`, `HOLD` or `DECLINE/BLOCK` decision to the bank's payment/core system. The bank's system of record remains responsible for actually executing, holding or rejecting the payment.
3. **Customer intelligence and onboarding clustering:** create an initial customer segmentation model when a new institution is onboarded, then progressively learn each customer's own normal behaviour.
4. **AML investigation management:** create and assign transaction/customer investigations, capture analyst evidence and recommendations, and route cases to the AML Manager for a final internal decision.
5. **Enterprise administration and reporting:** configurable integrations, monitored channels, API and optional MCP access, role-based access, audit logs, operational dashboards and client-branded PDF/CSV/XLSX reports.

The desired user experience is a clean, institutional web application called **VeriShield**. It should use minimal visual noise, restrained colours, clear status indicators and subtle animations. The platform must have a Demo Mode for controlled evaluation using synthetic data.

## 2. Product Vision, Objectives and Business Outcomes

### 2.1 Vision

Give banks a unified, explainable and configurable control layer that turns transaction data into timely risk decisions and actionable investigations without requiring each institution to rebuild the detection engine or user workflow.

### 2.2 Business objectives

- Reduce fraud losses by identifying abnormal activity before payment execution where synchronous integration is available.
- Reduce analyst workload by prioritising alerts and presenting the reasons, customer context and related activity in one place.
- Improve the quality and consistency of AML investigations through standard case files, assignment, evidence, recommendation and decision history.
- Improve customer onboarding by creating initial peer groups and behavioural baselines from available customer and historical transaction data.
- Make deployment repeatable through configurable connectors, field mappings, policies, tenant setup and templates.
- Give management measurable visibility into fraud patterns, AML workload, decision outcomes, model performance and platform health.
- Keep customer institutions operationally independent through tenant isolation, separate models, separate policies and separate branding.

### 2.3 Expected business value

| Business area | Value created | How VeriShield supports it |
|---|---|---|
| Fraud prevention | Earlier intervention and potentially avoided losses | Synchronous risk decision API, policy thresholds, hold/decline integration and post-event monitoring |
| AML productivity | More consistent investigations and clearer ownership | Case queues, analyst assignment, evidence, recommendations, SLA monitoring and manager decisions |
| Customer onboarding | Better initial customer understanding | Customer data profiling, peer-group clustering, cold-start baselines and data-quality feedback |
| Operational control | Faster diagnosis of feed and integration failures | Channel monitoring, connector health, retry queues, audit logs and system reports |
| Management oversight | Better decisions based on measurable outcomes | Executive dashboards, trend reports, case ageing, alert outcomes and model metrics |
| Reusability | Lower implementation effort for new institutions | Tenant onboarding wizard, adapter architecture, mapping configuration and branding settings |

No financial savings, fraud-detection rate or false-positive reduction should be promised until measured using representative, institution-approved data. The mock-data performance figures in the source repository are development benchmarks, not evidence of production performance at BOP.

## 3. Repository Assessment and Reuse Strategy

### 3.1 Existing foundation verified in the source repository

The reviewed repository currently provides a Python/PySide6 desktop application and an implemented BankSealer-style analytical engine. The README and `docs/ALGORITHM.md` describe:

- Local profile: HBOS-based per-customer/per-operation behavioural deviation.
- Temporal profile: per-customer activity-window statistics and excess-activity scoring.
- Global profile: peer-group clustering using iterative DBSCAN and CBLOF-style outlier scoring.
- Weighted combined suspicion score, configurable risk levels and bank/company threshold overrides.
- Transaction preparation, field mapping, validation, enrichment and duplicate handling.
- Mock, TCP JSON-lines, HTTP webhook, CSV and SQLite transaction channels.
- Customer summaries, transaction explanations, dashboards, alert lists, PDF reports and CSV export.
- Analyst feedback actions such as marking a transaction as legitimate or confirmed fraud.
- YAML and GUI configuration for thresholds, model parameters, channels and auto-report settings.

Useful source locations include `banksealer/engine/`, `banksealer/preparation.py`, `banksealer/channels/`, `banksealer/controller.py`, `banksealer/storage.py`, `banksealer/reports.py`, `mock_channel/` and `docs/ALGORITHM.md`.

### 3.2 Gaps between the current repository and VeriShield target state

| Capability | Current repository position | VeriShield requirement |
|---|---|---|
| User interface | Desktop PySide6 application | Responsive, browser-based enterprise frontend with role-specific workspaces |
| Users and authentication | No enterprise identity/role lifecycle evidenced in reviewed core | Provisioning, forced first-login password change, MFA, RBAC, session controls and audit |
| Persistence | SQLite persistence for transactions/scores/reports | Production relational database, migrations, tenant isolation, retention, backup and recovery |
| Startup data safety | The controller clears persisted data on a non-resume startup path | Safe deployment migrations; no implicit deletion; explicit reset only in isolated Demo Mode |
| Decision enforcement | Risk score and analyst status actions | Synchronous decision API and bank-side authorization/hold/release/decline adapters |
| AML cases | Suspicious transaction queue and analyst status | Case lifecycle, assignment, evidence, customer-level cases, recommendations and manager decisions |
| Customer onboarding clustering | Global peer groups derived from transaction spending behaviour | Tenant onboarding pipeline using KYC/customer metadata and available history, plus cold-start strategy |
| Integration configuration | Existing channel configuration and field mapping | Secure enterprise connection registry, credentials vault, test connection, health, retries, versioning and audit |
| APIs | HTTP ingestion channel exists; no full enterprise API surface established by the README | Versioned REST APIs, webhooks, event contracts, OpenAPI and system-to-system authentication |
| MCP | Not established in the current implementation | Optional, scoped MCP server for approved operational/analyst tools |
| Reporting branding | Reports include bank identity and standard report layouts | Tenant logo, colour, header/footer, disclaimer, language, numbering, watermark and template configuration |
| System monitoring | Basic channel preparation logs and application status | Metrics, traces, alerting, audit, failure reports, readiness and service-level monitoring |
| Deployment | Desktop Python launch; backend/database/infrastructure folders identified as placeholders | Packaged enterprise deployment with containers, supported runtime, database, secrets and upgrade path |

### 3.3 Reuse principles

1. Preserve the current scoring engine behind a stable internal interface; do not rewrite the algorithms until a validation exercise demonstrates a need.
2. Extract the engine from the desktop application and run it as a stateless/scalable scoring service, with controlled model-state management.
3. Keep the existing transaction-preparation logic and channel adapters where suitable, but put all ingress through a canonical event contract.
4. Treat the desktop UI as a prototype/reference implementation. The production UI is a separate web frontend.
5. Replace SQLite as the production system of record. SQLite may remain useful for local development or isolated demo packaging.
6. Re-test every algorithm against institution-approved historical data. The mock generator's injected fraud scenarios do not represent the complete fraud or money-laundering risk landscape.

## 4. Product Scope

### 4.1 In scope for the target product

- Tenant and institution setup.
- User and role administration.
- Secure connector/channel configuration and monitoring.
- Transaction ingestion, schema mapping, validation, enrichment and deduplication.
- Real-time scoring and post-event transaction monitoring.
- Configurable risk thresholds, rule policies and decision actions.
- Bank-side transaction decision integration.
- Customer profile, customer risk context and customer segmentation.
- Initial cluster/model building during institution onboarding.
- AML alert and case management.
- Manager assignment, analyst recommendation and manager disposition.
- Role-specific dashboards and report generation.
- Client-specific report branding and templates.
- Demo Mode with synthetic data and strict isolation.
- Versioned APIs, webhooks, event integration and optional MCP server.
- Model lifecycle governance, audit, operational monitoring and security controls.

### 4.2 Out of scope unless separately approved

- Replacing the bank's core banking, payment switch, digital banking or card-processing system.
- Becoming the system of record for customer accounts or payment balances.
- Automatically filing STR/SAR reports with a regulator or FIU without a separately approved workflow and legal review.
- Making a legal determination that a person has committed fraud or money laundering.
- Automatically freezing an account or customer relationship solely from a model score.
- Providing sanctions, PEP, adverse-media or identity-screening datasets. VeriShield may integrate with approved third-party screening services, but licensing, data quality and policy ownership remain with the institution.
- Using customer segmentation as the sole basis for adverse decisions.

## 5. User Model and Initial Five-User Setup

### 5.1 Roles

The four business roles are:

1. **IT Admin** - owns technical configuration, connections, users, channel health and platform operations.
2. **AML Department Manager** - owns AML operational queues, case assignment, business thresholds, dispositions and AML reports.
3. **AML Department Employee** - investigates assigned work and submits evidence, findings and recommendations.
4. **Chief of AML/Compliance** - has a full oversight dashboard and reporting/monitoring access across AML and transaction insights.

### 5.2 Initial five-user assumption

The request specifies five users but describes four roles and explicitly limits the AML Manager and Chief to one account each. For the initial five named accounts, this BRD assumes:

| Initial account | Role | Quantity | Notes |
|---|---|---:|---|
| Bootstrap account | IT Admin | 1 | Login username `1000`; initial password `Admin`; must be changed on first login |
| AML Manager | AML Department Manager | 1 | Exactly one active manager in the initial BOP setup |
| Analyst 1 | AML Department Employee | 1 | Receives assigned investigations |
| Analyst 2 | AML Department Employee | 1 | Receives assigned investigations |
| Chief | Chief of AML/Compliance | 1 | Exactly one active chief in the initial BOP setup |
| **Total** |  | **5** | Initial deployment baseline |

The application should not hard-code a global maximum of five unless the commercial license explicitly requires it. The initial BOP deployment is provisioned with five users; future client deployments can configure employee-seat counts.

### 5.3 User account data

Required fields:

- Internal immutable user UUID.
- Login username (unique 4- or 5-digit numeric string, for example `3176` or `9999`).
- First name.
- Last name.
- Email address.
- Role.
- Account status (Invited, Active, Locked, Disabled).
- Created by, created timestamp, last login timestamp.
- Password-change-required flag, MFA enrollment status and credential reset metadata.

The login username is an account identifier, not a security factor. The system must not rely on numeric usernames for authentication security.

### 5.4 Bootstrap administrator flow

- On a new, isolated installation, create one bootstrap IT Admin with username `1000` and initial password `Admin` as specified.
- The bootstrap account is for first-run activation only. Access should be restricted to the setup environment/network and the first successful login must force a password change before any other application action.
- The IT Admin must provide and verify an email address during first-run activation, set first/last name, and enrol MFA before the setup wizard can be completed.
- The default password must not be reused in a production image, source repository, public deployment guide or shared environment. Store only a strong salted password hash. Never log or display the password after creation.
- A production installer should create a unique one-time bootstrap secret or enforce a controlled first-run activation window. If the literal `1000/Admin` bootstrap is retained for a BOP pilot, deployment must be private-network restricted and the credential must be rotated immediately.

### 5.5 Creating other users

- Only IT Admin can create, disable, unlock or reset users.
- IT Admin enters first name, last name, email, 4-5 digit username and role.
- IT Admin sets or generates a temporary human-friendly password under the institution's password policy.
- Temporary passwords are single-use, expire after a configurable short period and are never displayed again after issuance.
- At first login, the user must change the temporary password and enrol MFA if not already enrolled.
- The system records who created/reset the account and when.
- The AML Manager is the only user who may assign cases to AML Employees in the initial role design.

## 6. Role-Based Access Control

All access must be enforced by the backend, not only by hiding frontend buttons. Permissions should be granular and auditable.

| Capability | IT Admin | AML Manager | AML Employee | Chief AML/Compliance |
|---|---:|---:|---:|---:|
| Configure database/API/queue connections | Full | No | No | View health summary only |
| Create/disable/reset users | Full | No | No | No |
| View system health, logs and channel failures | Full, with PII masking | AML-impacting status | Assigned-work status | Executive health summary |
| Configure connector schedules/retries | Full | No | No | No |
| View transactions | Technical metadata / masked samples only | Full AML scope | Linked to assigned cases and required context | Full oversight view |
| View customer profiles | Restricted/masked support view | Full AML scope | Only assigned cases | Full oversight view |
| View all AML alerts/cases | No business decision access | Full | Assigned cases | Full oversight |
| Assign/reassign cases | No | Full | No | View only by default |
| Add investigation notes/evidence | No | Yes | Assigned cases | View only |
| Submit investigation recommendation | No | No | Assigned cases | No |
| Make final AML case disposition | No | Full | No | Oversight; no routine operational disposition |
| Configure thresholds/business policies | Technical configuration only | Full | No | View only; optional approval permission can be enabled |
| Preview/simulate policy impact | No | Full | No | View results |
| Publish report header/footer branding | Platform-wide tenant branding permissions | Tenant report content/header/footer fields | No | View only |
| Generate AML and transaction reports | System reports | Full AML/transaction reports | Assigned-case reports only | Full executive/compliance reports |
| Generate system performance/failure reports | Full | AML service-impact summary | No | Executive summary |
| View model performance | Technical health | Business model effectiveness | Case-level explanation | Full oversight |
| Reset Demo Mode data | Full | No | No | No |
| Access Demo Mode | Configure/enable | Use | Use | Use |

**Segregation principle:** IT Admin manages technology, not AML outcomes. AML Employee recommends, but does not make the final manager disposition. The AML Manager owns operational decisions and thresholds. The Chief has complete oversight and reporting access; any extra approval or override capability must be explicitly granted and audited.

## 7. End-to-End Business Flows

### 7.1 Institution onboarding and initial configuration

1. IT Admin creates a new tenant/institution profile.
2. IT Admin enters legal name, institution code, timezone, base currency, supported languages, deployment region, report branding and data-retention parameters.
3. IT Admin configures data-source connectors: banking API, database read replica, queue, event stream, SFTP/CSV or webhook.
4. IT Admin maps source fields into the VeriShield canonical schema and runs connection tests.
5. VeriShield imports customer master/KYC data and approved historical transactions.
6. The onboarding pipeline produces a data-quality report: record counts, missing fields, duplicate rate, unsupported transactions, time coverage and field-mapping exceptions.
7. The model builder creates initial customer segments/peer groups using approved customer attributes and historical behaviour where available.
8. The BankSealer transaction-behaviour models are trained on eligible historical transactions; model metrics and coverage are reviewed.
9. The AML Manager reviews initial thresholds, rule outcomes and sample alerts. The institution approves policy settings before live enforcement.
10. IT Admin enables selected channels in monitoring-only or shadow mode, then the bank approves cut-over to decision mode.

### 7.2 Real-time transaction decision flow

1. The bank's payment/origination system sends a transaction to the VeriShield decision API before authorisation/settlement where the payment rail supports this integration.
2. VeriShield authenticates the calling system, validates the schema, checks idempotency and enriches the event using permitted customer/account/context data.
3. The rules engine evaluates hard rules, transaction limits, velocity conditions, customer policy, country/channel rules and institution-specific controls.
4. The fraud engine evaluates local, temporal and global behaviour profiles. The AML layer evaluates configured unusual-activity indicators and customer-risk context separately.
5. The decision service combines the relevant signals under the active policy version and returns a decision, scores, reason codes, expiry and correlation ID.
6. The bank's system applies the decision: continue, send for review, pause/hold, or decline/block.
7. VeriShield records the decision request and response, publishes an event to the monitoring pipeline and opens/updates alerts/cases according to policy.
8. If a hold requires human clearance, the case is routed to the AML Manager's queue. The Manager may assign it to an Employee for investigation.
9. The Employee submits findings and a recommendation. The AML Manager records the final internal disposition and, where applicable, sends a release/reject command through the bank's authorised integration.
10. The final bank execution result is reconciled back to VeriShield and included in reporting and approved model feedback.

**Critical control:** `REVIEW` is not automatically equivalent to stopping a payment. `HOLD` means the bank's payment system has placed the transaction into a pending state. `DECLINE/BLOCK` is a rejection instruction. Only a synchronous, production-approved integration with the bank's authorisation system can enforce these outcomes before execution. If a transaction has already settled, VeriShield can raise an alert and support the bank's recall/recovery process; it cannot retroactively block settlement.

### 7.3 AML case investigation flow

1. A suspicious transaction, unusual customer activity pattern, screening match or manual referral generates an alert.
2. Rules deduplicate/merge related alerts and associate them with the relevant customer, account, transaction and related entities.
3. AML Manager reviews the alert queue and either closes it with a reason, retains it for review or creates a formal investigation case.
4. Manager selects one or more transactions/customer profiles, sets priority and due date, and assigns the case to an AML Employee.
5. Employee sees the assigned case, relevant transaction history, customer profile, linked alerts, model explanations and permitted source documents.
6. Employee records investigation steps, evidence references, notes, source-of-funds/purpose observations, related parties and findings.
7. Employee submits a structured recommendation: no concern/close, request more information, continue enhanced monitoring, maintain hold, release transaction, escalate, or recommend suspicious activity reporting.
8. Case returns to the AML Manager. The Manager reviews the evidence and recommendation, records the final internal decision and rationale, and may return it for more work or assign a follow-up task.
9. Any payment release/decline must be transmitted through the authorised bank system and reconciled. Any external STR/SAR filing remains under the institution's approved compliance/legal process.
10. The case is closed with complete audit history, disposition codes, relevant evidence links and retention metadata.

### 7.4 Transaction/customer profile referral

AML Manager can create a referral from either a transaction or customer profile. The referral includes a purpose, priority, due date, selected time window, specific questions to answer and evidence attachments/links. Analysts should not receive unrelated customer data. The Manager retains ownership and receives the analyst's recommendation for decision.

### 7.5 Post-event and batch monitoring

Not every bank feed can be connected inline. VeriShield must support asynchronous monitoring for transactions that arrive after execution or through batch files. Such alerts must be clearly labelled `POST_EVENT`; reporting must not imply that those transactions were stopped. Reconciliation must compare received, scored, rejected, duplicated and missing transaction counts.

## 8. Transaction Risk Decisioning and Threshold Configuration

### 8.1 Separate risk dimensions

VeriShield should retain multiple interpretable dimensions rather than collapsing every financial-crime concern into a single opaque value:

- **Fraud behavioural score:** unusual behaviour for the individual, recent activity and peer group.
- **AML unusual-activity indicators/score:** patterns such as rapid movement of funds, structuring indicators, unusual counterparties, inconsistent activity or other institution-approved typologies.
- **Customer risk context:** institution's CDD/KYC risk classification, customer type, expected activity and review status.
- **Rule and screening outcomes:** hard transaction limits, watchlist/sanctions screening results from approved providers, geographic restrictions and institution-specific rules.

A suspicious model signal is an alerting input, not a finding of guilt. AML suspicion and confirmed fraud must remain distinct case classifications and disposition values.

### 8.2 Configurable policy dimensions

The AML Manager must be able to configure policy rules by:

- Tenant/institution and business unit.
- Transaction type/product/payment rail.
- Channel and origination system.
- Customer segment and customer risk category.
- Amount, currency, velocity, aggregation window and count.
- Beneficiary/counterparty novelty and concentration.
- Country/region, device/IP/session signals where lawful and available.
- Time-of-day/day-of-week and business calendar.
- Fraud, AML and screening signal thresholds.
- Effective-from/effective-to dates, exclusions and exception lists.

### 8.3 Decision actions

| Outcome | Meaning | Expected banking-system behaviour |
|---|---|---|
| `PASS` | No configured control requires intervention | Continue the normal payment workflow |
| `REVIEW` | Alert/review required; policy does not itself pause execution | Route to analyst queue or post-event review; payment treatment follows the configured policy |
| `HOLD` | Human or automated clearance required before execution | Keep payment pending and do not settle until an authorised release/decline is received or the hold expires under bank policy |
| `DECLINE` / `BLOCK` | Payment should be rejected | Bank authorisation system rejects the transaction and returns an execution result |
| `ERROR_POLICY` | Decision could not be reliably produced | Apply the institution-approved fail-open/fail-closed policy for that payment type and raise an operational incident |

### 8.4 Policy configuration controls

- Editable threshold values with clear validation and units.
- Default policy plus scoped overrides.
- Version history, editor, timestamp, justification and before/after values.
- Policy simulation against selected historical data before publication.
- Preview of expected alert/hold/block volumes and impact by segment.
- Scheduled activation and expiry.
- Emergency rollback to a previous version.
- A policy change affects new decisions from its effective time. Historical decisions remain immutable; a separate analytical re-evaluation may be run and must not rewrite the original operational decision.
- Critical policy changes should support optional maker-checker approval according to the institution's governance model.

### 8.5 Example policy matrix (illustrative only)

| Condition | Example score/rule state | Illustrative action |
|---|---|---|
| Within normal profile and no hard-rule hit | Low risk | PASS |
| Behavioural deviation above review threshold | Medium risk | REVIEW |
| Strong deviation plus material rule hit | High risk | HOLD |
| Confirmed hard policy breach under approved rules | Critical risk | DECLINE/BLOCK |

Threshold numbers must be calibrated using BOP-approved data, risk appetite, payment products and operating capacity. Do not deploy generic repository defaults as production policy without validation.

### 8.6 Failover and timeout policy

The institution must explicitly approve the fallback per payment rail and transaction class:

- **Fail-open:** payment continues if VeriShield is unavailable; transaction is queued for post-event monitoring.
- **Fail-closed:** payment is not authorised until a reliable decision is available.
- **Fallback review/hold:** payment is held by the bank's system for a bounded period.

The platform must not silently select a fallback. A timeout, unavailable dependency, malformed response or stale model must return a defined error state and trigger an operational alert. The bank owns the decision on which fallback is appropriate for each product.

## 9. Customer Onboarding, Segmentation and Cluster Model Builder

### 9.1 Purpose

Build an initial understanding of customers when a new institution is onboarded, including customers with little or no transaction history. The first customer segmentation model must be tenant-specific and generated during the institution onboarding process.

### 9.2 Data sources

Subject to client approval and lawful access:

- Customer master and KYC/CDD attributes.
- Individual/business classification and industry/business activity.
- Products, account types, relationship tenure and service usage.
- Expected turnover, expected transaction volume and stated purpose of account.
- Historical transaction aggregates and permitted transaction detail.
- Existing bank customer-risk category and review status.
- Beneficiary/counterparty and channel usage summaries.

Only fields with a documented purpose, quality and legal basis should be included. Sensitive or protected personal attributes must not be used as convenient clustering shortcuts.

### 9.3 Model-building steps

1. **Profile ingestion:** import customer reference records and historical transaction history.
2. **Data-quality assessment:** inspect missingness, duplicates, stale fields, inconsistent codes, currency handling, time coverage and class balance.
3. **Feature engineering:** create customer-level features such as product mix, transaction frequency, amount bands, geographic/counterparty diversity and expected-versus-observed activity.
4. **Segmentation:** choose a clustering approach based on data shape and evaluation. The current BankSealer global model is useful for behavioural peer groups, but onboarding segmentation may require additional methods suited to customer master data.
5. **Cluster interpretation:** calculate each segment's size, representative profile, feature ranges and stability. Give segments neutral descriptive labels, not automatic labels such as “criminal” or “safe”.
6. **Cold-start assignment:** assign a newly onboarded customer to a provisional peer segment using approved profile features; show the confidence/fit and mark limited-history status.
7. **Behavioural learning:** as transactions accumulate, build or refine the customer's individual local and temporal profile.
8. **Validation:** back-test alert volumes and model quality, review segments with AML and business stakeholders, approve version and publish.
9. **Monitoring:** monitor feature drift, segment migration, customer coverage, low-confidence assignments and model stability.

### 9.4 Required customer profile view

- Customer identity/reference (masked as appropriate).
- Customer type, segment, business activity and relationship context.
- Current institution-approved customer risk classification.
- Peer group/cluster ID and segment explanation.
- Model version, assignment confidence and training timestamp.
- Transaction aggregates and behavioural history.
- Usual channels, hours, amounts, beneficiaries/counterparties and geographic pattern where available.
- Open/closed alerts and investigations.
- Recent profile changes and relevant audit trail.
- Undertrained/new-customer indicator and recommended monitoring treatment.

### 9.5 Model safeguards

- A cluster is a segmentation/peer-comparison aid, not a regulatory risk classification or an autonomous block rule.
- No adverse decision may be made solely because a customer belongs to a cluster or because a model assigns a low-confidence segment.
- Each tenant has isolated training data and model artifacts.
- Model versions, training datasets, feature definitions, metrics and approvals are recorded.
- Rebuilds must support preview, champion/challenger comparison, shadow deployment and rollback.
- Approved analyst dispositions may be used for training only under a documented feedback policy; unreviewed alerts must not be treated as confirmed fraud labels.

## 10. AML Case Management Requirements

### 10.1 Case and alert structure

A case may contain multiple alerts, transactions, customers, accounts, counterparties, documents and internal notes. A customer-level investigation can be opened without a single triggering transaction. Every alert must preserve its original detection context and model/policy version.

### 10.2 Case statuses

Recommended lifecycle:

`NEW -> TRIAGED -> ASSIGNED -> IN_PROGRESS -> RECOMMENDATION_SUBMITTED -> MANAGER_REVIEW -> (RETURNED_FOR_MORE_INFO | APPROVED_DISPOSITION) -> CLOSED`

Case status and payment execution status must be separate. A case can be open while a payment is already completed; a transaction can remain held while a case is still under investigation.

### 10.3 Analyst recommendation values

- No concern / close with rationale.
- Additional information required.
- Continue monitoring / enhanced due diligence.
- Maintain hold pending further review.
- Recommend release (subject to Manager and bank execution permissions).
- Recommend decline/reject.
- Escalate to AML Manager/Chief/Legal or Compliance Committee.
- Recommend that the authorised compliance officer assess an STR/SAR filing.

### 10.4 Manager disposition values

- Close as no concern.
- Close as false positive/model alert.
- Continue enhanced monitoring.
- Return to analyst for more work.
- Release transaction (through bank integration if still pending).
- Reject transaction (through bank integration if still pending).
- Confirm internal fraud classification (where supported by evidence and bank process).
- Escalate for formal suspicious activity reporting assessment.

The product must not equate “suspicious” with “confirmed money laundering”. External STR/SAR filing is a separate regulated process and requires authorised human review.

### 10.5 Case controls

- Unique case ID, source alert IDs, associated entities and timestamps.
- Owner/assignee, priority, SLA, due date and escalation status.
- Complete chronology of actions and assignment changes.
- Internal notes and structured investigation checklist.
- Secure evidence attachment references and hashes; virus scanning and allowed file types.
- Related-case linking, duplicate detection and customer-level case consolidation.
- Manager comments and final rationale are mandatory for disposition.
- Reopen and follow-up workflows with reason.
- Access control for sensitive cases and restricted notes.
- Case export with audit history and branding.

## 11. Dashboards and Reporting

### 11.1 IT Admin dashboard

- Overall application/service health.
- Connected channels with status, last successful event, throughput, lag and error rate.
- Received/accepted/rejected/duplicate transaction counts.
- Database, queue and model-service health.
- API latency, error rates, timeouts and authentication failures.
- Connector test history, retry backlog and dead-letter queue.
- Model version, load status and scoring latency.
- Configuration changes, deployment version and audit events.
- System performance and failure reports by channel, period, error category and service.

IT Admin views should mask transaction/customer PII by default. Technical troubleshooting access to unmasked data must be separately permissioned and audited.

### 11.2 AML Manager dashboard

- Total alerts, open alerts and open cases.
- Risk/priority distribution and ageing buckets.
- Pending cases by analyst, due date and SLA breach.
- Transaction and customer profile search.
- Held transactions awaiting decision and their remaining hold time.
- Analyst workload and recommendation turnaround.
- Detection trends by product/channel/customer segment.
- False-positive/closed-no-concern rates and disposition breakdown.
- Threshold and policy version performance.
- Ability to assign/referral a transaction profile or customer profile to an employee.
- Report generation for AML operations, transactions, customers, case queues and usage.

### 11.3 AML Employee dashboard

- My assigned cases and tasks.
- Priority, due date, ageing and SLA countdown.
- Cases awaiting more information or Manager response.
- Transaction/customer context for assigned cases only.
- Investigation checklist completion and evidence references.
- Submitted recommendations and returned work.
- Personal workload and turnaround metrics.

### 11.4 Chief AML/Compliance dashboard

- Enterprise AML and transaction overview.
- Customer/transaction activity and risk trends.
- Open cases, ageing, backlog and SLA performance.
- High-risk cases and held transaction exposure.
- Alert outcomes, escalation trends and investigation quality indicators.
- Analyst/Manager productivity and case distribution.
- Model effectiveness and drift summaries.
- Report catalogue and executive report generation.
- Full oversight access subject to legal and role policy; all exports are audited.

### 11.5 Standard report catalogue

- Executive fraud and AML overview.
- Transaction decision report (single transaction and batch).
- Customer risk/behaviour profile.
- Customer segment/cluster distribution.
- AML alert and case register.
- Investigation report with chronology, evidence references, employee recommendation and manager disposition.
- Held/declined transaction report and release/decline outcomes.
- Threshold/policy configuration history and impact analysis.
- Analyst workload and SLA performance.
- Model health, coverage, drift and performance report.
- Channel/data ingestion health and reconciliation report.
- API, connector and system failure report.
- Demo Mode usage report, clearly marked as synthetic.

Formats: PDF for controlled business reports; CSV/XLSX for permitted analysis; API export for approved systems. Reports must support date/time range, institution, product, risk, channel, customer segment and status filters.

## 12. Client Branding and Report Template Configuration

Each tenant must have its own report branding settings. Recommended fields:

- Institution legal name and display name.
- Logo (SVG/PNG) with size and light/dark background handling.
- Primary, secondary and accent colours.
- Report title/subtitle, report code and numbering prefix.
- Header text and footer text.
- Confidentiality notice and legal disclaimer.
- Generated timestamp, timezone and page number format.
- Prepared by / reviewed by / approved by labels and optional signature blocks.
- Watermark such as `CONFIDENTIAL`, `DRAFT` or `DEMO DATA`.
- Language, date and number formats, base currency and locale.
- Optional appendix and evidence index.
- Versioned templates and preview before publishing.

IT Admin configures institution identity and logo. AML Manager may configure the approved AML report header/footer and report-specific wording within the tenant's permitted branding scope. Branding must never override mandatory bank disclaimers or regulatory content without authorised approval.

## 13. Enterprise Architecture

### 13.1 Proposed logical architecture

```text
Bank Channels / Systems
  |-- Core banking / payment switch / cards / digital banking
  |-- Customer & KYC systems / CRM / account master
  |-- Data warehouse / reporting stores
  |-- Screening providers / approved external risk data
  |-- CSV/SFTP / Webhooks / Kafka or RabbitMQ / TCP feeds
            |
            v
Enterprise Integration Layer
  - Connector registry, field mapping, validation, enrichment
  - Idempotency, retries, replay, dead-letter queue, reconciliation
            |
            v
VeriShield Platform API / API Gateway
  |-- Authentication, tenant context, rate limits, API audit
  |-- Transaction Decision API (synchronous)
  |-- Transaction/Event Ingestion API (asynchronous)
  |-- Customer, Alert, Case, Report and Admin APIs
            |
            +----------------------+-----------------------+
            |                      |                       |
            v                      v                       v
     Policy & Rules          Fraud Model Service      AML Monitoring Layer
     - Thresholds            - Local HBOS              - Typology rules
     - Hard controls         - Temporal profile        - Customer context
     - Decision actions      - Global peer model       - Screening results
            |                      |                       |
            +----------------------+-----------------------+
                                   |
                                   v
                         Decision Orchestrator
                       PASS / REVIEW / HOLD / DECLINE
                                   |
                                   v
                     Bank Authorization/Execution API
                       Hold / Release / Reject / Reconcile

  Data & Governance: PostgreSQL, event/message broker, cache, object storage,
  model registry, audit store, metrics/logs/traces, backup and recovery.

  User Experience: Web Frontend -> role-specific dashboards and workflows.
  Optional MCP Server -> scoped, audited tools calling the same application APIs.
```

### 13.2 Recommended technology boundaries

The final stack should be aligned with BOP's approved technology standards. A practical reference implementation is:

- **Frontend:** React + TypeScript, responsive institutional dashboard.
- **Platform/API layer:** Spring Boot (matching the repository's original service-oriented direction) or another bank-approved enterprise framework.
- **ML/scoring service:** Python service exposing a stable internal API; reuse the BankSealer engine behind an adapter.
- **Relational database:** PostgreSQL for configuration, users, customer references, alerts, cases, decisions and audit metadata.
- **Event transport:** Kafka for high-volume event streaming or RabbitMQ for queue/workflow patterns; select one according to throughput and existing bank infrastructure.
- **Cache/short-lived coordination:** Redis where needed for idempotency, rate limits and ephemeral state.
- **Object storage:** S3-compatible storage or bank-approved document repository for report/evidence artifacts.
- **Deployment:** Docker Compose for controlled demo/developer use; hardened containers and the bank-approved orchestration/platform for production.
- **Observability:** metrics, logs and distributed traces with alerting and retention controls.

These are proposed boundaries, not a commitment that every component must be installed. Avoid introducing infrastructure without a documented operational need.

### 13.3 Multi-tenant architecture

- Tenant identifier must be derived from authenticated context, not trusted from arbitrary client payloads.
- Separate tenant configuration, policy versions, connectors, model artifacts, report templates and audit scope.
- Use database row-level security or equivalent isolation controls, and separate database/schema deployment when required by the client's risk posture.
- Prevent cross-tenant search, exports, model training and cache leakage.
- Each tenant can use different deployment profiles, local timezones, currencies and data-retention schedules.

## 14. Configurable Integrations and Channel Management

### 14.1 Connector catalogue

Initial connector types:

- REST API (pull and push).
- HTTP webhook ingestion.
- Kafka / RabbitMQ / JMS or approved message broker.
- Database read-only connection to a reporting replica or approved CDC feed.
- SFTP/managed file transfer for CSV, JSONL or fixed-width files.
- TCP JSON-lines feed for compatibility with the current prototype where needed.
- Scheduled batch files and historical data import.
- Outbound REST/webhook callback for decisions, holds, releases and status reconciliation.
- Optional screening, CRM/KYC and case/document systems.

### 14.2 IT Admin connection configuration

Every connection record includes:

- Tenant, connection name, type and environment (DEV/UAT/PROD).
- Host/base URL, port, database/schema/topic/path and network route details as applicable.
- Authentication method (OAuth2 client credentials, mTLS, managed identity, signed webhook, vault-held credential or approved service account).
- TLS verification and certificate reference.
- Source/target field mappings and transformation rules.
- Polling interval, batch size, timeout, retry/backoff and concurrency limits.
- Allowed IPs, outbound destinations and connection schedule.
- Data scope and read/write permission declaration.
- Last test time, last success, status, error count and owner.
- Credential expiry/rotation reminder and secret reference (not plaintext secret).

### 14.3 Connection lifecycle

`DRAFT -> TESTING -> VALIDATED -> ENABLED -> DEGRADED/DISABLED -> RETIRED`

The UI must offer Test Connection, Validate Mapping, Preview Sample (masked), Enable, Disable, Retry, Replay and View Logs. Destructive actions require confirmation. Credential rotation must not expose the prior secret. Database connectors should be read-only by default; transaction execution uses an explicit bank-approved API, not direct writes into core banking tables.

### 14.4 Channel monitoring

For each channel display:

- Health state and last heartbeat/last received message.
- Events received, accepted, rejected, duplicated and retried.
- Processing lag, throughput and average/p95 latency.
- Last error and sanitized error details.
- Queue depth, retry count and dead-letter count.
- Reconciliation gaps between source and VeriShield.
- Start/stop/maintenance history and operator identity.

## 15. APIs, Events and MCP

### 15.1 API principles

- Version all external APIs (for example `/api/v1`).
- Publish OpenAPI specifications, examples and a sandbox collection.
- Use stable canonical transaction/customer/case IDs and correlation IDs.
- Support idempotency keys on decision and event endpoints.
- Paginate large search results and support filters/time ranges.
- Use structured error codes and safe retry guidance.
- Enforce authentication, scopes, rate limits, input validation, request size limits and audit.
- Keep ingestion and synchronous decision APIs separate so batch processing cannot block the payment path.

### 15.2 Core API catalogue (target)

| API | Method | Purpose |
|---|---|---|
| `/api/v1/decisions/evaluate` | POST | Synchronous pre-authorisation scoring and decision |
| `/api/v1/transactions/events` | POST | Asynchronous transaction/event ingestion |
| `/api/v1/transactions/{id}` | GET | Transaction details and decision context |
| `/api/v1/transactions/{id}/decision` | GET | Original decision and policy/model versions |
| `/api/v1/transactions/{id}/execution-result` | POST | Bank sends final execution/hold/release/reject result |
| `/api/v1/customers/{id}/profile` | GET | Customer and peer profile, subject to scope |
| `/api/v1/customers/{id}/alerts` | GET | Customer alert history |
| `/api/v1/alerts` | GET/POST | Search alerts and create manual referrals |
| `/api/v1/cases` | GET/POST | Search and create investigation cases |
| `/api/v1/cases/{id}/assign` | POST | Assign/reassign a case |
| `/api/v1/cases/{id}/recommendation` | POST | Submit analyst recommendation |
| `/api/v1/cases/{id}/disposition` | POST | Record Manager disposition |
| `/api/v1/policies` | GET/POST | Read and propose policy configuration |
| `/api/v1/policies/{id}/simulate` | POST | Historical policy simulation |
| `/api/v1/connectors` | GET/POST | Manage connection metadata |
| `/api/v1/connectors/{id}/test` | POST | Test connector health/credentials |
| `/api/v1/reports` | GET/POST | Generate/list reports |
| `/api/v1/models/status` | GET | Current models, versions and health |
| `/health`, `/ready`, `/metrics` | GET | Operational readiness and telemetry |

The exact endpoint contracts, payload fields and permission scopes must be delivered as part of API design and integration testing.

### 15.3 Example decision request

```json
{
  "tenant_id": "BOP",
  "transaction_id": "TX-20261123-000001",
  "idempotency_key": "TX-20261123-000001-v1",
  "customer_id": "CUST-00123",
  "account_reference": "ACCT-00456",
  "timestamp": "2026-11-23T09:30:00+02:00",
  "transaction_type": "bank_transfer",
  "amount": 1250.00,
  "currency": "ILS",
  "channel": "mobile_banking",
  "beneficiary_reference": "BEN-0098",
  "beneficiary_country": "PS",
  "session_reference": "SESSION-ABC",
  "device_reference": "DEVICE-XYZ"
}
```

### 15.4 Example decision response

```json
{
  "transaction_id": "TX-20261123-000001",
  "decision_id": "DEC-900001",
  "decision": "HOLD",
  "execution_required": true,
  "fraud_score": 0.82,
  "aml_signal_level": "ELEVATED",
  "customer_risk_context": "MEDIUM",
  "reason_codes": ["NEW_BENEFICIARY", "VELOCITY_ABOVE_PROFILE"],
  "policy_version": "BOP-POLICY-12",
  "fraud_model_version": "BOP-FRAUD-04",
  "issued_at": "2026-11-23T09:30:00+02:00",
  "expires_at": "2026-11-23T09:45:00+02:00",
  "correlation_id": "CORR-9F23"
}
```

These examples are illustrative contract shapes. `HOLD` only pauses execution when the bank system acknowledges and enforces the hold. Scores and reason codes must not disclose model internals to an unauthorised consumer.

### 15.5 MCP server requirements

MCP is an optional tool-access layer for approved AI assistants and operational tooling. It is not a replacement for the REST/event APIs and must not become a back door to the database or payment system.

Approved read-oriented tools may include:

- `list_connectors` and `get_connector_health`.
- `test_connector` (technical test only; no unbounded data extraction).
- `get_channel_metrics` and `get_ingestion_failures`.
- `get_model_status` and `get_scoring_latency_summary`.
- `get_aml_queue_summary` and `get_case_summary` (role- and tenant-scoped).
- `search_transactions` and `get_customer_profile` (masked/minimised, authorised scope only).
- `preview_policy` and `simulate_policy`.
- `generate_report` for permitted report templates.

Potential write tools such as enabling/disabling a channel, creating a user, publishing a policy, assigning a case or changing branding must require the same application permissions, validation, audit and explicit user confirmation as the web UI. MCP must not expose unrestricted SQL, raw secret retrieval, direct core-banking writes or autonomous payment block/release tools. Any decision command must pass through the governed application API and bank approval controls.

## 16. Data Model (Logical Entities)

The implementation should define relational entities and audit relationships for at least:

- `Tenant` / `Institution` - name, code, status, locale, timezone, currency and deployment profile.
- `User` / `Role` / `Permission` / `UserSession` - identity, login, MFA, lifecycle and scopes.
- `Connector` / `Channel` / `CredentialReference` / `FieldMapping` - connection metadata, secret reference and health.
- `IngestionBatch` / `IngestionEvent` / `DeadLetterEvent` - data receipt, validation and retry history.
- `Customer` / `AccountReference` / `CustomerAttribute` - master references and approved profile fields.
- `CustomerSegment` / `ClusterModel` / `ModelVersion` / `ModelTrainingRun` - tenant-specific segment definitions and model artifacts.
- `Transaction` / `TransactionEnrichment` - canonical event and derived context.
- `RiskEvaluation` / `RuleHit` / `Decision` / `DecisionPolicyVersion` - score, reason, decision, expiry and policy/model versions.
- `ExecutionStatusEvent` - bank-side authorisation, hold, release, decline, settlement or recall status.
- `Alert` / `Case` / `CaseEntityLink` / `CaseAssignment` / `CaseNote` / `EvidenceReference` / `Recommendation` / `Disposition`.
- `ReportJob` / `ReportArtifact` / `ReportTemplate` / `BrandingProfile`.
- `AuditEvent` / `SecurityEvent` / `SystemIncident` / `ConnectorHealthMetric`.

All business records should be tenant-scoped. Use immutable identifiers; preserve original raw payload only when approved, encrypted and retained under policy. Store evidence/document content in secure object storage and retain metadata/hash in the relational database. Avoid storing full card/account secrets when a token/reference is sufficient.

## 17. User Interface and VeriShield Brand Requirements

### 17.1 Product name and positioning

**VeriShield**  
*Transaction Intelligence. Trusted Decisions.*

The brand should communicate verification, protection, transparency and institutional control. Avoid an aggressive cyber-security aesthetic or consumer fintech styling.

### 17.2 Suggested visual identity

| Element | Recommendation |
|---|---|
| Primary ink | Deep navy `#10233F` |
| Primary accent | Teal `#0BAA9B` |
| Secondary blue | `#3569C8` |
| Highlight | Soft amber `#E8A33A` |
| Critical status | Restrained red `#C84B55` |
| Background | `#F4F7FA` |
| Surface | White `#FFFFFF` |
| Typography | Inter or a bank-approved sans-serif; strong numeric readability |
| Logo concept | Minimal shield outline with a precise check/verification stroke; wordmark VeriShield |

### 17.3 Interaction principles

- Clean institutional layout with consistent side navigation and clear page hierarchy.
- High information density without visual clutter; reserve strong colour for risk states and important exceptions.
- Subtle transitions and micro-animations only; no distracting continuous animation.
- Respect reduced-motion settings and support keyboard navigation.
- Use clear labels in addition to colour; risk must never be communicated by colour alone.
- Provide responsive layouts for approved desktop and tablet form factors.
- Provide global search, saved filters, export controls and contextual audit history where authorised.
- Distinguish Demo Mode with a persistent visible banner and watermark.

### 17.4 Main navigation

1. Overview
2. Transactions
3. Customers
4. Alerts
5. AML Cases
6. Reports
7. Models & Segments (role-dependent)
8. Integrations & Channels (IT Admin)
9. Policies & Thresholds (AML Manager)
10. Users & Settings (IT Admin)

## 18. Demo Mode

### 18.1 Purpose

Allow IT Admin and authorised business users to evaluate workflows, test dashboards and demonstrate the platform without connecting to production systems or using real customer information.

### 18.2 Requirements

- IT Admin can enable/disable Demo Mode and initialise/reset the synthetic dataset.
- Demo Mode uses synthetic customer, account and transaction records; it must not use copied production PII.
- The UI displays a persistent `DEMO MODE - SYNTHETIC DATA` banner.
- Reports include a `DEMO DATA - NOT FOR OPERATIONAL USE` watermark.
- Demo Mode has its own database/schema/namespace and separate model artifacts from production.
- Outbound transaction execution, hold/release, customer notifications and external regulatory submission are disabled in Demo Mode.
- Production connectors cannot be activated from the Demo Mode workspace.
- Demo reset is explicit, confirmed and audited; it never deletes production data.
- Users can select scenarios such as normal activity, unusual new beneficiary, velocity burst, account takeover-like behaviour, unusual customer turnover and multi-account/counterparty patterns.
- Demo metrics must be labelled as synthetic and must not be presented as production detection performance.

## 19. Non-Functional Requirements

Targets below are proposed engineering objectives and must be baselined with BOP infrastructure, transaction volumes and payment-rail latency budgets before acceptance.

| Area | Requirement |
|---|---|
| Availability | Proposed target 99.9% monthly platform availability for production decision services, excluding agreed maintenance |
| Decision latency | Proposed internal scoring target p95 <= 300 ms under agreed benchmark load; end-to-end budget must be agreed with each bank/payment rail |
| Capacity | Support horizontal scaling; capacity and peak TPS must be agreed and verified using representative load tests |
| Resilience | Bounded retries, idempotency, circuit breakers, queue buffering, dead-letter handling and recovery procedures |
| Data durability | Durable decisions and audit events; no silent data loss on process restart |
| Backup/recovery | Encrypted backups, tested restoration, and institution-approved RPO/RTO; proposed starting target RPO <= 15 minutes and RTO <= 2 hours, subject to BOP approval |
| Security | TLS in transit, encryption at rest, secure secret management, RBAC, MFA, audit and vulnerability management |
| Tenant isolation | No cross-tenant data, model, cache, export or search leakage |
| Maintainability | Automated tests, migrations, structured configuration, documented API contracts and versioned deployments |
| Observability | Metrics, logs, traces, health endpoints and actionable alerts |
| Accessibility | Keyboard navigation, readable contrast, labels and reduced-motion support |
| Internationalisation | Tenant-specific timezone, currency, date/number formatting and English/Arabic-ready UI/report layouts |
| Portability | Repeatable deployment through approved containers and configuration; secrets injected at runtime |

## 20. Security, Privacy and Compliance Controls

- Enforce least-privilege RBAC and tenant-aware authorization on every backend endpoint.
- Use MFA for privileged and AML roles; support SSO/OIDC integration where approved.
- Hash passwords using a modern adaptive password-hashing algorithm; use secure password reset and account lockout controls.
- Store integration credentials in an approved secrets manager/vault. Never store credentials in YAML, logs, source code or exported configuration.
- Use TLS and certificate validation for all external connections; support mTLS for system-to-system integrations.
- Encrypt databases, backups and sensitive report/evidence artifacts at rest; support enterprise KMS/HSM integration where required.
- Mask or tokenize customer identifiers in logs, test payloads and technical dashboards.
- Separate maker/operator/approver duties for critical controls where the institution requires it.
- Record immutable or tamper-evident audit events for logins, user changes, connector changes, policy changes, model promotions, case actions, exports and transaction decisions.
- Protect against replay, duplicate requests, injection, broken object-level authorisation and excessive data extraction.
- Define data classification, consent/lawful basis, retention, deletion, legal hold and data-subject handling with the institution.
- Restrict evidence file types and sizes, scan uploads and log access/download events.
- Maintain a secure SDLC: dependency scanning, container scanning, code review, secrets scanning, penetration testing and patch process.
- Keep operational and AML investigation audit records available for the institution's legally required retention period. FATF Recommendation 11 provides a five-year transaction-record benchmark; BOP Legal/Compliance must confirm applicable Palestinian requirements and any stricter obligations before retention is configured.
- Align controls to the institution's applicable AML/CFT obligations and approved policies. Product configuration is not a substitute for regulatory interpretation or compliance sign-off.

### 20.1 Model governance

- Maintain an inventory of models, owners, intended use, version, training data lineage, features and limitations.
- Report performance by relevant customer/product/channel segments where legally and statistically valid.
- Track false positives, false negatives where labels are available, alert conversion, analyst disposition, drift, coverage and stability.
- Separate model training, validation, approval and production promotion.
- Support shadow mode and challenger testing before new model versions affect decisions.
- Explain risk drivers to analysts in understandable language and preserve exact model/policy versions behind every decision.
- Review model feedback for label quality; analyst “no concern” does not automatically prove a transaction is benign in all contexts.

## 21. Audit, Monitoring and Operational Management

### 21.1 Audit event requirements

Capture at minimum: actor, role, tenant, action, target resource, timestamp, source IP/device where permitted, correlation ID, before/after values for configuration changes, outcome and reason. Audit data must be queryable by authorised auditors and protected against unauthorised alteration.

### 21.2 System performance and failure reporting

IT Admin must be able to produce reports by service/channel/period covering:

- transaction ingestion and scoring volume;
- accepted/rejected/duplicate/retried records;
- connector availability and downtime;
- processing lag and queue depth;
- API p50/p95/p99 latency and error/timeout rate;
- decision service availability and fallback count;
- database and message-broker status;
- model loading/training failure and stale model state;
- failed report jobs and evidence storage errors;
- security and authentication events;
- incident timeline, impact, recovery and operator actions.

### 21.3 Incident workflow

`DETECTED -> ACKNOWLEDGED -> INVESTIGATING -> MITIGATED -> RESOLVED -> POSTMORTEM`

Incidents should have severity, owner, start/end time, affected channels/transactions, customer impact, mitigation, recovery validation and root-cause notes. Critical decision-service failures must alert the designated operations contact.

## 22. Acceptance Criteria

A production release must not be considered plug-and-play until the following testable outcomes are demonstrated.

### 22.1 Deployment and tenant setup

- A clean environment can be installed from documented packages/configuration without manual source-code edits for each tenant.
- A tenant can be created with institution name, timezone, currency, logo, policies and connector settings.
- Tenant isolation tests prove one tenant cannot read another tenant's data or model metadata.
- Database migration and upgrade tests preserve existing data; no normal startup path clears operational records.

### 22.2 User administration

- The bootstrap IT Admin can sign in once with the approved first-run credential, is forced to change it, provides email and enrols MFA.
- IT Admin can create the other four initial accounts using 4-5 digit usernames and required identity fields.
- Temporary password is single-use, expires and requires a password change on first login.
- Backend permission tests enforce the role matrix even when an endpoint is called directly.

### 22.3 Connections and channels

- IT Admin can configure at least one API/webhook connector, one database or event connector and one file-based connector without code modification.
- Test Connection, mapping preview, enable/disable, health and error reporting work.
- Failed and duplicate messages are retained with actionable reasons and safe replay capability.
- Secrets do not appear in logs, exports or UI responses.

### 22.4 Transaction decisions

- A valid request returns a deterministic decision for the same transaction/idempotency key under the same policy/model version.
- All four policy outcomes can be tested: PASS, REVIEW, HOLD and DECLINE/BLOCK.
- The bank-side integration acknowledges hold, release or decline actions and returns an execution status.
- Timeouts, service unavailability and malformed input follow an institution-approved fallback policy.
- Every decision preserves the scores, reason codes, policy version, model version and correlation ID.
- No report or dashboard claims that an asynchronous post-event transaction was blocked.

### 22.5 Customer onboarding and models

- Onboarding import generates a data-quality report.
- A tenant-specific initial segmentation/peer model is built from approved features where sufficient data exists.
- New/undertrained customers are clearly identified and are not automatically treated as high risk solely due to limited history.
- Model version, training run, metrics and approvals are traceable.
- A model can be tested in shadow mode and rolled back.

### 22.6 AML workflow and reporting

- AML Manager can create a transaction/customer referral and assign it to an employee.
- Employee can access only assigned cases, add notes/evidence references and submit a recommendation.
- Recommendation returns to Manager for final disposition; all actions and timestamps are recorded.
- Chief can view oversight dashboards and generate executive reports.
- Manager can configure thresholds, simulate changes and generate branded AML/transaction reports.
- Tenant header/footer/logo/watermark settings are reflected correctly in exported PDF reports.

### 22.7 Demo Mode

- Demo Mode can be enabled and reset by IT Admin.
- Synthetic records are isolated from production data and models.
- Production connectors and transaction execution are disabled in Demo Mode.
- Demo banner and report watermark are visible in all relevant screens/exports.

### 22.8 Operations and performance

- Health/ready/metrics endpoints and dashboards are available.
- Load tests meet the institution-approved transaction throughput and latency target.
- Backup restoration and disaster recovery are exercised and evidenced.
- Security testing covers authentication, RBAC, tenant isolation, secrets, API abuse and audit integrity.

## 23. Delivery Roadmap

### Phase 0 - Discovery and data readiness

- Confirm BOP payment rails, channels, source systems, customer master, KYC fields and data owners.
- Identify approved integration patterns and payment decision time budget.
- Agree risk appetite, AML typologies, thresholds, failover mode and user permissions.
- Profile sample historical data and document quality gaps.
- Confirm deployment, security, legal, retention and regulatory requirements.

### Phase 1 - Enterprise foundation

- Extract scoring engine into a service boundary.
- Build web frontend, platform API, identity/user management and tenant configuration.
- Implement production database, migration, backup and audit services.
- Implement connector registry, field mapping, channel health and core API contracts.
- Implement isolated Demo Mode.

### Phase 2 - Transaction decisioning

- Build synchronous decision endpoint and policy engine.
- Integrate one BOP payment flow in non-production/shadow mode.
- Implement bank-side hold/release/decline adapter and reconciliation.
- Validate latency, resilience, timeout/fallback and idempotency.
- Calibrate thresholds against historical and labelled data.

### Phase 3 - Customer intelligence

- Implement customer/KYC ingestion and data-quality assessment.
- Build tenant onboarding model builder and customer clustering.
- Integrate peer-group and individual behavioural profiles.
- Add model registry, monitoring, explanation and rollback.

### Phase 4 - AML operations

- Implement alerts, cases, assignment, evidence, recommendations and Manager disposition.
- Implement role dashboards and SLA monitoring.
- Implement branded report templates and report catalogue.
- Validate case workflow with AML users and Compliance leadership.

### Phase 5 - Hardening and rollout

- Security assessment, penetration test and operational readiness review.
- Disaster recovery test, load/performance tests and user acceptance testing.
- Shadow-mode production pilot, controlled enforcement pilot and staged rollout.
- Training, support runbooks, service ownership and release/rollback procedures.

## 24. Risks, Dependencies and Mitigations

| Risk/dependency | Potential impact | Mitigation |
|---|---|---|
| No pre-authorisation integration | System can alert but cannot stop a payment before execution | Select a synchronous integration point with Payment/IT teams; clearly separate post-event monitoring |
| Poor customer or transaction data | Unreliable profiles and noisy alerts | Data-quality report, mapping validation, data remediation and phased onboarding |
| Limited history for new customers | Weak individual baselines | Cold-start peer segmentation, conservative monitoring, explicit undertrained state |
| Too many false positives | AML analyst overload and customer friction | Threshold simulation, typology tuning, deduplication, case prioritisation and pilot feedback |
| Model drift or changing customer behaviour | Reduced detection quality | Drift monitoring, retraining governance, challenger testing and rollback |
| Unsafe policy changes | Unintended holds/declines | Simulation, versioning, scheduled activation, approval and rollback |
| Integration outage | Delayed or unavailable decisions | Approved fallback per payment rail, circuit breakers, buffering and operational alerts |
| Misuse of demo data | Confusion with real customer or production results | Isolated storage, visible banner, watermark and disabled execution |
| Over-broad administrator access | Privacy or operational risk | Least privilege, masking, audited break-glass access and separate technical/business roles |
| Regulatory interpretation varies | Incorrect retention, thresholds or reporting workflow | BOP Legal/Compliance sign-off and configurable country/jurisdiction controls |

## 25. Key Assumptions and Decisions to Confirm During Discovery

This BRD makes the following explicit assumptions so implementation can proceed without leaving core requirements ambiguous:

1. The initial BOP installation is provisioned with five named users: one IT Admin, one AML Manager, two AML Employees and one Chief AML/Compliance.
2. `1000/Admin` is the first-run bootstrap credential requested by the owner, but it must be rotated immediately and must never remain active in production.
3. The AML Manager has final internal case disposition authority. Analysts investigate and recommend; they do not make the final Manager decision.
4. Chief AML/Compliance has full oversight and reporting access by default; additional approval powers are optional and must be explicitly configured.
5. Actual payment blocking requires a bank-owned synchronous execution adapter. VeriShield will not directly update core banking tables.
6. `REVIEW`, `HOLD` and `DECLINE/BLOCK` are distinct actions and must be mapped to each bank's payment lifecycle.
7. Demo Mode uses synthetic data and cannot execute live payment actions.
8. Clustering during onboarding is used for peer segmentation and cold-start support, not as a standalone customer risk or block decision.
9. Each customer institution has separate policy, model, connection and branding configuration.
10. BOP Legal, Compliance, Security, IT Architecture and Payment Operations must approve retention, legal basis, failover rules, typologies and production cut-over.

## 26. Suggested Enhancements Backlog

These enhancements are suitable after the first controlled production release, subject to data access, risk approval and value assessment.

### Advanced transaction intelligence

- Graph analytics for connected customers, counterparties, devices, accounts and shared identifiers.
- Multi-entity and cross-channel behaviour correlation.
- Sequence and time-series models to identify emerging patterns.
- Optional supervised models trained on verified fraud outcomes.
- Adaptive segmentation with explainable segment migration.
- Scenario builder for AML typologies and bank-specific rules without code deployments.
- Alert deduplication, clustering and prioritisation by expected investigative value.

### AML investigation productivity

- Reusable case templates and investigation checklists.
- SLA escalation, reminder and workload balancing.
- Related-party network visualisation.
- Evidence packs and case chronology export.
- Controlled integration with document management and approved screening platforms.
- Four-eyes approval workflows for selected case dispositions.
- Regulatory filing preparation workspace with human approval and no automatic submission by default.

### Enterprise integrations

- SSO with corporate identity provider and automated user provisioning.
- ServiceNow/Jira incident creation for IT failures.
- SIEM forwarding and security analytics integration.
- Data warehouse/lakehouse export with privacy controls.
- Additional payment switch, card, wallet, branch and digital channel adapters.
- Client-managed keys and hardware security module integration.
- Tenant-specific private cloud/on-prem deployment packaging.

### AI assistance and MCP expansion

- AML case summarisation with source-linked evidence citations.
- Analyst question-answering over authorised case documents.
- Suggested investigation steps based on internal playbooks.
- Natural-language report generation using approved metrics only.
- MCP tools for read-only case/connector/model insight and confirmed, audited operational actions.
- Retrieval-augmented access to the bank's approved AML policy library; the assistant must cite the internal source and must not autonomously close cases or make payment decisions.

### Governance and performance

- Model risk scorecards and periodic validation packs.
- Fairness/proxy analysis appropriate to the feature set and jurisdiction.
- Automated regression and back-testing after policy/model changes.
- Data lineage and end-to-end decision explainability.
- Cost, capacity and resource efficiency dashboards.
- Multi-region disaster recovery and active/active decision services where required.

## 27. Reference Material

### Source repository

- [BankSealer Fraud Detection - README](https://github.com/abuwalid1998/banksealer-fraud-detection)
- [BankSealer algorithm notes](https://github.com/abuwalid1998/banksealer-fraud-detection/blob/main/docs/ALGORITHM.md)
- [Configuration example](https://github.com/abuwalid1998/banksealer-fraud-detection/blob/main/config/banksealer.yaml)

### External guidance to consider during Compliance/Architecture review

- [FATF Recommendations (February 2025)](https://www.fatf-gafi.org/content/dam/fatf-gafi/recommendations/Feburary%202025%20FATF%20Recommendations.pdf) - risk-based customer due diligence and transaction record-keeping principles.
- [Wolfsberg Group - Statement on Effective Monitoring for Suspicious Activity, Part I (2024)](https://wolfsberg-group.org/resources/2024-statement-on-effective-monitoring-for-suspicious-activity/168) - effectiveness-oriented, risk-based suspicious activity monitoring.
- [Wolfsberg Group - Statement on Effective Monitoring for Suspicious Activity, Part II (2025)](https://wolfsberg-group.org/resources/165/202) - responsible transition to innovation, model-risk balance and explainability.

These references are design inputs, not a legal opinion or a substitute for local regulatory interpretation. The institution must confirm the applicable Palestinian requirements and internal policy obligations before go-live.

---

**End of document - VeriShield BRD v1.0**
