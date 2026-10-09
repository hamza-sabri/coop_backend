"""Keep the till's customer list (customers/quick/, cached) in step with every
way a customer can change — including the ones that never pass through the
staff API: signing up in the app, a points balance moving, the showcase or
go_live commands. Invalidated after the transaction commits, so a reader can
never re-cache the old list in between."""
from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.store import models


def _drop(store_id):
    if not store_id:
        return

    def go():
        from apps.store.views import invalidate_customers_quick_cache

        invalidate_customers_quick_cache(store_id)

    transaction.on_commit(go)


@receiver(post_save, sender=models.Customer)
@receiver(post_delete, sender=models.Customer)
def _customer_changed(sender, instance, **kwargs):
    _drop(instance.store_id)


@receiver(post_save, sender=models.LoyaltyProfile)
@receiver(post_delete, sender=models.LoyaltyProfile)
def _points_changed(sender, instance, **kwargs):
    _drop(instance.store_id)
