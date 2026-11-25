# VeriShield high availability

VeriShield runs as one or more identical application servers that share one PostgreSQL database.

```
                         ┌──────────────────────┐
 channels / browsers ──► │ load balancer (HTTPS)│ health check: GET /ready
                         └──────┬────────┬──────┘
                                │        │
                     ┌──────────▼─┐   ┌──▼─────────┐
                     │ app node 1 │   │ app node 2 │   every node scores decisions
                     │  (LEADER)  │   │ (FOLLOWER) │   the leader also runs channels + jobs
                     └──────┬─────┘   └─────┬──────┘
                            └──────┬────────┘
                         ┌─────────▼─────────┐   streaming replica ┌──────────────────┐
                         │ PostgreSQL primary │ ──────────────────► │ PostgreSQL standby│
                         └───────────────────┘                      └──────────────────┘
```

## What is shared and what is elected

| Concern | How it works in a cluster |
|---|---|
| Decisions (`POST /api/v1/decisions/evaluate`) | Any node. Idempotency keys, sequence numbers and sessions are in the database. |
| Channel connectors, background jobs | Only the **leader**: the node holding the `leader` lease in `cluster_leases`. The lease is renewed every 10 s; if the leader stops, another node takes over after 30 s and starts the channels. |
| Learned customer profiles | Every node absorbs the transactions scored by the other nodes every 10 s. The leader writes a signed checkpoint of each active model every 5 minutes; a starting node loads it. |
| Audit trail (hash chain) | Appends are serialised with a row lock (`SELECT … FOR UPDATE`) so the chain never forks. |
| Secrets vault | Same `VERISHIELD_SECRET_KEY` and the same key-ring file (`vault.keys`, or `VERISHIELD_VAULT_KEY_FILE`) on every node. |
| Evidence files, reports, logo | Put the data directory on a shared volume (SMB / NFS) or use the same path on a replicated volume. |

## Settings on every node

```
VERISHIELD_ENV=production
VERISHIELD_DATABASE_URL=postgresql://verishield:***@db.bop.local:5432/verishield
VERISHIELD_SECRET_KEY=<same 64-character value on every node>
VERISHIELD_CLUSTER=1
VERISHIELD_NODE_ID=app1            # unique per node
VERISHIELD_TRUST_PROXY=1           # TLS terminated by the load balancer
VERISHIELD_METRICS_TOKEN=<token for Prometheus>
```

SQLite cannot be shared between servers: cluster mode requires PostgreSQL (the Cluster tab warns otherwise).

## Load balancer

Route each customer to the same node when possible, so that velocity checks over a few seconds see every payment
of that customer at once. Send the customer id in a header (`X-Customer-Id`) from the channel and hash on it.

HAProxy:

```
backend verishield
    balance hdr(X-Customer-Id)
    hash-type consistent
    option httpchk GET /ready
    http-check expect status 200
    server app1 10.0.10.11:8443 check ssl verify required ca-file /etc/haproxy/bank-ca.pem
    server app2 10.0.10.12:8443 check ssl verify required ca-file /etc/haproxy/bank-ca.pem
```

nginx:

```
upstream verishield {
    hash $http_x_customer_id consistent;
    server 10.0.10.11:8443 max_fails=2 fail_timeout=10s;
    server 10.0.10.12:8443 max_fails=2 fail_timeout=10s;
}
```

Windows Network Load Balancing or F5 / Citrix ADC work the same way: health monitor on `GET /ready`, persistence on
the `X-Customer-Id` header (or source address when the header is not available).

## Database

* PostgreSQL streaming replication to a standby, with automatic failover (Patroni, repmgr or the bank's standard
  cluster product). Point `VERISHIELD_DATABASE_URL` at the virtual IP / DNS name of the primary.
* Daily backups and a quarterly restore drill; after a restore, check *Users & Settings › Security posture* - it
  verifies the audit chain.

## Failure behaviour

| Failure | Effect |
|---|---|
| One application node | The load balancer stops sending to it; if it was the leader, another node takes the lease within 30 s and restarts the channels. Decisions continue on the remaining node(s). |
| Database primary | Decisions fail until failover completes; channels apply the institution-approved fallback per transaction type (fail open or hold). |
| SIEM / SMS / e-mail gateway | Events wait in the database (SIEM cursor, outbox); nothing is lost. |
