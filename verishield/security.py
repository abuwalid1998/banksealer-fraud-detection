"""Security primitives: password hashing, TOTP MFA, tokens, API keys, secrets vault, masking."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote

from .settings import settings

# --------------------------------------------------------------------------- passwords
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    """scrypt (memory-hard, adaptive) with a per-password random salt."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${base64.b64encode(salt).decode()}$" \
           f"{base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r),
                                p=int(p), dklen=len(base64.b64decode(digest_b64)))
        return hmac.compare_digest(digest, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False


def password_problems(password: str, policy: dict, username: str = "") -> list[str]:
    problems = []
    min_len = int(policy.get("password_min_length", 10))
    if len(password) < min_len:
        problems.append(f"at least {min_len} characters")
    if not re.search(r"[A-Z]", password):
        problems.append("an upper-case letter")
    if not re.search(r"[a-z]", password):
        problems.append("a lower-case letter")
    if not re.search(r"\d", password):
        problems.append("a digit")
    if policy.get("password_require_symbol", True) and not re.search(r"[^A-Za-z0-9]", password):
        problems.append("a symbol")
    if username and username in password:
        problems.append("must not contain the username")
    if password.lower() in ("admin", "password", "verishield", "changeme"):
        problems.append("must not be a default password")
    return problems


_WORDS = ("Amber", "Atlas", "Birch", "Cedar", "Coral", "Delta", "Ember", "Falcon", "Harbor", "Indigo", "Juniper",
          "Lumen", "Maple", "Nova", "Olive", "Orbit", "Pearl", "Quartz", "Raven", "Sierra", "Spruce", "Summit",
          "Tide", "Willow")


def temporary_password() -> str:
    """Human-friendly temporary password, e.g. ``Cedar-Harbor-4821!``."""
    a, b = secrets.choice(_WORDS), secrets.choice(_WORDS)
    return f"{a}-{b}-{secrets.randbelow(9000) + 1000}!"


# --------------------------------------------------------------------------- tokens
def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hmac.new(settings.secret_key.encode(), token.encode(), hashlib.sha256).hexdigest()


def new_api_key(prefix: str = "") -> tuple[str, str, str]:
    """Returns ``(full_key, prefix, hash)``; only the hash is stored. A rotation keeps the prefix."""
    prefix = prefix or "vs_" + secrets.token_hex(4)
    key = f"{prefix}.{secrets.token_urlsafe(32)}"
    return key, prefix, token_hash(key)


# --------------------------------------------------------------------------- TOTP (RFC 6238)
def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{code:0{digits}d}"


def totp_now(secret: str, at: Optional[float] = None) -> str:
    return _hotp(secret, int((at or time.time()) // 30))


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return False
    counter = int(time.time() // 30)
    return any(hmac.compare_digest(_hotp(secret, counter + d), code) for d in range(-window, window + 1))


def totp_uri(secret: str, account: str, issuer: str = "VeriShield") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"


# --------------------------------------------------------------------------- secrets vault
# Key ring: ``vault.keys`` in the data directory (or VERISHIELD_VAULT_KEY_FILE, e.g. on a separately protected
# volume). The first key encrypts; every key - and the legacy key derived from the server secret - decrypts, so a
# rotation never makes stored secrets unreadable. ``rotate_vault_key`` adds a key and re-encrypts everything.
def keyring_path():
    from pathlib import Path
    custom = os.environ.get("VERISHIELD_VAULT_KEY_FILE", "")
    return Path(custom) if custom else settings.data_dir / "vault.keys"


def _legacy_key() -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(("vault:" + settings.secret_key).encode()).digest())


def keyring() -> list[dict]:
    import json
    path = keyring_path()
    try:
        keys = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, ValueError):
        keys = []
    return [k for k in keys if k.get("key")]


def _save_keyring(keys: list[dict]) -> None:
    import json
    path = keyring_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keys, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:  # pragma: no cover - Windows ACLs are set by the installer
        pass
    os.replace(tmp, path)


def _fernet():
    try:
        from cryptography.fernet import Fernet, MultiFernet
    except ImportError:  # pragma: no cover - cryptography is a declared dependency
        return None
    keys = [Fernet(k["key"].encode()) for k in keyring()] + [Fernet(_legacy_key())]
    return MultiFernet(keys)


def vault_status() -> dict:
    keys = keyring()
    return {"keys": [{"id": k["id"], "created": k.get("created", ""), "primary": i == 0} for i, k in enumerate(keys)],
            "primary": keys[0]["id"] if keys else "LEGACY", "primary_created": keys[0].get("created") if keys else "",
            "file": str(keyring_path()), "legacy_in_use": not keys}


def rotate_vault_key(db) -> dict:
    """New primary key; every stored secret and MFA seed is re-encrypted with it. Returns counts."""
    from cryptography.fernet import Fernet
    from datetime import datetime, timezone
    from .models import SecretRecord, User
    keys = keyring()
    new = {"id": f"K{len(keys) + 1}-{secrets.token_hex(3)}", "key": Fernet.generate_key().decode(),
           "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _save_keyring([new] + keys)
    f = _fernet()
    counts = {"secrets": 0, "mfa_seeds": 0, "failed": 0}
    from sqlalchemy import select
    for rec in db.execute(select(SecretRecord)).scalars():
        try:
            rec.ciphertext = f.rotate(rec.ciphertext.encode()).decode()
            counts["secrets"] += 1
        except Exception:  # noqa: BLE001 - unreadable value: keep it, report it
            counts["failed"] += 1
    for u in db.execute(select(User).where(User.mfa_secret.is_not(None))).scalars():
        if not u.mfa_secret:
            continue
        try:
            u.mfa_secret = f.rotate(u.mfa_secret.encode()).decode()
            counts["mfa_seeds"] += 1
        except Exception:  # noqa: BLE001
            counts["failed"] += 1
    db.commit()
    counts["primary"] = new["id"]
    return counts


def retire_old_keys(db) -> dict:
    """Keep only the primary key, after checking that every stored value decrypts with it."""
    from cryptography.fernet import Fernet, InvalidToken
    from sqlalchemy import select
    from .models import SecretRecord, User
    keys = keyring()
    if not keys:
        raise ValueError("rotate the vault key first")
    primary = Fernet(keys[0]["key"].encode())
    values = [r.ciphertext for r in db.execute(select(SecretRecord)).scalars()] + \
             [u.mfa_secret for u in db.execute(select(User).where(User.mfa_secret.is_not(None))).scalars() if u.mfa_secret]
    for v in values:
        try:
            primary.decrypt(v.encode())
        except InvalidToken:
            raise ValueError("some values are still encrypted with an older key - rotate again first")
    _save_keyring(keys[:1])
    return {"retired": len(keys) - 1, "primary": keys[0]["id"]}


def encrypt_secret(value: str) -> str:
    f = _fernet()
    if f is None:
        raise RuntimeError("the 'cryptography' package is required for the local secrets vault")
    return f.encrypt(value.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    f = _fernet()
    if f is None:
        raise RuntimeError("the 'cryptography' package is required for the local secrets vault")
    return f.decrypt(ciphertext.encode()).decode()


def encrypt_field(value: str) -> str:
    """Encrypt a small sensitive field (e.g. a TOTP seed) at rest."""
    return encrypt_secret(value)


def decrypt_field(value: str) -> str:
    return decrypt_secret(value)


def check_url(url: str) -> str:
    """Outbound URLs configured by administrators: only http(s), no file:, ftp:, data: ..."""
    from urllib.parse import urlparse
    parts = urlparse(url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("only http:// or https:// URLs are allowed")
    return url


def sign_blob(blob: bytes) -> str:
    """HMAC of an internal binary artefact (model checkpoints) so that tampered data is never unpickled."""
    return hmac.new(("blob:" + settings.secret_key).encode(), blob, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- masking
def mask(value: Optional[str], keep: int = 3) -> str:
    if not value:
        return ""
    text = str(value)
    if len(text) <= keep * 2:
        return text[0] + "•" * (len(text) - 1)
    return text[:keep] + "•" * min(6, len(text) - keep * 2) + text[-keep:]


def mask_ip(ip: Optional[str]) -> str:
    if not ip:
        return ""
    parts = ip.split(".")
    return ".".join(parts[:2] + ["x", "x"]) if len(parts) == 4 else mask(ip)


def sanitize_error(message: str) -> str:
    """Remove anything that looks like a credential from an error message before it is stored/shown."""
    message = re.sub(r"(?i)(password|passwd|pwd|token|secret|api[_-]?key|authorization)\s*[=:]\s*\S+", r"\1=***",
                     message)
    message = re.sub(r"://[^/@\s]+:[^/@\s]+@", "://***:***@", message)
    return message[:500]
