"""HTTPS certificate helpers (self-signed for pilots, or conversion of a bank-issued PFX to PEM)."""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
from pathlib import Path


def _write(out_dir: str, cert, key) -> dict:
    from cryptography.hazmat.primitives import hashes, serialization

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cert_path, key_path, cer_path = out / "cert.pem", out / "key.pem", out / "verishield.cer"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))
    cer_path.write_bytes(cert.public_bytes(serialization.Encoding.DER))   # for distribution to client trust stores
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    expires = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    return {"cert": str(cert_path), "key": str(key_path), "cer": str(cer_path),
            "expires": expires.isoformat(), "thumbprint": cert.fingerprint(hashes.SHA1()).hex().upper()}


def make_self_signed(hosts: list[str], out_dir: str, days: int = 825) -> dict:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = []
    for h in hosts or ["localhost"]:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            names.append(x509.DNSName(h))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0] if hosts else "localhost"),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VeriShield")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    return _write(out_dir, cert, key)


def pfx_to_pem(pfx_path: str, password: str, out_dir: str) -> dict:
    from cryptography.hazmat.primitives.serialization import pkcs12

    key, cert, _chain = pkcs12.load_key_and_certificates(Path(pfx_path).read_bytes(),
                                                         password.encode() if password else None)
    if key is None or cert is None:
        raise ValueError("the PFX file must contain the certificate and its private key")
    out = _write(out_dir, cert, key)
    if _chain:                                    # append intermediate certificates to the chain
        from cryptography.hazmat.primitives import serialization
        with open(out["cert"], "ab") as fh:
            for c in _chain:
                fh.write(c.public_bytes(serialization.Encoding.PEM))
    return out
