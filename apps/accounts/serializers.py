from django.contrib.auth import authenticate, get_user_model
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.settings import api_settings as jwt_settings

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    pharmacy_name = serializers.CharField(source="store.name", read_only=True, default="")
    store_slug = serializers.CharField(source="store.slug", read_only=True, default="")
    # Tenant header details used to prefill the POS receipt/label print settings.
    pharmacy_phone = serializers.CharField(source="store.phone", read_only=True, default="")
    pharmacy_address = serializers.CharField(source="store.address", read_only=True, default="")
    # Store-wide near-expiry alert window (days) — the default the product
    # form/list falls back to when a product has no per-row override.
    pharmacy_expiry_alert_days = serializers.IntegerField(
        source="store.expiry_alert_days", read_only=True, default=30
    )
    pharmacy_logo = serializers.SerializerMethodField()
    # The feature modules THIS account may use (store tier ∩ per-user
    # grants). The frontend builds its nav/routes from this list.
    modules = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "store",
            "pharmacy_name",
            "store_slug",
            "pharmacy_phone",
            "pharmacy_address",
            "pharmacy_expiry_alert_days",
            "pharmacy_logo",
            "modules",
            "role",
            "username",
            "email",
            "first_name",
            "last_name",
            "phone",
            "display_name",
            "avatar",
            "profile_image_url",
            "is_staff",
            "date_joined",
        ]
        read_only_fields = [
            "id",
            "store",
            "pharmacy_name",
            "store_slug",
            "pharmacy_phone",
            "pharmacy_address",
            "pharmacy_expiry_alert_days",
            "pharmacy_logo",
            "modules",
            "role",
            "is_staff",
            "date_joined",
        ]

    def get_pharmacy_logo(self, obj):
        # `logo` may hold a `b2://<key>` marker — sign it on read like every
        # other stored image so the frontend gets a servable URL.
        from apps.core.uploads import resolve_stored_url

        store = getattr(obj, "store", None)
        return resolve_stored_url(store.logo) if store else ""

    def get_modules(self, obj):
        from apps.store.modules import effective_modules

        return sorted(effective_modules(obj))


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Login response includes the serialized user alongside the tokens.

    Accepts an optional `store` (slug) field: usernames are unique per
    store, so tenant sites send their own slug to pin the lookup. The
    slug reaches PharmacyScopedBackend via authenticate(store_slug=...);
    kwargs-unaware backends are skipped by Django automatically.
    """

    store = serializers.CharField(
        required=False, allow_blank=True, write_only=True
    )

    def validate(self, attrs):
        from rest_framework import exceptions

        request = self.context.get("request")
        slug = (attrs.pop("store", "") or "").strip()

        self.user = authenticate(
            request=request,
            username=attrs[self.username_field],
            password=attrs["password"],
            store_slug=slug or None,
        )
        if not jwt_settings.USER_AUTHENTICATION_RULE(self.user):
            raise exceptions.AuthenticationFailed(
                self.error_messages["no_active_account"],
                code="no_active_account",
            )

        # Billing suspension: block login for a deactivated tenant so the
        # cashier sees a clear message instead of an empty, 403-ing app. The
        # request-time guard in request_pharmacy_id still covers already-issued
        # tokens.
        store = getattr(self.user, "store", None)
        if store is not None and not store.is_active:
            raise exceptions.AuthenticationFailed(
                "اشتراك الصيدلية موقوف. يرجى التواصل مع الدعم لتفعيله.",
                code="pharmacy_suspended",
            )

        refresh = self.get_token(self.user)
        data = {"refresh": str(refresh), "access": str(refresh.access_token)}
        if jwt_settings.UPDATE_LAST_LOGIN:
            from django.contrib.auth.models import update_last_login

            update_last_login(None, self.user)
        data["user"] = UserSerializer(self.user).data
        return data


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class StaffSerializer(serializers.ModelSerializer):
    """Owner-managed staff record for ONE store.

    Writable role + per-user module grants (`allowed_modules`) + active flag.
    `password` is write-only: required on create, optional on edit (or set via
    the viewset's reset-password action). Tenant scoping and owner-only access
    are enforced by the viewset; this serializer validates the payload and the
    two safety rules — no privilege escalation beyond the store's own tier,
    and never lock the store out of its last owner / deactivate yourself.
    """

    password = serializers.CharField(
        write_only=True,
        required=False,
        min_length=4,
        allow_blank=False,
        style={"input_type": "password"},
    )
    is_owner = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "first_name",
            "last_name",
            "display_name",
            "phone",
            "role",
            "allowed_modules",
            "is_active",
            "is_owner",
            "password",
            "date_joined",
            "last_login",
        ]
        read_only_fields = ["id", "is_owner", "date_joined", "last_login"]

    def get_is_owner(self, obj):
        return bool(getattr(obj, "is_owner", False))

    def _pharmacy_id(self):
        return self.context["request"].user.store_id

    def validate_allowed_modules(self, value):
        # An owner can only grant modules the PHARMACY itself has — defence in
        # depth even though effective_modules() intersects again at read time.
        from apps.store.models import Store
        from apps.store.modules import normalize, pharmacy_modules

        granted = set(normalize(value or []))
        store = Store.objects.filter(pk=self._pharmacy_id()).first()
        tier = set(pharmacy_modules(store)) if store else set()
        extra = granted - tier
        if extra:
            raise serializers.ValidationError(
                "وحدات غير متاحة ضمن باقة الصيدلية: " + "، ".join(sorted(extra))
            )
        return sorted(granted)

    def validate(self, attrs):
        pid = self._pharmacy_id()
        request = self.context["request"]

        # Username is unique WITHIN the store (scoped model constraint) —
        # surface a clean field error instead of a raw IntegrityError.
        username = attrs.get("username") or getattr(self.instance, "username", None)
        if username:
            clash = User.objects.filter(store_id=pid, username=username)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"username": "اسم المستخدم مستخدم بالفعل في هذه الصيدلية."}
                )

        # A brand-new account needs a password; edits may omit it (unchanged).
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError({"password": "كلمة المرور مطلوبة."})

        # Never lock the store out of every owner, and never let an owner
        # deactivate their own account (they would 403 on the next request).
        if self.instance is not None:
            target = self.instance
            new_role = attrs.get("role", target.role)
            new_active = attrs.get("is_active", target.is_active)
            losing_owner = target.role == User.Role.OWNER and (
                new_role != User.Role.OWNER or not new_active
            )
            if losing_owner:
                others = (
                    User.objects.filter(
                        store_id=pid, role=User.Role.OWNER, is_active=True
                    )
                    .exclude(pk=target.pk)
                    .exists()
                )
                if not others:
                    raise serializers.ValidationError(
                        "لا يمكن إزالة صلاحية آخر مالك للصيدلية."
                    )
            if target.pk == request.user.pk and not new_active:
                raise serializers.ValidationError("لا يمكنك تعطيل حسابك الخاص.")
        return attrs

    def create(self, validated_data):
        # store is stamped server-side from the requester — never the client.
        password = validated_data.pop("password", None)
        validated_data["store_id"] = self._pharmacy_id()
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        validated_data.pop("store", None)
        validated_data.pop("store_id", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance
