"""Tenant-aware authentication.

Usernames are unique PER PHARMACY, not per system, so plain ModelBackend
(get_by_natural_key → exactly one row) can crash or pick the wrong account
once two stores both have a "sara". This backend scopes the lookup:

- Tenant sites send their store slug with the login request → the lookup
  is (store, username), which is unique.
- Without a slug (central site, Django admin), all accounts with that
  username are considered and the password decides. If the password matches
  MORE than one account the login is refused — ambiguous logins must go
  through the store's own domain.

Inherits ModelBackend so permissions, get_user, and is_active handling stay
stock Django.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

User = get_user_model()


class PharmacyScopedBackend(ModelBackend):
    def authenticate(  # noqa: D102
        self, request, username=None, password=None, store_slug=None, **kwargs
    ):
        if username is None:
            username = kwargs.get(User.USERNAME_FIELD)
        if username is None or password is None:
            return None

        candidates = User.objects.filter(username=username)
        if store_slug:
            candidates = candidates.filter(store__slug=store_slug)
        # Bounded: (store, username) is unique, so this stays tiny even
        # with a popular username across many tenants.
        candidates = list(candidates.select_related("store")[:20])

        if not candidates:
            # Same timing as a real check — don't leak which usernames exist.
            User().set_password(password)
            return None

        matches = [
            u
            for u in candidates
            if u.check_password(password) and self.user_can_authenticate(u)
        ]
        return matches[0] if len(matches) == 1 else None
