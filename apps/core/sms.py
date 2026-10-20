"""Sending SMS through HTD (sms.htd.ps) — the HTTP JSON API.

Docs: "HTTP-API Manual" v14.3. POST https://sms.htd.ps/API/SendJSON.aspx with
id (API ID, secret), sender (approved sender name), to (international number,
no +), msg, mode=0. The reply carries a text status; "Message Sent
Successfully" is the only success.

Dry run when no API ID is configured (or SMS_DRY_RUN=true): nothing leaves the
server, the log row says "dry_run", and the caller still gets a result, so the
whole sign-in flow can be tested without spending credit.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from django.conf import settings

log = logging.getLogger(__name__)

# Characters the GSM-7 alphabet carries; anything else (Arabic) means UCS-2,
# where one SMS holds 70 characters and a multi-part one 67 each.
_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)


def segments(msg: str) -> int:
    if all(ch in _GSM7 for ch in msg):
        return 1 if len(msg) <= 160 else -(-len(msg) // 153)
    return 1 if len(msg) <= 70 else -(-len(msg) // 67)


def international(local: str) -> str | None:
    """059/056 → 970… (Palestinian networks); other 05x → 972…. None if not a mobile."""
    d = re.sub(r"\D", "", local or "")
    if d.startswith("00"):
        d = d[2:]
    if d.startswith(("970", "972")) and len(d) == 12:
        return d
    if re.fullmatch(r"05\d{8}", d):
        cc = "970" if d[:3] in ("059", "056") else "972"
        return cc + d[1:]
    return None


@dataclass
class SmsResult:
    ok: bool
    dry_run: bool
    response: str


def send(local_phone: str, msg: str) -> SmsResult:
    to = international(local_phone)
    if not to:
        return SmsResult(False, False, "Invalid TO Parameters (local check)")
    api_id = (getattr(settings, "HTD_SMS_API_ID", "") or "").strip()
    sender = getattr(settings, "HTD_SMS_SENDER", "COOP")
    return send_as(to, msg, sender=sender, api_id=api_id)


def send_as(to: str, msg: str, *, sender: str, api_id: str) -> SmsResult:
    if getattr(settings, "SMS_DRY_RUN", True) or not api_id:
        log.info("sms dry-run to=%s… len=%s", to[:6], len(msg))
        return SmsResult(True, True, "dry_run")
    import requests

    try:
        resp = requests.post(
            getattr(settings, "HTD_SMS_URL", "https://sms.htd.ps/API/SendJSON.aspx"),
            data={"id": api_id, "sender": sender, "to": to, "msg": msg, "mode": "0"},
            timeout=15,
        )
        text = (resp.text or "").strip()
    except Exception as exc:  # network down, DNS, timeout
        log.warning("sms send failed: %s", exc)
        return SmsResult(False, False, f"error: {type(exc).__name__}")
    status = text
    try:
        body = resp.json()
        if isinstance(body, dict):
            status = str(body.get("STATUS") or body.get("status") or text)
    except ValueError:
        pass
    ok = "sent successfully" in status.lower()
    if not ok:
        log.warning("sms rejected by provider: %s", status[:200])
    return SmsResult(ok, False, status[:255])
