"""VeriShield command line.

    python -m verishield serve [--host 0.0.0.0] [--port 8000] [--reload]
    python -m verishield init                 create/upgrade the database and first-run records
    python -m verishield reset-demo           delete and regenerate Demo Mode data (LIVE data untouched)
    python -m verishield create-tenant CODE "Legal name" [--currency USD] [--timezone UTC]
    python -m verishield create-api-key NAME --scopes decisions:write,events:write [--tenant BOP] [--space LIVE]
    python -m verishield verify-audit [--tenant BOP]
"""
from __future__ import annotations

import argparse
import logging
import sys


def _banner(host: str, port: int) -> None:
    from .bootstrap import DEMO_USERS
    from .models import ROLE_LABELS
    from .settings import settings

    shown = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    lines = [
        "",
        "  VeriShield - Transaction Intelligence. Trusted Decisions.",
        f"  Web app:   http://{shown}:{port}",
        f"  API docs:  http://{shown}:{port}/api/docs",
        f"  Data dir:  {settings.data_dir}",
        f"  Database:  {settings.database_url.split('@')[-1] if '@' in settings.database_url else settings.database_url}",
        "",
        f"  First sign-in (IT Admin): username {settings.bootstrap_username} / password "
        f"{'(from VERISHIELD_BOOTSTRAP_PASSWORD)' if settings.production else settings.bootstrap_password}"
        "  -> you must change it",
    ]
    if settings.seed_demo_users and not settings.production:
        lines.append("  Development accounts (temporary passwords, changed at first sign-in):")
        for username, first, last, _, role, password in DEMO_USERS:
            lines.append(f"     {username}  {password:<15} {ROLE_LABELS[role]} ({first} {last})")
    lines.append("")
    print("\n".join(lines), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verishield", description="VeriShield platform")
    sub = parser.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the web application")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--reload", action="store_true")
    s.add_argument("--no-demo", action="store_true", help="do not generate synthetic demo data")
    sub.add_parser("init", help="create/upgrade the database")
    sub.add_parser("reset-demo", help="regenerate Demo Mode data")
    t = sub.add_parser("create-tenant", help="onboard another institution")
    t.add_argument("code")
    t.add_argument("name")
    t.add_argument("--currency", default="USD")
    t.add_argument("--timezone", default="UTC")
    k = sub.add_parser("create-api-key", help="create a system API key")
    k.add_argument("name")
    k.add_argument("--scopes", default="decisions:write,events:write,execution:write")
    k.add_argument("--tenant")
    k.add_argument("--space", default="LIVE", choices=["LIVE", "DEMO"])
    v = sub.add_parser("verify-audit", help="verify the tamper-evident audit chain")
    v.add_argument("--tenant")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    from .settings import settings
    cmd = args.cmd or "serve"
    if cmd == "serve":
        import uvicorn
        if getattr(args, "no_demo", False):
            settings.seed_demo = False
        host = getattr(args, "host", None) or settings.host
        port = getattr(args, "port", None) or settings.port
        _banner(host, port)
        uvicorn.run("verishield.app:app", host=host, port=port, reload=getattr(args, "reload", False),
                    log_level="info", proxy_headers=True, forwarded_allow_ips="*")
        return 0

    from .bootstrap import initialise
    from .db import session_scope
    if cmd == "init":
        print(initialise())
        return 0
    if cmd == "reset-demo":
        initialise(seed_demo=False)
        from .services import demo
        with session_scope() as db:
            print(demo.reset(db, settings.tenant_code, "cli"))
        return 0
    if cmd == "create-tenant":
        initialise(seed_demo=False)
        from .bootstrap import ensure_bootstrap_admin, ensure_tenant
        from .services.policy import ensure_default_policy
        with session_scope() as db:
            tenant = ensure_tenant(db, args.code.upper(), args.name, args.currency.upper(), args.timezone, False)
            created = ensure_bootstrap_admin(db, tenant)
            db.flush()
            ensure_default_policy(db, tenant.id)
        print(f"Tenant {args.code.upper()} ready." + (f" Bootstrap IT Admin: {settings.bootstrap_username} "
                                                       "(sign in with institution code "
                                                       f"{args.code.upper()} and change the password)."
                                                       if created else ""))
        return 0
    if cmd == "create-api-key":
        initialise(seed_demo=False)
        from .deps import API_SCOPES
        from .models import ApiClient
        from .security import new_api_key
        scopes = [x.strip() for x in args.scopes.split(",") if x.strip()]
        bad = [x for x in scopes if x not in API_SCOPES]
        if bad:
            print(f"unknown scope(s): {', '.join(bad)}; valid: {', '.join(API_SCOPES)}", file=sys.stderr)
            return 2
        key, prefix, hashed = new_api_key()
        with session_scope() as db:
            db.add(ApiClient(tenant_id=(args.tenant or settings.tenant_code).upper(), space=args.space, name=args.name,
                             key_prefix=prefix, key_hash=hashed, scopes=scopes, created_by="cli"))
        print(f"API key (shown once): {key}")
        return 0
    if cmd == "verify-audit":
        initialise(seed_demo=False)
        from . import audit
        with session_scope() as db:
            print(audit.verify_chain(db, (args.tenant or settings.tenant_code).upper()))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
