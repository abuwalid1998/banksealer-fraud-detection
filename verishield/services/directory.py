"""Sign-in with the bank's Active Directory (LDAP / LDAPS bind) - switched ON by the IT Admin.

A VeriShield user marked ``auth_source = AD`` signs in with the password of his domain account: VeriShield binds
to the domain controller as that account (LDAPS or StartTLS), so domain password rules, expiry and lock-out
apply, and the password is never stored in VeriShield. Optionally the role follows AD group membership
(``group_role_map``) at every sign-in. Users sign in with their 4-5 digit VeriShield username or their domain
account name. Requires the ``ldap3`` package (included in the Windows offline bundle).
"""
from __future__ import annotations

import logging
from typing import Optional

from ..models import Tenant, User

log = logging.getLogger("verishield.directory")

DEFAULTS = {"enabled": False, "server_url": "", "use_starttls": False, "verify_tls": True, "ca_file": "",
            "bind_format": "{account}@bank.local", "base_dn": "", "user_filter": "(sAMAccountName={account})",
            "sync_roles": False, "group_role_map": {}, "timeout_seconds": 8, "allow_local_fallback": True}


def config(tenant: Tenant) -> dict:
    return {**DEFAULTS, **((tenant.settings or {}).get("directory") or {})}


def _escape(value: str) -> str:
    from ldap3.utils.conv import escape_filter_chars
    return escape_filter_chars(value)


def authenticate(tenant: Tenant, account: str, password: str) -> dict:
    """Bind as the user; returns ``{"ok", "message", "groups", "display_name", "email"}``."""
    c = config(tenant)
    if not c["enabled"] or not c["server_url"]:
        return {"ok": False, "message": "directory sign-in is not configured"}
    if not account or not password:
        return {"ok": False, "message": "missing credentials"}
    try:
        import ssl

        from ldap3 import ALL, Connection, Server, Tls
    except ImportError:
        return {"ok": False, "message": "the ldap3 package is not installed on the server"}
    try:
        tls = Tls(validate=ssl.CERT_REQUIRED if c["verify_tls"] else ssl.CERT_NONE,
                  ca_certs_file=c["ca_file"] or None)
        url = c["server_url"]
        server = Server(url, use_ssl=url.lower().startswith("ldaps://"), tls=tls, get_info=ALL,
                        connect_timeout=float(c["timeout_seconds"]))
        user_dn = c["bind_format"].replace("{account}", account)
        conn = Connection(server, user=user_dn, password=password, receive_timeout=float(c["timeout_seconds"]),
                          auto_bind=False, raise_exceptions=False)
        conn.open()
        if c["use_starttls"] and not url.lower().startswith("ldaps://"):
            conn.start_tls()
        if not conn.bind():
            return {"ok": False, "message": "the domain rejected the credentials"}
        info = {"ok": True, "message": "authenticated by the directory", "groups": [], "display_name": "", "email": ""}
        if c["base_dn"]:
            conn.search(c["base_dn"], c["user_filter"].replace("{account}", _escape(account)),
                        attributes=["memberOf", "displayName", "mail"])
            if conn.entries:
                e = conn.entries[0]
                info["groups"] = [str(g) for g in (e.memberOf.values if "memberOf" in e else [])]
                info["display_name"] = str(e.displayName) if "displayName" in e else ""
                info["email"] = str(e.mail) if "mail" in e else ""
        conn.unbind()
        return info
    except Exception as exc:  # noqa: BLE001
        log.warning("directory bind failed: %s", exc)
        return {"ok": False, "message": f"directory unavailable: {type(exc).__name__}", "unavailable": True}


def role_from_groups(tenant: Tenant, groups: list[str]) -> Optional[str]:
    mapping = config(tenant).get("group_role_map") or {}
    lowered = {g.lower(): g for g in groups}
    for group_dn, role in mapping.items():
        if group_dn.lower() in lowered or any(g.split(",")[0].lower() == f"cn={group_dn.lower()}" for g in lowered):
            return role
    return None


def test(tenant: Tenant, account: str, password: str) -> dict:
    res = authenticate(tenant, account, password)
    if res.get("ok"):
        res["mapped_role"] = role_from_groups(tenant, res.get("groups", []))
    return res


__all__ = ["authenticate", "role_from_groups", "config", "test", "User"]
