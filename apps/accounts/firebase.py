"""Firebase on the Django side — customer identity for the native app.

This replaces Clerk for the mobile app while leaving every Clerk path intact,
because the two populations overlap: a customer who signed up through the web
PWA has a `clerk_id`, and the same human signing into the Flutter app arrives
with a `firebase_uid`. Creating a second row for them would split their beans
across two accounts, which is the one failure a loyalty scheme cannot survive.

Three jobs:

1.  `FirebaseAuthentication` — verify the ID token the app sends. Verification
    is a local RS256 signature check against Google's published JWKS; the keys
    are cached, so the hot path makes no network call. Firebase ID tokens are
    NOT opaque session tokens: everything needed to trust them is in the token
    plus a public key, which is why the backend needs no service-account
    credentials to sign people in. The service account is only ever needed to
    SEND push.

2.  `upsert_customer_from_firebase` — create or find the Customer, linking to an
    existing row where one plainly belongs to the same person. Order matters and
    is deliberate: firebase_uid, then VERIFIED email, then phone.

3.  `FirebaseSyncView` — called once by the app on sign-in so the row exists
    before the first /shop/me/, exactly as ClerkSyncView did.

Staff still sign in with Django users. None of this touches the admin.
"""
import json
import logging
import time

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework import authentication, exceptions, permissions

log = logging.getLogger(__name__)

#: Google publishes the securetoken signing keys in JWKS form here. (The older
#: x509 endpoint carries the same keys in a format PyJWT cannot read directly.)
JWKS_URL = (
    "https://www.googleapis.com/service_accounts/v1/jwk/"
    "securetoken@system.gserviceaccount.com"
)

_jwks_client = None


def project_id() -> str:
    return (getattr(settings, "FIREBASE_PROJECT_ID", "") or "").strip()


def firebase_enabled() -> bool:
    return bool(project_id())


def _client():
    """One cached JWKS client for the process.

    PyJWKClient caches the key set and refetches only when it sees a `kid` it
    does not know — which is what makes per-request verification free. A new
    client per request would fetch Google's keys on every single API call.
    """
    global _jwks_client
    if _jwks_client is None:
        from jwt import PyJWKClient

        _jwks_client = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwks_client


def looks_like_firebase(token: str) -> bool:
    """Is this a Firebase ID token? — read, do not verify.

    A Clerk session token arrives in the same `Bearer` header, so the
    authenticator has to know whose token it is holding before it can decide
    whether a failure is "not mine, pass it on" or "yours, and broken". Reading
    the unverified `iss` claim is safe precisely because nothing is trusted
    from it: the answer only routes the token to the right verifier, which then
    checks the signature properly.
    """
    try:
        import base64

        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body))
        return str(claims.get("iss", "")).startswith(
            "https://securetoken.google.com/"
        )
    except Exception:  # noqa: BLE001
        return False


def verify_id_token(token: str) -> dict:
    """Verify a Firebase ID token and return its claims. Raises on anything off.

    The checks below are the full set Firebase documents. Skipping `aud` or
    `iss` in particular would let a token minted by ANY other Firebase project
    authenticate against this API, which is the classic way this goes wrong.
    """
    import jwt

    pid = project_id()
    if not pid:
        # Not the customer's problem, and not something signing in again can
        # fix: this deployment has no FIREBASE_PROJECT_ID, so no token from the
        # app can ever be verified. Said plainly, because the generic "session
        # invalid" sent whoever saw it round the sign-in loop forever.
        log.error(
            "firebase: FIREBASE_PROJECT_ID is not set — every app request "
            "will be rejected. Set it in the deployment environment."
        )
        raise exceptions.AuthenticationFailed(
            "تسجيل الدخول من التطبيق غير مفعّل على الخادم"
        )

    try:
        signing_key = _client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=pid,
            issuer=f"https://securetoken.google.com/{pid}",
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            leeway=30,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("firebase: token verification failed: %s", exc)
        raise exceptions.AuthenticationFailed(
            "انتهت الجلسة. سجّل دخولك من جديد"
        )

    if not claims.get("sub"):
        raise exceptions.AuthenticationFailed("انتهت الجلسة. سجّل دخولك من جديد")
    # A token cannot have been issued for a sign-in that has not happened yet.
    auth_time = claims.get("auth_time")
    if auth_time and int(auth_time) > time.time() + 60:
        raise exceptions.AuthenticationFailed("انتهت الجلسة. سجّل دخولك من جديد")
    return claims


class FirebaseAuthentication(authentication.BaseAuthentication):
    """Authenticate a customer by their Firebase ID token.

    Returns None on purpose, like ClerkAuthentication before it: a customer has
    no auth.User row. The identity lands on the request and `IsAppCustomer`
    is what actually gates the view. See that class for why.
    """

    keyword = "Bearer"

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.lower().startswith("bearer "):
            return None
        token = header.split(" ", 1)[1].strip()
        if not token:
            return None

        # A Clerk session token arrives in the same header. Decide whose token
        # this is FIRST, then fail properly.
        #
        # The previous version returned None whenever Clerk was configured, so
        # every Firebase problem — an expired token, a wrong project id, a
        # missing FIREBASE_PROJECT_ID — came out of the far end of the stack as
        # the permission class's flat "جلسة غير صالحة", with no clue which of
        # those it was and nothing in the log. A real error is worth more than
        # a tidy fallback.
        if not looks_like_firebase(token):
            return None
        claims = verify_id_token(token)

        request.firebase_uid = claims["sub"]
        request.firebase_claims = claims
        return None


class IsAppCustomer(permissions.BasePermission):
    """Authenticated as a SHOP CUSTOMER — Firebase or Clerk, either is fine.

    The authenticators deliberately return None (a customer is not a Django
    user), so DRF leaves request.user anonymous and IsAuthenticated would
    reject every one of these calls no matter how valid the token. This checks
    what the authenticators actually set.
    """

    message = "سجّل دخولك للمتابعة"

    def has_permission(self, request, view) -> bool:
        return bool(
            getattr(request, "firebase_uid", None)
            or getattr(request, "clerk_id", None)
        )


def identity_filter(request):
    """ORM kwargs selecting the caller's own Customer row, or None.

    Firebase wins when both are present, which happens for exactly one request:
    the first call after a Clerk user migrates to the app.
    """
    uid = getattr(request, "firebase_uid", None)
    if uid:
        return {"firebase_uid": uid}
    cid = getattr(request, "clerk_id", None)
    if cid:
        return {"clerk_id": cid}
    return None


def upsert_customer_from_firebase(store, claims: dict, extra: dict | None = None):
    """Create or find the Customer behind a Firebase account. Idempotent.

    Linking order — each step is narrower than it looks:

    1.  `firebase_uid` — they have been here before. Nothing to decide.
    2.  VERIFIED email — they signed up through the web app with the same Google
        account, so the Clerk row is theirs. Only `email_verified` counts: an
        unverified address is attacker-controlled and would hand over someone
        else's balance.
    3.  phone — the regular the staff already knew, who never used the web app.

    Otherwise a new customer, which is the correct answer for a genuinely new
    person and the only safe answer for an ambiguous one.
    """
    from apps.store.models import BeanLedger, Customer, LoyaltyProfile

    extra = extra or {}
    uid = claims.get("sub")
    if not uid:
        return None

    email = (claims.get("email") or "").strip().lower()
    email_verified = bool(claims.get("email_verified"))
    name = (
        (claims.get("name") or "").strip()
        or (extra.get("name") or "").strip()
        or (email.split("@")[0] if email else "")
        or "زبون كوب"
    )
    avatar = claims.get("picture") or ""
    phone = (claims.get("phone_number") or extra.get("phone") or "").strip() or None

    with transaction.atomic():
        scoped = Customer.objects.for_pharmacy(store.pk)

        cust = scoped.filter(firebase_uid=uid).first()

        if cust is None and email and email_verified:
            cust = (
                scoped.filter(email__iexact=email, firebase_uid="")
                .order_by("id")
                .first()
            )
        if cust is None and phone:
            cust = scoped.filter(phone=phone, firebase_uid="").first()
        if cust is None:
            cust = Customer(store=store)

        cust.firebase_uid = uid
        # Never blank out a name the staff curated with a placeholder.
        if name and name != "زبون كوب":
            cust.name = name
        elif not cust.name:
            cust.name = name
        if email:
            cust.email = email
        if avatar:
            cust.avatar = avatar
        if phone and not cust.phone:
            # Phone is unique per store; do not collide with a counter record.
            clash = scoped.filter(phone=phone).exclude(pk=cust.pk or 0).first()
            if clash is None:
                cust.phone = phone
        cust.save()

        profile, made = LoyaltyProfile.objects.get_or_create(
            store=store, customer=cust, defaults={"beans": 0}
        )
        if made:
            bonus = int(getattr(settings, "SIGNUP_BONUS_BEANS", 5) or 0)
            if bonus:
                profile.beans = bonus
                profile.save(update_fields=["beans"])
                BeanLedger.objects.create(
                    store=store,
                    customer=cust,
                    delta=bonus,
                    reason="signup",
                    balance_after=bonus,
                    note="هدية التسجيل",
                    idempotency_key=f"signup:fb:{uid}",
                )
    return cust


@method_decorator(csrf_exempt, name="dispatch")
class FirebaseSyncView(View):
    """POST with a Firebase ID token → the Customer exists in Django.

    The app calls this once per sign-in. It is a plain Django View, not DRF,
    for the same reason ClerkSyncView is: it authenticates by hand and has to
    work before any customer row exists.
    """

    def get(self, request, *args, **kwargs):
        """Is app sign-in wired up on this deployment? — one curl, no token.

        Exists because the failure it diagnoses is invisible from the outside:
        an unset FIREBASE_PROJECT_ID rejects every request from the app in a
        way that looks, from the phone, exactly like a session that expired.
        The project id is not a secret — it ships inside the app's own
        google-services.json — so answering it here costs nothing and saves an
        afternoon.
        """
        return JsonResponse({
            "enabled": firebase_enabled(),
            "project_id": project_id(),
            "store_slug": getattr(settings, "CLERK_STORE_SLUG", ""),
        })

    def post(self, request, *args, **kwargs):
        if not firebase_enabled():
            log.error(
                "firebase: /firebase/sync/ called but FIREBASE_PROJECT_ID is "
                "not set — the app cannot sign anyone in against this server."
            )
            return JsonResponse(
                {
                    "ok": False,
                    "detail": "تسجيل الدخول من التطبيق غير مفعّل على الخادم",
                    "reason": "FIREBASE_PROJECT_ID is not set",
                },
                status=503,
            )

        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.lower().startswith("bearer "):
            return JsonResponse(
                {"ok": False, "detail": "سجّل دخولك للمتابعة"}, status=401
            )
        try:
            claims = verify_id_token(header.split(" ", 1)[1].strip())
        except exceptions.AuthenticationFailed as exc:
            return JsonResponse(
                {"ok": False, "detail": str(exc.detail)}, status=401
            )

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

        cust = upsert_customer_from_firebase(store, claims, extra=body)
        if cust is None:
            return JsonResponse({"ok": False}, status=400)

        profile = getattr(cust, "loyalty", None)
        return JsonResponse({
            "ok": True,
            "customer_id": cust.pk,
            "name": cust.name,
            "phone": cust.phone or "",
            "beans": getattr(profile, "beans", 0),
            "tier": getattr(profile, "tier", "single"),
        })
