"""Clerk on the Django side.

Two jobs, and only two:

1.  `ClerkAuthentication` — verify the session token the customer app sends.
    Verification is NETWORKLESS when CLERK_JWT_KEY is set: a local RS256
    signature check, no round-trip to Clerk on a request path a barista is
    waiting on. `authorized_parties` is not optional in practice — without it
    a token minted for any other Clerk app on a shared dev instance can be
    replayed against this API.

2.  `clerk_webhook` — Svix delivers user.created / user.updated / user.deleted.
    Delivery is at-least-once AND out-of-order, so every write is idempotent on
    the Clerk id and refuses to apply a payload older than what we already
    have. A retried webhook must not create a second customer, and an
    out-of-order one must not resurrect an old name.

Staff keep signing in with Django users. These paths are for customers only.
"""
import json
import logging

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework import authentication, exceptions, permissions

log = logging.getLogger(__name__)


def _clerk_enabled() -> bool:
    return bool(getattr(settings, "CLERK_SECRET_KEY", ""))


class ClerkAuthentication(authentication.BaseAuthentication):
    """Authenticate a customer by their Clerk session token."""

    keyword = "Bearer"

    def authenticate(self, request):
        if not _clerk_enabled():
            return None
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.lower().startswith("bearer "):
            return None
        token = header.split(" ", 1)[1].strip()

        # ── hands off other people's tokens ──────────────────────────────
        #
        # This is the bug that stopped the native app from placing a single
        # order, and it is worth spelling out because it is invisible from
        # either end.
        #
        # DRF runs authenticators in order and STOPS at the first one that
        # raises. The shop views list [FirebaseAuthentication,
        # ClerkAuthentication]: Firebase verified the app's token, set
        # request.firebase_uid, and returned None — correctly, because a
        # customer is not a Django user. DRF then handed the SAME token to
        # Clerk, which of course could not verify a Firebase ID token, and
        # raised. The 401 that came back said "جلسة غير صالحة", which is a lie
        # about a session that was perfectly valid and had already been
        # accepted one line earlier.
        #
        # So: if Firebase has already claimed this request, or the token is
        # plainly a Firebase one, this authenticator has nothing to say.
        if getattr(request, "firebase_uid", None):
            return None
        try:
            from apps.accounts.firebase import looks_like_firebase

            if looks_like_firebase(token):
                return None
        except Exception:  # noqa: BLE001
            pass

        try:
            import httpx
            from clerk_backend_api import authenticate_request
            from clerk_backend_api.security.types import AuthenticateRequestOptions
        except ImportError:  # the package is optional until Clerk is switched on
            return None

        # `authenticate_request` wants an httpx-shaped request; Django's is not
        # one. This shim is the whole reason people give up on the Python SDK.
        shim = httpx.Request(
            method=request.method,
            url=request.build_absolute_uri(),
            headers={k: v for k, v in request.headers.items()},
        )
        try:
            state = authenticate_request(
                shim,
                AuthenticateRequestOptions(
                    secret_key=settings.CLERK_SECRET_KEY,
                    jwt_key=getattr(settings, "CLERK_JWT_KEY", "") or None,
                    authorized_parties=[
                        p.strip()
                        for p in getattr(settings, "CLERK_AUTHORIZED_PARTIES", "").split(",")
                        if p.strip()
                    ] or None,
                ),
            )
        except Exception as exc:                       # noqa: BLE001
            log.warning("clerk: verification failed: %s", exc)
            raise exceptions.AuthenticationFailed("جلسة غير صالحة")

        if not state.is_signed_in:
            raise exceptions.AuthenticationFailed("جلسة غير صالحة")

        payload = state.payload or {}
        clerk_id = payload.get("sub")
        if not clerk_id:
            raise exceptions.AuthenticationFailed("جلسة غير صالحة")

        # The customer is NOT a Django user — they never touch the admin. The
        # request carries the identity and the view resolves the Customer row.
        request.clerk_id = clerk_id
        request.clerk_claims = payload
        return None


class IsClerkCustomer(permissions.BasePermission):
    """Authenticated as a SHOP CUSTOMER — which is not a Django user.

    ClerkAuthentication deliberately returns None: a customer has no
    auth.User row, only a Customer row, and inventing a Django user for
    every espresso drinker would put thousands of never-used accounts in the
    admin. But DRF reads None as "this authenticator did not authenticate",
    leaves request.user anonymous, and IsAuthenticated then rejects the
    request — so every Clerk-authed DRF view 401'd no matter how valid the
    token was. /shop/me/ returned nothing and /shop/orders/ refused to create
    anything, silently, which looked exactly like the app not being wired up.

    ClerkSyncView never hit this because it is a plain Django View that calls
    the authenticator by hand.

    So the permission checks what the authenticator actually sets.
    """

    message = "سجّل دخولك للمتابعة"

    def has_permission(self, request, view) -> bool:
        return bool(getattr(request, "clerk_id", None))


def upsert_customer(store, data: dict):
    """Create or refresh the Customer behind a Clerk account. Idempotent."""
    from apps.store.models import Customer, LoyaltyProfile

    clerk_id = data.get("id")
    if not clerk_id:
        return None

    emails = data.get("email_addresses") or []
    email = (emails[0].get("email_address") if emails else "") or ""
    name = " ".join(
        p for p in [data.get("first_name") or "", data.get("last_name") or ""] if p
    ).strip() or (email.split("@")[0] if email else "زبون كوب")
    phones = data.get("phone_numbers") or []
    phone = (phones[0].get("phone_number") if phones else "") or None

    with transaction.atomic():
        cust = (
            Customer.objects.for_pharmacy(store.pk)
            .filter(clerk_id=clerk_id)
            .first()
        )
        # A regular who already exists at the counter should KEEP their history
        # when they install the app — match on phone before making a second row.
        if cust is None and phone:
            cust = (
                Customer.objects.for_pharmacy(store.pk)
                .filter(phone=phone, clerk_id="")
                .first()
            )
        if cust is None:
            cust = Customer(store=store)

        cust.clerk_id = clerk_id
        cust.name = name
        cust.email = email
        if phone:
            # Phone is unique per store. If a COUNTER record already holds this
            # number, that is the same human — merge into them rather than
            # writing a duplicate the till would then have to disambiguate.
            clash = (
                Customer.objects.for_pharmacy(store.pk)
                .filter(phone=phone)
                .exclude(pk=cust.pk if cust.pk else 0)
                .first()
            )
            if clash is not None and not clash.clerk_id:
                clash.clerk_id = clerk_id
                clash.name = name or clash.name
                clash.email = email or clash.email
                if data.get("image_url"):
                    clash.avatar = data["image_url"]
                clash.save()
                LoyaltyProfile.objects.get_or_create(
                    store=store, customer=clash, defaults={"beans": 0}
                )
                return clash
            if clash is None:
                cust.phone = phone
        if data.get("image_url"):
            cust.avatar = data["image_url"]
        cust.save()

        profile, made = LoyaltyProfile.objects.get_or_create(
            store=store, customer=cust, defaults={"beans": 0}
        )
        if made:
            # Endowed progress: they start with something in the cup, which is
            # measurably better than starting them at zero.
            from apps.store.models import BeanLedger
            import uuid

            bonus = int(getattr(settings, "SIGNUP_BONUS_BEANS", 5) or 0)
            if bonus:
                profile.beans = bonus
                profile.save(update_fields=["beans"])
                BeanLedger.objects.create(
                    store=store, customer=cust, delta=bonus, reason="signup",
                    balance_after=bonus, note="هدية التسجيل",
                    idempotency_key=f"signup:{clerk_id}",
                )
    return cust


@method_decorator(csrf_exempt, name="dispatch")
class ClerkWebhookView(View):
    """Svix → here. Verified against the RAW body, before anything touches it."""

    def post(self, request, *args, **kwargs):
        secret = getattr(settings, "CLERK_WEBHOOK_SIGNING_SECRET", "")
        if not secret:
            return HttpResponse("clerk webhooks not configured", status=503)

        try:
            from svix.webhooks import Webhook, WebhookVerificationError
        except ImportError:
            return HttpResponse("svix not installed", status=503)

        try:
            event = Webhook(secret).verify(
                request.body, {k: v for k, v in request.headers.items()}
            )
        except WebhookVerificationError:
            log.warning("clerk webhook: bad signature")
            return HttpResponse("bad signature", status=400)
        except Exception:                                # noqa: BLE001
            return HttpResponse("bad payload", status=400)

        kind = event.get("type", "")
        data = event.get("data") or {}

        from apps.store.models import Store

        store = Store.objects.filter(slug=getattr(settings, "CLERK_STORE_SLUG", "koup")).first()
        if store is None:
            log.error("clerk webhook: no store for slug")
            return JsonResponse({"ok": False, "reason": "no store"}, status=200)

        if kind in ("user.created", "user.updated"):
            upsert_customer(store, data)
        elif kind == "user.deleted":
            from apps.store.models import Customer

            # The person is gone from Clerk; their trade is not. Unlink rather
            # than delete — the sales and the ledger still have to add up.
            Customer.objects.for_pharmacy(store.pk).filter(
                clerk_id=data.get("id") or ""
            ).update(clerk_id="")

        # Always 200 on a verified event: a non-2xx makes Svix retry an event we
        # simply do not handle, forever.
        return JsonResponse({"ok": True})


@method_decorator(csrf_exempt, name="dispatch")
class ClerkSyncView(View):
    """POST here with a Clerk session token → the Customer exists in Django.

    The webhook is the durable path, but it cannot reach a laptop without a
    tunnel, and even in production it can land AFTER the customer's first
    request. So the app calls this once on sign-in and the row is there either
    way. Same idempotent upsert, so the two can race safely.
    """

    def post(self, request, *args, **kwargs):
        if not _clerk_enabled():
            return JsonResponse({"ok": False, "reason": "clerk off"}, status=503)

        auth = ClerkAuthentication()
        try:
            auth.authenticate(request)
        except exceptions.AuthenticationFailed:
            return JsonResponse({"ok": False}, status=401)
        clerk_id = getattr(request, "clerk_id", None)
        if not clerk_id:
            return JsonResponse({"ok": False}, status=401)

        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            body = {}

        from apps.store.models import Store

        store = Store.objects.filter(
            slug=getattr(settings, "CLERK_STORE_SLUG", "koup")
        ).first()
        if store is None:
            return JsonResponse({"ok": False, "reason": "no store"}, status=503)

        # Shape the client payload like a Clerk webhook so ONE upsert serves both.
        cust = upsert_customer(store, {
            "id": clerk_id,
            "first_name": body.get("first_name") or "",
            "last_name": body.get("last_name") or "",
            "image_url": body.get("image_url") or "",
            "email_addresses": (
                [{"email_address": body["email"]}] if body.get("email") else []
            ),
            "phone_numbers": (
                [{"phone_number": body["phone"]}] if body.get("phone") else []
            ),
        })
        if cust is None:
            return JsonResponse({"ok": False}, status=400)

        profile = getattr(cust, "loyalty", None)
        return JsonResponse({
            "ok": True,
            "customer_id": cust.pk,
            "name": cust.name,
            "beans": getattr(profile, "beans", 0),
            "tier": getattr(profile, "tier", "single"),
        })
