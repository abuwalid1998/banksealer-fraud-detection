"""VeriShield command line.

    python -m verishield serve [--host 0.0.0.0] [--port 8000] [--reload]
    python -m verishield init                 create/upgrade the database and first-run records
    python -m verishield reset-demo           delete and regenerate Demo Mode data (LIVE data untouched)
    python -m verishield create-tenant CODE "Legal name" [--currency USD] [--timezone UTC]
    python -m verishield create-api-key NAME --scopes decisions:write,events:write [--tenant BOP] [--space LIVE]
    python -m verishield verify-audit [--tenant BOP]
    python -m verishield make-cert --self-signed --hosts srv01,srv01.bank.local,10.1.2.3 --out C:\\VeriShield\\certs
    python -m verishield make-cert --pfx server.pfx --password-env PFX_PASSWORD --out C:\\VeriShield\\certs
"""
from __future__ import annotations

import argparse
import logging
import sys


def _setup_logging(log_file: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        from logging.handlers import RotatingFileHandler
        import os
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=handlers, force=True)


def _banner(host: str, port: int) -> None:
    from .bootstrap import DEMO_USERS
    from .models import ROLE_LABELS
    from .settings import settings

    shown = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    scheme = "https" if settings.ssl_certfile else "http"
    lines = [
        "",
        "  VeriShield - Transaction Intelligence. Trusted Decisions.",
        f"  Web app:   {scheme}://{shown}:{port}",
        f"  API docs:  {scheme}://{shown}:{port}/api/docs",
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
    logging.getLogger("verishield").info("\n".join(lines))


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
    k.add_argument("--rate-limit", type=int, default=600, help="requests per minute (default 600)")
    mc = sub.add_parser("make-cert", help="create PEM certificate files for HTTPS")
    mc.add_argument("--self-signed", action="store_true", help="generate a self-signed certificate")
    mc.add_argument("--hosts", default="localhost", help="comma separated host names / IP addresses (SAN)")
    mc.add_argument("--pfx", help="convert an existing PFX/P12 certificate (e.g. issued by the bank's CA)")
    mc.add_argument("--password-env", default="", help="environment variable holding the PFX password")
    mc.add_argument("--out", required=True, help="output folder for cert.pem / key.pem")
    v = sub.add_parser("verify-audit", help="verify the tamper-evident audit chain")
    v.add_argument("--tenant")
    args = parser.parse_args(argv)
    from .settings import settings
    _setup_logging(settings.log_file)
    cmd = args.cmd or "serve"
    if cmd == "make-cert":
        from .certs import make_self_signed, pfx_to_pem
        import os
        if args.pfx:
            out = pfx_to_pem(args.pfx, os.environ.get(args.password_env, "") if args.password_env else "", args.out)
        elif args.self_signed:
            out = make_self_signed([h.strip() for h in args.hosts.split(",") if h.strip()], args.out)
        else:
            print("use --self-signed or --pfx", file=sys.stderr)
            return 2
        print(f"certificate: {out['cert']}\nprivate key: {out['key']}\nexpires: {out['expires']}\n"
              f"SHA-1 thumbprint: {out['thumbprint']}" + (f"\nclient trust file: {out['cer']}" if out.get("cer") else ""))
        return 0
    if cmd == "serve":
        import uvicorn
        if getattr(args, "no_demo", False):
            settings.seed_demo = False
        host = getattr(args, "host", None) or settings.host
        port = getattr(args, "port", None) or settings.port
        _banner(host, port)
        ssl = {}
        if settings.ssl_certfile:
            ssl = {"ssl_certfile": settings.ssl_certfile, "ssl_keyfile": settings.ssl_keyfile or None}
        uvicorn.run("verishield.app:app", host=host, port=port, reload=getattr(args, "reload", False),
                    log_level="info", proxy_headers=True, forwarded_allow_ips="*", log_config=None, **ssl)
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
                             key_prefix=prefix, key_hash=hashed, scopes=scopes, created_by="cli",
                             rate_limit_per_minute=args.rate_limit))
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
