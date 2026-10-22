"""Standard Web Push (RFC 8030 + RFC 8291 encryption + RFC 8292 VAPID).

The customer app is a web app: its phone gets notifications through the
browser's own push service (Apple, Google, Mozilla), which needs no Firebase
project and no per-shop account — one VAPID key pair for the whole platform,
and every shop's customers subscribe with it.

Written on `cryptography` (already installed through PyJWT[crypto]) instead of
pywebpush, whose http-ece dependency does not build on current Python.

Keys (settings, from the environment — never in the repository):
    WEBPUSH_PUBLIC_KEY    base64url, uncompressed P-256 point (65 bytes)
    WEBPUSH_PRIVATE_KEY   base64url, the private scalar (32 bytes)
    WEBPUSH_SUBJECT       mailto:you@example.com
Make a pair with:  python manage.py make_vapid_keys
Without keys, sending is skipped quietly (the in-app feed still has everything).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from urllib.parse import urlsplit

from django.conf import settings

log = logging.getLogger(__name__)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    text = (text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_key() -> str:
    return (getattr(settings, "WEBPUSH_PUBLIC_KEY", "") or "").strip()


def configured() -> bool:
    return bool(public_key() and (getattr(settings, "WEBPUSH_PRIVATE_KEY", "") or "").strip())


def make_keys() -> tuple[str, str]:
    """A new (public, private) pair, both base64url."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    return b64u(pub), b64u(priv)


def _private_key():
    from cryptography.hazmat.primitives.asymmetric import ec

    raw = unb64u(getattr(settings, "WEBPUSH_PRIVATE_KEY", ""))
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


# ---------------------------------------------------------------------------
# RFC 8291: the message, encrypted for one browser.


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None, server_key=None) -> bytes:
    """aes128gcm body for one subscription (a single record)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = as_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = as_key.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!L", 4096) + bytes([len(as_public)]) + as_public
    return header + body


# ---------------------------------------------------------------------------
# RFC 8292: who is sending (VAPID).


def vapid_header(endpoint: str) -> str:
    import jwt

    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(time.time()) + 12 * 3600,
        "sub": getattr(settings, "WEBPUSH_SUBJECT", "") or "mailto:admin@example.com",
    }
    token = jwt.encode(claims, _private_key(), algorithm="ES256")
    return f"vapid t={token}, k={public_key()}"


def send(sub, data: dict, ttl: int = 3600) -> str:
    """Deliver to one PushSubscription row. Returns 'ok', 'gone' or 'error'."""
    import requests

    if not configured():
        return "error"
    try:
        body = encrypt(json.dumps(data, ensure_ascii=False).encode(), sub.p256dh, sub.auth)
        resp = requests.post(
            sub.endpoint,
            data=body,
            headers={
                "Authorization": vapid_header(sub.endpoint),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(ttl),
                # "Your order is ready" is worth waking the phone for.
                "Urgency": "high",
                # One notification per order on the lock screen: a newer status replaces the older.
                **({"Topic": str(data.get("tag"))[:32]} if data.get("tag") else {}),
            },
            timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("webpush: transport failed: %s", exc)
        return "error"
    if resp.status_code in (200, 201, 202):
        return "ok"
    if resp.status_code in (404, 410):
        return "gone"
    log.warning("webpush: %s %s", resp.status_code, (resp.text or "")[:200])
    return "error"
