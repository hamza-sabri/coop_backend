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
    res = send_as(to, msg, sender=sender, api_id=api_id)
    # Palestinian numbers are reachable as 970… and, on some routes, 972….
    # If the provider refuses one form, try the other once (a refused
    # message is not billed).
    # (Not for account-level refusals — wrong IP, bad ID, no credit — where
    # the other number would be refused just the same.)
    account_level = res.response[:4] in ("H004", "H001", "H002", "H003") or "not allowed" in res.response.lower()
    if not res.ok and not res.dry_run and not res.response.startswith("error:") and not account_level:
        alt = ("972" if to.startswith("970") else "970") + to[3:]
        log.info("sms retry with other country code (%s…)", alt[:6])
        res2 = send_as(alt, msg, sender=sender, api_id=api_id)
        if res2.ok:
            return res2
    return res


def send_as(to: str, msg: str, *, sender: str, api_id: str) -> SmsResult:
    if getattr(settings, "SMS_DRY_RUN", True) or not api_id:
        log.info("sms dry-run to=%s… len=%s", to[:6], len(msg))
        return SmsResult(True, True, "dry_run")
    import requests

    # The plain HTTP API (manual §1) — the same call HTD's own panel shows:
    # SendSMS.aspx?id=…&sender=…&to=…&msg=…  (requests URL-encodes msg).
    try:
        resp = requests.get(
            getattr(settings, "HTD_SMS_URL", "https://sms.htd.ps/API/SendSMS.aspx"),
            params={"id": api_id, "sender": sender, "to": to, "msg": msg, "mode": "0"},
            timeout=15,
        )
        text = (resp.text or "").strip()
    except Exception as exc:  # network down, DNS, timeout
        log.warning("sms send failed: %s", exc)
        return SmsResult(False, False, f"error: {type(exc).__name__}")
    ok = "sent successfully" in text.lower() or text.upper().startswith("OK")
    if not ok:
        log.warning("sms rejected by provider: %s (to=%s… sender=%s len=%s)", text[:200], to[:6], sender, len(msg))
    return SmsResult(ok, False, text[:255])
