"""Self-heal: rebuild the Debt for any credit sale that lost it.

A `payment_method="debt"` sale MUST have a linked Debt — that is what puts the
amount on the customer's balance. The checkout writes both inside one
transaction, so this should never find anything. It exists because the failure
mode is invisible: the sale looks fine on the sales page while the customer's
statement silently under-reports what they owe, and nobody notices until the
store counts cash at the end of the month.

Run it on a schedule. It is idempotent and only ever ADDS the missing debt —
it never edits or deletes an existing one, never touches stock, and never
touches a sale that already has its debt.

    python manage.py repair_sale_debts --dry-run   # report only
    python manage.py repair_sale_debts             # repair + report
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.store import models


def orphan_credit_sales():
    """Credit sales with a customer but no linked debt (returns excluded —
    a return is always cash, so it never mirrors into a debt)."""
    return (
        models.Sale.objects.unscoped()
        .filter(
            payment_method="debt",
            debt__isnull=True,
            customer__isnull=False,
            is_return=False,
        )
        .order_by("pk")
    )


def repair_sale(sale):
    """Create the missing Debt for one sale, mirroring the checkout exactly."""
    debt = models.Debt.objects.create(
        store_id=sale.store_id,
        customer_id=sale.customer_id,
        created_by_id=sale.created_by_id,
        note=f"بيع رقم {sale.pk}",
    )
    for item in sale.items.all():
        models.DebtItem.objects.create(
            debt=debt,
            product=item.product,
            variant=item.variant,
            medication_name=item.medication_name,
            variant_label=item.variant_label,
            unit_price=item.unit_price,
            quantity=item.quantity,
        )
    debt.recalculate_total(save=False)
    debt.discounted_total = sale.discounted_total
    debt.save()
    # .update() so the sale's updated_at is not rewritten by a repair.
    models.Sale.objects.unscoped().filter(pk=sale.pk).update(debt=debt)
    return debt


class Command(BaseCommand):
    help = "Rebuild the missing Debt for any credit sale that has none."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be repaired without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        found = list(orphan_credit_sales())

        if not found:
            self.stdout.write(self.style.SUCCESS("OK — no orphaned credit sales."))
            return

        ids = [s.pk for s in found]
        self.stderr.write(
            self.style.WARNING(
                f"{len(found)} credit sale(s) missing their debt: {ids}"
            )
        )
        self._alert(ids, repaired=not dry_run)

        if dry_run:
            self.stdout.write("--dry-run: nothing written.")
            return

        pharmacy_ids, repaired = set(), []
        with transaction.atomic():
            # Re-read under a row lock so a concurrent checkout cannot create
            # the debt between the scan above and the write below.
            for sale in orphan_credit_sales().select_for_update():
                debt = repair_sale(sale)
                repaired.append((sale.pk, debt.pk, str(debt.discounted_total)))
                pharmacy_ids.add(sale.store_id)

        # Balances are cached — drop them so the fix shows immediately.
        from apps.store.views import (
            invalidate_customers_quick_cache,
            invalidate_dashboard_cache,
        )

        for pid in pharmacy_ids:
            invalidate_dashboard_cache(pid)
            invalidate_customers_quick_cache(pid)

        for sale_pk, debt_pk, amount in repaired:
            self.stdout.write(
                self.style.SUCCESS(f"sale {sale_pk} → debt {debt_pk} ({amount})")
            )
        self.stdout.write(self.style.SUCCESS(f"Repaired {len(repaired)}."))

    @staticmethod
    def _alert(sale_ids, repaired):
        """Tell Sentry — finding anything here means the checkout guard was
        bypassed somehow, which we want to hear about, repaired or not."""
        try:
            import sentry_sdk
        except ImportError:  # pragma: no cover - Sentry is optional
            return
        verb = "repaired" if repaired else "detected (dry-run)"
        sentry_sdk.capture_message(
            f"repair_sale_debts {verb} {len(sale_ids)} credit sale(s) "
            f"with no linked debt: {sale_ids}",
            level="error",
        )
