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


def new_api_key() -> tuple[str, str, str]:
    """Returns ``(full_key, prefix, hash)``; only the hash is stored."""
    prefix = "vs_" + secrets.token_hex(4)
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
def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:  # pragma: no cover - cryptography is a declared dependency
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(("vault:" + settings.secret_key).encode()).digest())
    return Fernet(key)


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
