"""Customer app sign-in by phone — per store, and as cheap in SMS as possible.

Three doors, one session:

1. Staff add a customer (or press "send app link"): ONE SMS with a link that
   carries a one-time token. Opening it signs them in. No second SMS for an
   OTP — the link is the proof.
2. A customer signs up on the site themselves: phone → 6-digit SMS code.
3. iPhone home-screen app (which does not share Safari's storage): Safari
   shows a 6-digit handoff code, the installed app asks for it. No SMS.

All three end in a CustomerSession: a long-lived random token the phone keeps
in localStorage (we store only its hash), so nobody is asked for a code again
on that device. Everything is scoped to ONE store: the same number at another
café is a different customer with a different session.

SMS spending is bounded three ways: per number (a minute apart, 5 a day,
across all stores), per IP, and per store per day (SMS_DAILY_CAP_PER_STORE).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from rest_framework import exceptions, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core import sms
from apps.store import models
from apps.store.club import PHONE_RE, normalise_phone

OTP_TTL = timedelta(minutes=5)
HANDOFF_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
PHONE_COOLDOWN = timedelta(seconds=60)
PHONE_DAILY_MAX = 5
IP_DAILY_MAX = 20
SEND_LINK_COOLDOWN = timedelta(minutes=2)
TOKEN_PREFIX = "cs_"
_B62 = string.ascii_letters + string.digits


# --------------------------------------------------------------------- helpers
def _hash(kind: str, store_id: int, secret: str) -> str:
    return hashlib.sha256(f"{kind}:{store_id}:{secret}".encode()).hexdigest()


def _digits(n: int = 6) -> str:
    return str(secrets.randbelow(10**n)).zfill(n)


def _link_token() -> str:
    # 10 base62 characters ≈ 59 bits: plenty for a single-use, 24-hour secret
    # behind attempt limits, and short enough to keep the SMS to one part.
    return "".join(secrets.choice(_B62) for _ in range(10))


def one_sms(*candidates: str) -> str:
    """The first wording that fits ONE SMS (70 characters in Arabic). Every
    extra 70 characters is billed as another SMS, so a long shop name drops
    the friendly words before it ever costs double."""
    for msg in candidates:
        if sms.segments(msg) == 1:
            return msg
    return candidates[-1]


def store_host(store: models.Store) -> str:
    return store.host or f"{store.slug}.{getattr(settings, 'PUBLIC_ROOT_DOMAIN', 'clinixa.cloud')}"


def app_url(store: models.Store) -> str:
    return f"https://{store_host(store)}{getattr(settings, 'CUSTOMER_APP_PATH', '/app')}"


def clean_phone(raw) -> str | None:
    phone = normalise_phone(str(raw or ""))
    return phone if PHONE_RE.match(phone) and sms.international(phone) else None


def _client_ip(request) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR", "")) or "?"


def _store_from_request(request) -> models.Store:
    slug = str(request.data.get("store") or "").strip().lower()
    store = models.Store.objects.filter(slug=slug, is_active=True).first() if slug else None
    if store is None:
        raise exceptions.NotFound("المتجر غير موجود")
    return store


class SmsFailed(exceptions.APIException):
    """The SMS provider refused the message — not our server's fault (502)."""

    status_code = 502
    default_detail = "تعذّر إرسال الرسالة — جرّب بعد قليل"


class SmsLimited(exceptions.APIException):
    status_code = 429
    default_detail = "طلبات كثيرة — جرّب بعد قليل"


def _check_limits(store: models.Store, phone: str, request=None) -> None:
    now = timezone.now()
    # A message the provider refused cost nothing and reached nobody: it must
    # not lock the customer out of trying again.
    logs = models.SmsLog.objects.unscoped().exclude(status=models.SmsLog.Status.FAILED)
    last = logs.filter(phone=phone).order_by("-created_at").values_list("created_at", flat=True).first()
    if last and now - last < PHONE_COOLDOWN:
        wait = int((PHONE_COOLDOWN - (now - last)).total_seconds()) + 1
        raise SmsLimited({"detail": f"انتظر {wait} ثانية قبل طلب رمز جديد", "retry_after": wait})
    if logs.filter(phone=phone, created_at__gte=now - timedelta(days=1)).count() >= PHONE_DAILY_MAX:
        raise SmsLimited({"detail": "وصلت للحد اليومي من الرسائل لهذا الرقم — جرّب غداً"})
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cap = int(getattr(settings, "SMS_DAILY_CAP_PER_STORE", 300))
    if logs.filter(store=store, created_at__gte=day_start).count() >= cap:
        raise SmsLimited({"detail": "الخدمة مشغولة حالياً — جرّب لاحقاً"})
    if request is not None:
        key = f"sms:ip:{_client_ip(request)}:{now:%Y%m%d}"
        n = cache.get(key, 0)
        if n >= IP_DAILY_MAX:
            raise SmsLimited({"detail": "طلبات كثيرة من هذا الجهاز — جرّب غداً"})
        cache.set(key, n + 1, 60 * 60 * 26)


def _send(store, phone, msg, kind, customer=None, user=None) -> models.SmsLog:
    sender = (store.sms_sender or getattr(settings, "HTD_SMS_SENDER", "COOP")).strip()
    to = sms.international(phone) or ""
    res = sms.send_as(
        to, msg, sender=sender, api_id=(getattr(settings, "HTD_SMS_API_ID", "") or "").strip()
    )
    status = (
        models.SmsLog.Status.DRY_RUN if res.dry_run
        else models.SmsLog.Status.SENT if res.ok
        else models.SmsLog.Status.FAILED
    )
    return models.SmsLog.objects.create(
        store=store, customer=customer, phone=phone, kind=kind, status=status,
        segments=sms.segments(msg), provider_response=res.response, created_by=user,
    )


def _new_code(store, phone, kind, secret, ttl, customer=None, user=None, retire=True):
    # One live code of a kind per number: a new one retires the old. (A QR on
    # the counter screen does not retire a link already texted to them.)
    if retire:
        models.CustomerAuthCode.objects.unscoped().filter(
            store=store, phone=phone, kind=kind, used_at__isnull=True
        ).update(used_at=timezone.now())
    return models.CustomerAuthCode.objects.create(
        store=store, customer=customer, phone=phone, kind=kind,
        code_hash=_hash(kind, store.pk, secret), expires_at=timezone.now() + ttl,
        created_by=user,
    )


def _consume(store, kind, secret, phone=None) -> models.CustomerAuthCode | None:
    """Find and burn a live code. Wrong guesses count against the newest live
    code for that number, and 5 of them kill it."""
    qs = models.CustomerAuthCode.objects.unscoped().select_for_update().filter(
        store=store, kind=kind, used_at__isnull=True, expires_at__gt=timezone.now()
    )
    if phone:
        qs = qs.filter(phone=phone)
    want = _hash(kind, store.pk, secret)
    if phone:
        row = qs.order_by("-created_at").first()
        if row is None:
            return None
        if row.attempts >= MAX_ATTEMPTS:
            return None
        if not hmac.compare_digest(row.code_hash, want):
            row.attempts += 1
            row.save(update_fields=["attempts", "updated_at"])
            return None
    else:
        row = qs.filter(code_hash=want).first()
        if row is None:
            return None
    row.used_at = timezone.now()
    row.save(update_fields=["used_at", "updated_at"])
    return row


def _customer_for(store, phone, name="") -> models.Customer:
    """The store's customer with this number — the one staff registered at the
    counter, if any — or a new one. Same rules as the counter: one per number
    per store. A brand-new loyalty profile gets the sign-up gift."""

    cust = models.Customer.objects.for_pharmacy(store.pk).filter(phone=phone).first()
    if cust is None:
        cust = models.Customer.objects.create(
            store=store, phone=phone, name=(name or "").strip()[:255] or "زبون",
        )
    ensure_profile(store, cust)
    return cust


def ensure_profile(store, cust) -> None:
    """A loyalty profile for this customer; a brand-new one gets the sign-up
    gift. Same rule whichever door they came in by (code, link, handoff)."""
    from apps.store.models import BeanLedger, LoyaltyProfile

    profile, made = LoyaltyProfile.objects.get_or_create(
        store=store, customer=cust, defaults={"beans": 0}
    )
    if made:
        bonus = int(getattr(settings, "SIGNUP_BONUS_BEANS", 5) or 0)
        if bonus:
            profile.beans = bonus
            profile.save(update_fields=["beans"])
            BeanLedger.objects.create(
                store=store, customer=cust, delta=bonus, reason="signup",
                balance_after=bonus, note="هدية التسجيل",
                idempotency_key=f"signup:phone:{store.pk}:{cust.pk}",
            )


def _open_session(request, store, customer, via) -> dict:
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    days = int(getattr(settings, "CUSTOMER_SESSION_DAYS", 180))
    expires = timezone.now() + timedelta(days=days)
    models.CustomerSession.objects.create(
        store=store, customer=customer, token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        via=via, expires_at=expires, last_seen_at=timezone.now(),
        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:200],
    )
    if customer.app_registered_at is None:
        customer.app_registered_at = timezone.now()
        customer.save(update_fields=["app_registered_at", "updated_at"])
    from apps.store.views import invalidate_customers_quick_cache

    invalidate_customers_quick_cache(store.pk)
    return {
        "token": raw,
        "expires_at": expires.isoformat(),
        "store": store.slug,
        "customer": {"id": customer.pk, "name": customer.name, "phone": customer.phone},
    }


# ---------------------------------------------------------------- staff side
def send_app_link(store, customer, user=None) -> dict:
    """Text the customer a link that signs them in — one SMS, no OTP after."""
    phone = clean_phone(customer.phone)
    if not phone:
        raise exceptions.ValidationError({"detail": "لا يوجد رقم جوال صحيح لهذا الزبون"})
    if customer.app_invited_at and timezone.now() - customer.app_invited_at < SEND_LINK_COOLDOWN:
        raise SmsLimited({"detail": "أُرسل الرابط قبل قليل — انتظر دقيقتين"})
    _check_limits(store, phone)
    token = _link_token()
    ttl = timedelta(hours=int(getattr(settings, "APP_LINK_TTL_HOURS", 24)))
    with transaction.atomic():
        _new_code(store, phone, models.CustomerAuthCode.Kind.INVITE, token, ttl,
                  customer=customer, user=user)
        url = f"{app_url(store)}/i/{token}"
        msg = one_sms(f"{store.name}: نقاطك وطلباتك هنا {url}", f"{store.name}: نقاطك {url}", f"نقاطك: {url}", url)
        log = _send(store, phone, msg, models.SmsLog.Kind.INVITE, customer=customer, user=user)
        if log.status != models.SmsLog.Status.FAILED:
            customer.app_invited_at = timezone.now()
            customer.save(update_fields=["app_invited_at", "updated_at"])
    if log.status == models.SmsLog.Status.FAILED:
        raise SmsFailed(f"تعذّر إرسال الرسالة: {log.provider_response}")
    out = {
        "sent": True,
        "dry_run": log.status == models.SmsLog.Status.DRY_RUN,
        "invited_at": customer.app_invited_at.isoformat(),
    }
    if out["dry_run"] and settings.DEBUG:
        out["debug_link"] = f"{app_url(store)}/i/{token}"  # local testing only
    return out


QR_TTL = timedelta(minutes=5)


def counter_qr(store, customer, user=None) -> dict:
    """A QR for the customer-facing screen: scan it in the shop and you are
    signed in. Same one-time link as the SMS (/app/i/<token>), but no SMS at
    all, and alive for 5 minutes only — it is on a screen anyone can see."""
    import base64
    import io

    import qrcode
    import qrcode.image.svg

    token = _link_token()
    _new_code(store, customer.phone or "", models.CustomerAuthCode.Kind.INVITE, token,
              QR_TTL, customer=customer, user=user, retire=False)
    url = f"{app_url(store)}/i/{token}"
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return {
        "url": url,
        "qr": "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode(),
        "expires_in": int(QR_TTL.total_seconds()),
    }


# ---------------------------------------------------------- authentication
class CustomerSessionAuthentication:
    """`Authorization: Bearer cs_…` → request.app_customer_id / app_store_id.

    Returns None like the Firebase and Clerk authenticators (a customer has no
    Django user); IsAppCustomer is what lets the request through. A cs_ token
    that is unknown, revoked or expired is a 401, so the app can drop it and
    show the sign-in screen instead of failing quietly.
    """

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.lower().startswith("bearer "):
            return None
        raw = header.split(" ", 1)[1].strip()
        if not raw.startswith(TOKEN_PREFIX):
            return None
        row = (
            models.CustomerSession.objects.unscoped()
            .select_related("store")
            .filter(token_hash=hashlib.sha256(raw.encode()).hexdigest())
            .first()
        )
        now = timezone.now()
        if (
            row is None or row.revoked_at is not None or row.expires_at <= now
            or not row.store.is_active
        ):
            raise exceptions.AuthenticationFailed("انتهت الجلسة — سجّل دخولك من جديد")
        if row.last_seen_at is None or now - row.last_seen_at > timedelta(hours=1):
            models.CustomerSession.objects.unscoped().filter(pk=row.pk).update(last_seen_at=now)
        request.app_customer_id = row.customer_id
        request.app_store_id = row.store_id
        request.app_session_id = row.pk
        return None

    def authenticate_header(self, request):
        return "Bearer"


# ------------------------------------------------------------------- views
class _Public(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]


class OtpRequestView(_Public):
    """POST {store, phone} → an SMS code. Same answer whether or not the number
    is already a customer, so this cannot be used to look people up."""

    def post(self, request):
        store = _store_from_request(request)
        phone = clean_phone(request.data.get("phone"))
        if not phone:
            return Response({"detail": "رقم جوال غير صحيح — مثال: 0599123456"}, status=400)
        _check_limits(store, phone, request)
        code = _digits()
        with transaction.atomic():
            _new_code(store, phone, models.CustomerAuthCode.Kind.OTP, code, OTP_TTL)
            # Last line in the WebOTP format, so Android offers to fill it in.
            msg = one_sms(
                f"{store.name}: رمز الدخول {code}\n\n@{store_host(store)} #{code}",
                f"رمز الدخول {code}\n\n@{store_host(store)} #{code}",
                f"رمز الدخول {code}",
            )
            log = _send(store, phone, msg, models.SmsLog.Kind.OTP)
        if log.status == models.SmsLog.Status.FAILED:
            return Response({"detail": "تعذّر إرسال الرسالة — جرّب بعد قليل"}, status=502)
        out = {"sent": True, "expires_in": int(OTP_TTL.total_seconds())}
        if log.status == models.SmsLog.Status.DRY_RUN and settings.DEBUG:
            out["debug_code"] = code  # local testing only, never in production
        return Response(out)


class OtpVerifyView(_Public):
    """POST {store, phone, code, name?} → a session."""

    @transaction.atomic
    def post(self, request):
        store = _store_from_request(request)
        phone = clean_phone(request.data.get("phone"))
        code = str(request.data.get("code") or "").strip()
        if not phone or not code.isdigit():
            return Response({"detail": "الرمز غير صحيح"}, status=400)
        row = _consume(store, models.CustomerAuthCode.Kind.OTP, code, phone=phone)
        if row is None:
            return Response({"detail": "الرمز غير صحيح أو انتهت صلاحيته"}, status=400)
        customer = _customer_for(store, phone, request.data.get("name") or "")
        return Response(_open_session(request, store, customer, row.kind))


class LinkRedeemView(_Public):
    """POST {store, token} — the link staff texted. Burned on first use.

    A POST from the page's script, never a GET: messaging apps fetch links to
    draw previews, and a GET that signed people in would be spent by the
    preview bot before the customer ever tapped it."""

    @transaction.atomic
    def post(self, request):
        store = _store_from_request(request)
        token = str(request.data.get("token") or "").strip()
        if not (8 <= len(token) <= 32) or not token.isalnum():
            return Response({"detail": "الرابط غير صالح"}, status=400)
        row = _consume(store, models.CustomerAuthCode.Kind.INVITE, token)
        if row is None or row.customer_id is None:
            return Response(
                {"detail": "الرابط مستخدم أو منتهي — ادخل برقم جوالك", "code": "link_expired"},
                status=410,
            )
        customer = models.Customer.objects.for_pharmacy(store.pk).filter(pk=row.customer_id).first()
        if customer is None:
            return Response({"detail": "الحساب غير موجود"}, status=404)
        ensure_profile(store, customer)
        return Response(_open_session(request, store, customer, row.kind))


class HasCustomerSession(permissions.BasePermission):
    message = "سجّل دخولك للمتابعة"

    def has_permission(self, request, view) -> bool:
        return bool(getattr(request, "app_session_id", None))


class _Customer(APIView):
    authentication_classes = [CustomerSessionAuthentication]
    permission_classes = [HasCustomerSession]

    def _session(self, request):
        sid = getattr(request, "app_session_id", None)
        if not sid:
            raise exceptions.NotAuthenticated("سجّل دخولك للمتابعة")
        return models.CustomerSession.objects.unscoped().select_related("store", "customer").get(pk=sid)


class HandoffCreateView(_Customer):
    """POST (signed in, in Safari) → a 6-digit code for the home-screen app."""

    def post(self, request):
        s = self._session(request)
        code = _digits()
        _new_code(s.store, s.customer.phone or "", models.CustomerAuthCode.Kind.HANDOFF,
                  code, HANDOFF_TTL, customer=s.customer)
        return Response({"code": code, "expires_in": int(HANDOFF_TTL.total_seconds())})


class HandoffRedeemView(_Public):
    """POST {store, phone, code} from the home-screen app → a session. No SMS."""

    @transaction.atomic
    def post(self, request):
        store = _store_from_request(request)
        phone = clean_phone(request.data.get("phone"))
        code = str(request.data.get("code") or "").strip()
        if not phone or not code.isdigit():
            return Response({"detail": "الرمز غير صحيح"}, status=400)
        row = _consume(store, models.CustomerAuthCode.Kind.HANDOFF, code, phone=phone)
        if row is None or row.customer_id is None:
            return Response({"detail": "الرمز غير صحيح أو انتهت صلاحيته"}, status=400)
        customer = models.Customer.objects.for_pharmacy(store.pk).filter(pk=row.customer_id).first()
        if customer is None:
            return Response({"detail": "الحساب غير موجود"}, status=404)
        return Response(_open_session(request, store, customer, row.kind))


class LogoutView(_Customer):
    def post(self, request):
        sid = getattr(request, "app_session_id", None)
        if sid:
            models.CustomerSession.objects.unscoped().filter(pk=sid).update(revoked_at=timezone.now())
        return Response({"ok": True})
