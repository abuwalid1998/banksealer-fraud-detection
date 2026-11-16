# BankSealer algorithm: implementation notes

This project implements the approach of **Carminati, Caron, Maggi, Epifani, Zanero:
"BankSealer: A decision support system for online banking fraud analysis and investigation"**
(Computers & Security, 2015). BankSealer builds three complementary profiles of every customer
from their past transactions and ranks new transactions by how much they deviate from them.
It is unsupervised and needs no fraud labels. Every score comes with an explanation that an
analyst can act on.

| Profile  | Question it answers | Model | Code |
|----------|---------------------|-------|------|
| Local    | Is this transaction unusual **for this customer**? | HBOS, one histogram per feature, per customer and per operation type | `banksealer/engine/local_profile.py` |
| Temporal | Is the **recent activity** of this customer unusual? | Per-customer thresholds (mean + k·std) on sliding-window aggregates | `banksealer/engine/temporal_profile.py` |
| Global   | Does this customer behave **unlike their peers**? | Iterative DBSCAN on Mahalanobis-whitened spending patterns, scored with CBLOF | `banksealer/engine/global_profile.py` |

## Data and preparation

Operation types: `bank_transfer`, `phone_recharge`, `prepaid_card`. Each prepared transaction
carries customer, timestamp, amount, beneficiary (IBAN, phone number or card), beneficiary
country (from the IBAN), IP, `cc_asn` (country and autonomous system of the IP), session and bank/company.

Channels deliver raw records. `banksealer/preparation.py` maps the fields (per-channel mapping
plus aliases), parses timestamps and amounts, normalises operation types, validates required
fields, derives the IBAN country and `cc_asn`, and removes duplicates. Rejected records and the
reason for each are shown in the *Channels* tab.

## 1. Local profile (HBOS)

For customer *u* and operation type *τ*, each feature *i* has a histogram `hist_i`. Its highest
bin is normalised to 1. Categorical features (IBAN, beneficiary country, `cc_asn`, IP, phone
number, operator, card) use frequency histograms. The amount uses log-spaced static bins, and
the hour uses a 24-bin circular histogram. Numeric bins are smoothed towards their neighbours.

```
HBOS_u(t) = Σ_i  w_i · log( 1 / hist_i(t_i) )
hist_i(v) = (count(v) + α) / (max_count + α)         # α: smoothing for never-seen values
```

The longer a customer's history, the more a never-seen value counts: with 100 past transfers to
the same IBAN a new IBAN scores log(100.5/0.5) ≈ 5.3, but with 3 past transfers only ≈ 1.9.

**Undertrained customers** have fewer than `min_history` operations of a type. Their histograms
are shrunk towards the histograms of their peer group (global-profile cluster), or of the whole
population when no cluster is known:

```
hist(v) = λ · hist_user(v) + (1 − λ) · hist_peer(v),   λ = n / (n + k)
```

Normalised score: `s_L = 1 − exp(−HBOS / score_scale)`.
Each feature's term `w_i · log(1/hist_i)` becomes one reason, e.g. *"Beneficiary IBAN LT70…6039
never used before by this customer (0 of 48 past transfers; usual: IT60…3456)"*.

## 2. Temporal profile

For every transaction, the customer's activity in the sliding window `[t − W, t]` (default
W = 24 h) is aggregated into: number of operations, total amount, largest amount, distinct
beneficiaries, distinct connections (`cc_asn`), and transfers to foreign IBANs. Training
computes these aggregates at each past transaction. The per-customer mean μ_f and standard
deviation σ_f, kept with online Welford updates, define the personal threshold:

```
threshold_f = μ_f + k · σ_f               (k = sigma_multiplier, default 3)
raw         = Σ_f w_f · max(0, (x_f − max(threshold_f, x_f_before)) / σ_f)
s_T         = 1 − exp(−raw / score_scale)
```

`x_f_before` is the window value *without* the current transaction, so each transaction is
charged only for the excess it adds. A bigger window caused by an earlier fraud is not charged
again to the legitimate operations that follow. A burst of new operations still accumulates
excess. σ_f has a floor (absolute, and 10% of μ_f), and undertrained customers blend μ and σ
with population statistics.

## 3. Global profile (iterative DBSCAN + CBLOF)

Each customer is summarised by a spending-pattern vector:

* operations and amount per 30 days for each operation type
* average amount
* distinct beneficiaries
* share of foreign transfers
* share of night-time operations

Counts and amounts are log1p-scaled. The vector is then standardised and ZCA-whitened with the
inverse square root of the covariance, so the Euclidean distance in whitened space is the
**Mahalanobis distance**.

**Iterative DBSCAN**: DBSCAN runs with radius `eps`, and the points left as noise are clustered
again with `eps · eps_growth`, for up to `max_iterations` rounds.

**CBLOF** (He et al.): clusters are sorted by size, and the *large* clusters are the smallest
prefix that covers `alpha` of the customers or ends at a size ratio ≥ `beta`.

```
CBLOF(p) = |C| · d(p, centroid(C))                 if C is large
CBLOF(p) = |C| · min_{L large} d(p, centroid(L))   otherwise (small cluster / noise)
s_G      = 1 − exp(−max(0, CBLOF − median) / (q95 − median))
```

The `|C|` factor is optional (`weight_by_cluster_size`). The default is the unweighted variant.
A new transaction is scored with the customer's vector *including* that transaction. Reasons name
the features that deviate most from the peer-group centroid, for example *"recharged amount per
30 days 48.78 is much higher than the group average 0.00"*. The global model is refitted every
`retrain_global_every` scored transactions, and peer-group histograms for the local profile are
rebuilt at the same time.

## 4. Decision, explanation, report

```
S = w_L·s_L + w_T·s_T + w_G·s_G          (weights from config, normalised)
  + high_risk_country_boost  if the beneficiary or IP country is in the bank's high-risk list
  + night_boost              if executed during the bank's night hours
S = max(S, hard_rule_score)  if amount > the bank's max_amount for that operation type
```

The thresholds come from the transaction's **bank/company** (the `thresholds` defaults merged
with `banks.<id>.thresholds`):

* `S ≥ suspicious` → MEDIUM
* `S ≥ high` → HIGH
* `S ≥ critical` → CRITICAL

A single profile above its own alert level (`local_alert`, `temporal_alert`, `global_alert`)
also flags the transaction.

Each reason's impact on S is `w_profile · s_profile · share`, where *share* is the reason's part
of the profile's raw score. Reasons are ranked by this impact into the explanation:

```
HIGH risk: suspicion score 0.74 (threshold 0.50 for Mediterranea Bank). Profile scores - local 0.92, ...
Why:
1. [Bank rule] Involves high-risk country NG (bank list; +0.10 to the score)
2. [Global profile] Spending pattern differs from its peer group #4: ...
3. [Local profile] Connection (country/ASN) NG-AS37148 never used before by this customer ...
```

The PDF report (`banksealer/reports.py`) contains:

* the transaction
* the profile score bars against the bank threshold
* the ranked findings
* the customer's learned behaviour: usual beneficiaries, connections, amounts and peer group
* recommended actions for the risk level

Because the profile scores are kept, **changing the thresholds re-evaluates every scored
transaction instantly**, without rescoring.

## 5. Online learning and feedback

* Every transaction enters the sliding window, because velocity is real activity.
* Only transactions that are not suspicious update the profiles
  (`exclude_suspicious_from_learning`), so an attacker cannot train the model.
* *Mark legitimate* teaches the profiles the analyst-approved behaviour.
* *Retrain profiles* refits on history plus legitimate scored transactions.

## Implementation choices beyond the paper

These details are design decisions of this implementation and can be tuned in the configuration:

* the 0–1 normalisation of each profile score (`1 − exp(−raw/scale)`)
* the weighted combination into one suspicion score
* the bank rules (limits, country list, night hours)
* the "marginal excess" in the temporal profile
* the peer-group shrinkage for undertrained users
* the exact global feature set

## Measured on the mock channel

Run `python -m banksealer.cli evaluate`, which trains on 45 days and scores 10 more days with
injected fraud. With the default configuration it reaches an AUC of about 0.99. Precision is
about 0.94 and recall about 0.83–0.85. Information stealing and prepaid-card drains are caught
almost entirely. Short salami and recharge bursts are the hardest cases.
