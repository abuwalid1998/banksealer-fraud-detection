"""Security posture self-check for the IT Admin (and the security team before go-live)."""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ApiClient, Tenant, User, utcnow
from ..security import verify_password, vault_status
from ..settings import settings

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"


def _check(cid: str, area: str, title: str, status: str, detail: str, fix: str = "") -> dict:
    return {"id": cid, "area": area, "title": title, "status": status, "detail": detail, "fix": fix}


def posture(db: Session, tenant: Tenant) -> dict:
    sec = (tenant.settings or {}).get("security", {})
    checks: list[dict] = []
    add = checks.append

    # ---------------------------------------------------------------- transport & platform
    tls = bool(settings.ssl_certfile) or settings.trust_proxy
    add(_check("tls", "Transport", "HTTPS for the web UI and API", PASS if tls else (FAIL if settings.production else WARN),
               "HTTPS is enabled" + (" (terminated by the load balancer)" if settings.trust_proxy else "")
               if tls else "the server listens on plain HTTP",
               "Set VERISHIELD_SSL_CERTFILE / VERISHIELD_SSL_KEYFILE, or terminate TLS on the load balancer and set "
               "VERISHIELD_TRUST_PROXY=1."))
    add(_check("env", "Platform", "Production mode", PASS if settings.production else WARN,
               f"environment = {settings.env}", "Set VERISHIELD_ENV=production on live servers."))
    weak_secret = len(settings.secret_key or "") < 32
    add(_check("secret", "Platform", "Server secret key strength", FAIL if weak_secret else PASS,
               "shorter than 32 characters" if weak_secret else f"{len(settings.secret_key)} characters",
               "Set VERISHIELD_SECRET_KEY to a random value of at least 32 characters (or let the installer create it)."))
    add(_check("cors", "Platform", "Cross-origin access", FAIL if "*" in settings.cors_origins else PASS,
               ", ".join(settings.cors_origins) or "same origin only", "Remove '*' from VERISHIELD_CORS_ORIGINS."))
    add(_check("metrics", "Platform", "Metrics endpoint protection", PASS if settings.metrics_token else WARN,
               "bearer token required" if settings.metrics_token else "/metrics is readable without a token",
               "Set VERISHIELD_METRICS_TOKEN and configure it in Prometheus."))
    add(_check("db", "Platform", "Database engine", PASS if db.get_bind().dialect.name == "postgresql" else
               (WARN if settings.production else INFO), db.get_bind().dialect.name,
               "Use PostgreSQL in production (backups, replication, cluster mode)."))

    # ---------------------------------------------------------------- identities
    boot = db.execute(select(User).where(User.tenant_id == tenant.id, User.is_bootstrap.is_(True))).scalar()
    default_pw = bool(boot and boot.password_hash and verify_password(settings.bootstrap_password, boot.password_hash))
    add(_check("bootstrap", "Identity", "Initial administrator password changed", FAIL if default_pw else PASS,
               "the initial IT Admin still uses the published password" if default_pw else "changed",
               "Sign in as the initial IT Admin and change the password, or disable the account."))
    add(_check("fixed_pw", "Identity", "Demo test accounts with fixed passwords",
               FAIL if settings.demo_fixed_passwords and settings.production else (WARN if settings.demo_fixed_passwords else PASS),
               "enabled" if settings.demo_fixed_passwords else "disabled",
               "Set VERISHIELD_DEMO_FIXED_PASSWORDS=0 and VERISHIELD_SEED_DEMO_USERS=0 on live servers."))
    users = db.execute(select(User).where(User.tenant_id == tenant.id, User.status == "ACTIVE")).scalars().all()
    local = [u for u in users if (u.auth_source or "LOCAL") == "LOCAL"]
    no_mfa = [u for u in local if not u.mfa_enabled]
    mfa_on = bool(sec.get("mfa_required")) or settings.require_mfa
    add(_check("mfa", "Identity", "Multi-factor authentication",
               PASS if mfa_on and not no_mfa else (WARN if mfa_on else FAIL if settings.production else WARN),
               f"policy {'ON' if mfa_on else 'OFF'}; {len(no_mfa)} of {len(local)} local accounts without MFA",
               "Turn on 'MFA required' in Users & Settings › Security policy (AD accounts use the bank's MFA)."))
    ad = (tenant.settings or {}).get("directory", {}).get("enabled")
    add(_check("ad", "Identity", "Central sign-in (Active Directory)", PASS if ad else INFO,
               "enabled" if ad else "local accounts only", "Connect Active Directory in Users & Settings."))
    minlen = int(sec.get("password_min_length", 10))
    add(_check("pwlen", "Identity", "Password length policy", PASS if minlen >= 12 else WARN, f"minimum {minlen}",
               "Set the minimum password length to 12 or more."))
    idle = int(sec.get("session_idle_minutes", 60))
    add(_check("idle", "Identity", "Session idle timeout", PASS if idle <= 30 else WARN, f"{idle} minutes",
               "Set the idle timeout to 30 minutes or less."))
    add(_check("lockout", "Identity", "Account lockout", PASS if int(sec.get("lockout_threshold", 5)) <= 5 else WARN,
               f"after {sec.get('lockout_threshold', 5)} failed attempts"))

    # ---------------------------------------------------------------- keys
    vs = vault_status()
    age_days = None
    if vs["primary_created"]:
        from datetime import datetime
        age_days = (utcnow() - datetime.fromisoformat(vs["primary_created"]).replace(tzinfo=None)).days
    add(_check("vault", "Keys", "Vault key rotation",
               WARN if vs["legacy_in_use"] or (age_days or 0) > 365 else PASS,
               "derived from the server secret (never rotated)" if vs["legacy_in_use"] else
               f"primary key {vs['primary']} is {age_days} days old",
               "Rotate the vault key (Security tab) at least yearly and after any staff change in IT."))
    stale = db.execute(select(func.count()).select_from(ApiClient).where(
        ApiClient.tenant_id == tenant.id, ApiClient.active.is_(True),
        func.coalesce(ApiClient.rotated_at, ApiClient.created_at) < utcnow() - timedelta(days=180))).scalar()
    add(_check("apikeys", "Keys", "API keys rotated in the last 180 days", WARN if stale else PASS,
               f"{stale} active key(s) older than 180 days" if stale else "all keys rotated recently",
               "Rotate channel keys (API clients) - the old key keeps working for the grace period."))

    # ---------------------------------------------------------------- monitoring & data
    from ..audit import verify_chain
    chain = verify_chain(db, tenant.id)
    add(_check("audit", "Monitoring", "Audit trail integrity", PASS if chain["valid"] else FAIL,
               f"{chain['checked']:,} events verified" if chain["valid"] else f"chain broken at event {chain['broken_at']}",
               "Investigate immediately; restore from backup and compare."))
    siem = (tenant.settings or {}).get("siem", {})
    add(_check("siem", "Monitoring", "Security events forwarded to the SIEM", PASS if siem.get("enabled") else WARN,
               f"{siem.get('protocol', '').upper()} {siem.get('host', '')}:{siem.get('port', '')}" if siem.get("enabled")
               else "not configured", "Configure SIEM forwarding in Integrations › Monitoring & SIEM."))
    ai = (tenant.settings or {}).get("ai", {})
    if ai.get("enabled"):
        add(_check("ai", "Data protection", "AI assistant masks identifiers", PASS if ai.get("mask_identifiers", True) else FAIL,
                   "masking ON" if ai.get("mask_identifiers", True) else "customer names / accounts are sent unmasked",
                   "Turn on masking in AI Agents › AI assistant."))
    mcp = (tenant.settings or {}).get("mcp", {})
    if mcp.get("enabled"):
        add(_check("mcp", "Data protection", "AI-agent access limited", PASS if int(mcp.get("max_calls_per_minute", 60)) <= 120
                   else WARN, f"{len(mcp.get('tools', []))} tools, {mcp.get('max_calls_per_minute', 60)} calls/min"))
    scan = settings.data_dir / "security_scan.json"
    if scan.exists():
        try:
            data = json.loads(scan.read_text(encoding="utf-8"))
            high = int(data.get("high", 0))
            add(_check("scan", "Software", "Dependency & code scan", FAIL if high else PASS,
                       f"{data.get('date', '')}: {high} high, {data.get('medium', 0)} medium findings",
                       "Run deploy/security/scan.py and update the affected libraries."))
        except (OSError, ValueError):
            pass
    else:
        add(_check("scan", "Software", "Dependency & code scan", INFO, "no scan report on this server",
                   "Run python deploy/security/scan.py before each release."))
    add(_check("backup", "Operations", "Backups and restore drills", INFO, "checked outside VeriShield",
               "Daily PostgreSQL backups, quarterly restore drill, then verify the audit chain."))

    scored = [c for c in checks if c["status"] in (PASS, WARN, FAIL)]
    points = sum(1.0 if c["status"] == PASS else 0.5 if c["status"] == WARN else 0 for c in scored)
    score = round(100 * points / max(1, len(scored)))
    return {"score": score, "grade": "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D",
            "fail": sum(1 for c in checks if c["status"] == FAIL), "warn": sum(1 for c in checks if c["status"] == WARN),
            "checks": checks, "vault": vs, "checked_at": utcnow().isoformat(timespec="seconds") + "Z"}
