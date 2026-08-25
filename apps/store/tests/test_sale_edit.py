"""Editing a sale in place.

The cashier rang the wrong item, or the wrong quantity, and the customer is
still at the counter. Re-ringing means the receipt already handed over points
at a voided invoice and the day's history shows two sales for one basket — so
the sale is corrected in place: same id, same receipt code, same position in
the day.

That is also exactly how a till gets robbed: ring ₪300, take the cash, edit the
invoice down to ₪30. Every test below exists because of one of those two
sentences.
"""
from decimal import Decimal

from django.db import transaction
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.store import models


class SaleEditTests(APITestCase):
    def setUp(self):
        self.store = models.Store.objects.create(name="المودة", slug="almawdah")
        self.user = User.objects.create_user(
            username="owner", password="pw", store=self.store, role="owner"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.rice = models.Product.objects.create(
            store=self.store, name="أرز", price=Decimal("10.00"),
            stock=Decimal("100"),
        )
        self.oil = models.Product.objects.create(
            store=self.store, name="زيت", price=Decimal("25.00"),
            stock=Decimal("50"),
        )
        self.customer = models.Customer.objects.create(
            store=self.store, name="أبو محمد"
        )

    # ── helpers ──────────────────────────────────────────────────────────
    def _sell(self, items=None, **over):
        body = {
            "payment_method": "cash",
            "items": items
            or [{"product": self.rice.pk, "quantity": "2", "unit_price": "10.00"}],
        }
        body.update(over)
        r = self.client.post("/api/v1/sales/", body, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def _edit(self, sale_id, **body):
        return self.client.patch(
            f"/api/v1/sales/{sale_id}/", body, format="json"
        )

    def _stock(self, product):
        return models.Product.unguarded.get(pk=product.pk).stock

    # ── identity ─────────────────────────────────────────────────────────
    def test_the_receipt_in_the_customers_hand_still_finds_the_sale(self):
        sale = self._sell()
        code = sale["receipt_code"]
        self.assertTrue(code)

        r = self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "1", "unit_price": "25.00"}],
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["id"], sale["id"])
        self.assertEqual(r.data["receipt_code"], code)

        found = self.client.get(f"/api/v1/sales/?search={code}")
        self.assertEqual([s["id"] for s in found.data["results"]], [sale["id"]])

    def test_an_edit_cannot_move_the_sale_into_another_days_takings(self):
        sale = self._sell()
        r = self._edit(
            sale["id"],
            created_at="2020-01-01T00:00:00Z",
            receipt_code="999999999999",
            items=[{"product": self.rice.pk, "quantity": "1", "unit_price": "10.00"}],
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["created_at"], sale["created_at"])
        self.assertEqual(r.data["receipt_code"], sale["receipt_code"])

    def test_it_does_not_create_a_second_sale(self):
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.rice.pk, "quantity": "5", "unit_price": "10.00"}],
        )
        self.assertEqual(models.Sale.unguarded.count(), 1)

    # ── the numbers ──────────────────────────────────────────────────────
    def test_the_total_follows_the_new_lines(self):
        sale = self._sell()
        self.assertEqual(Decimal(sale["discounted_total"]), Decimal("20.00"))
        r = self._edit(
            sale["id"],
            items=[
                {"product": self.rice.pk, "quantity": "1", "unit_price": "10.00"},
                {"product": self.oil.pk, "quantity": "2", "unit_price": "25.00"},
            ],
        )
        self.assertEqual(Decimal(r.data["total"]), Decimal("60.00"))
        self.assertEqual(Decimal(r.data["discounted_total"]), Decimal("60.00"))

    def test_the_old_lines_are_gone_not_appended(self):
        sale = self._sell()
        r = self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "1", "unit_price": "25.00"}],
        )
        self.assertEqual(len(r.data["items"]), 1)
        self.assertEqual(r.data["items"][0]["medication_name"], "زيت")

    def test_a_discount_survives_the_edit_when_it_is_sent_again(self):
        sale = self._sell(discounted_total="18.00")
        r = self._edit(
            sale["id"],
            discounted_total="15.00",
            items=[{"product": self.rice.pk, "quantity": "2", "unit_price": "10.00"}],
        )
        self.assertEqual(Decimal(r.data["total"]), Decimal("20.00"))
        self.assertEqual(Decimal(r.data["discounted_total"]), Decimal("15.00"))

    # ── stock ────────────────────────────────────────────────────────────
    def test_raising_a_quantity_costs_only_the_difference(self):
        # 100 - 2 = 98 after the sale; going 2 → 3 must land on 97, not on
        # "give 2 back, take 3" arithmetic that briefly invents stock.
        sale = self._sell()
        self.assertEqual(self._stock(self.rice), Decimal("98.000"))
        self._edit(
            sale["id"],
            items=[{"product": self.rice.pk, "quantity": "3", "unit_price": "10.00"}],
        )
        self.assertEqual(self._stock(self.rice), Decimal("97.000"))

    def test_lowering_a_quantity_puts_the_difference_back(self):
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.rice.pk, "quantity": "1", "unit_price": "10.00"}],
        )
        self.assertEqual(self._stock(self.rice), Decimal("99.000"))

    def test_swapping_the_product_moves_stock_on_both(self):
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "2", "unit_price": "25.00"}],
        )
        self.assertEqual(self._stock(self.rice), Decimal("100.000"))  # fully back
        self.assertEqual(self._stock(self.oil), Decimal("48.000"))

    def test_an_edit_that_changes_no_quantity_leaves_stock_alone(self):
        sale = self._sell()
        before = self._stock(self.rice)
        self._edit(
            sale["id"],
            items=[{"product": self.rice.pk, "quantity": "2", "unit_price": "12.00"}],
        )
        self.assertEqual(self._stock(self.rice), before)

    def test_a_return_moves_stock_the_other_way(self):
        sale = self._sell(is_return=True)
        self.assertEqual(self._stock(self.rice), Decimal("102.000"))
        self._edit(
            sale["id"],
            items=[{"product": self.rice.pk, "quantity": "5", "unit_price": "10.00"}],
        )
        self.assertEqual(self._stock(self.rice), Decimal("105.000"))

    def test_a_migrated_shamel_invoice_never_touches_stock(self):
        """Those 145,647 rows never took stock out — reversing them would
        invent stock that was never on the shelf."""
        sale = self._sell(note="shamel:12345")
        models.Product.unguarded.filter(pk=self.rice.pk).update(
            stock=Decimal("100")
        )
        self._edit(
            sale["id"],
            note="shamel:12345",
            items=[{"product": self.rice.pk, "quantity": "9", "unit_price": "10.00"}],
        )
        self.assertEqual(self._stock(self.rice), Decimal("100.000"))

    # ── the customer's balance ───────────────────────────────────────────
    def test_a_credit_sales_debt_follows_the_edit(self):
        sale = self._sell(payment_method="debt", customer=self.customer.pk)
        debt_id = sale["debt"]
        self.assertIsNotNone(debt_id)

        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "4", "unit_price": "25.00"}],
        )
        debt = models.Debt.unguarded.get(pk=debt_id)
        # Same debt row — anything already pointing at it still does.
        self.assertEqual(debt.total, Decimal("100.00"))
        self.assertEqual(debt.discounted_total, Decimal("100.00"))
        self.assertEqual(
            [i.medication_name for i in debt.items.all()], ["زيت"]
        )

    def test_switching_a_credit_sale_to_cash_removes_the_debt(self):
        sale = self._sell(payment_method="debt", customer=self.customer.pk)
        debt_id = sale["debt"]
        r = self._edit(
            sale["id"],
            payment_method="cash",
            customer=None,
            items=[{"product": self.rice.pk, "quantity": "2", "unit_price": "10.00"}],
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(r.data["debt"])
        self.assertFalse(models.Debt.unguarded.filter(pk=debt_id).exists())

    def test_switching_a_cash_sale_to_credit_creates_the_debt(self):
        sale = self._sell()
        r = self._edit(
            sale["id"],
            payment_method="debt",
            customer=self.customer.pk,
            items=[{"product": self.rice.pk, "quantity": "2", "unit_price": "10.00"}],
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNotNone(r.data["debt"])
        self.assertEqual(
            models.Debt.unguarded.get(pk=r.data["debt"]).total, Decimal("20.00")
        )

    def test_a_credit_sale_cannot_be_edited_into_having_no_customer(self):
        sale = self._sell(payment_method="debt", customer=self.customer.pk)
        r = self._edit(
            sale["id"],
            customer=None,
            items=[{"product": self.rice.pk, "quantity": "2", "unit_price": "10.00"}],
        )
        self.assertEqual(r.status_code, 400)

    def test_a_settled_debt_is_refused_outright(self):
        """The customer already paid. Rewriting what they owed, weeks later,
        is not a correction — it is a different decision, and a human makes
        it."""
        sale = self._sell(payment_method="debt", customer=self.customer.pk)
        models.Debt.unguarded.filter(pk=sale["debt"]).update(is_paid=True)
        r = self._edit(
            sale["id"],
            items=[{"product": self.rice.pk, "quantity": "1", "unit_price": "10.00"}],
        )
        self.assertEqual(r.status_code, 400)
        # and nothing moved
        self.assertEqual(self._stock(self.rice), Decimal("98.000"))
        self.assertEqual(
            models.Sale.unguarded.get(pk=sale["id"]).total, Decimal("20.00")
        )

    # ── the history ──────────────────────────────────────────────────────
    def test_the_previous_version_is_kept_whole(self):
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "1", "unit_price": "25.00"}],
        )
        r = self.client.get(f"/api/v1/sales/{sale['id']}/revisions/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["results"]), 1)

        v1 = r.data["results"][0]
        self.assertEqual(v1["version"], 1)
        self.assertEqual(v1["edited_by"], "owner")
        self.assertEqual(v1["snapshot"]["discounted_total"], "20.00")
        self.assertEqual(
            [i["medication_name"] for i in v1["snapshot"]["items"]], ["أرز"]
        )

    def test_every_edit_adds_a_version(self):
        sale = self._sell()
        for qty in ("3", "4", "5"):
            self._edit(
                sale["id"],
                items=[
                    {"product": self.rice.pk, "quantity": qty, "unit_price": "10.00"}
                ],
            )
        r = self.client.get(f"/api/v1/sales/{sale['id']}/revisions/")
        self.assertEqual([v["version"] for v in r.data["results"]], [3, 2, 1])
        # newest first, and each one holds the state it replaced
        self.assertEqual(r.data["results"][-1]["snapshot"]["items"][0]["quantity"], "2.000")

    def test_the_snapshot_survives_the_product_being_deleted(self):
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "1", "unit_price": "25.00"}],
        )
        models.Product.unguarded.filter(pk=self.rice.pk).delete()
        r = self.client.get(f"/api/v1/sales/{sale['id']}/revisions/")
        self.assertEqual(
            r.data["results"][0]["snapshot"]["items"][0]["medication_name"], "أرز"
        )

    def test_the_sale_says_how_many_times_it_was_edited(self):
        sale = self._sell()
        self.assertEqual(sale["revision_count"], 0)
        r = self._edit(
            sale["id"],
            items=[{"product": self.rice.pk, "quantity": "1", "unit_price": "10.00"}],
        )
        self.assertEqual(r.data["revision_count"], 1)
        listed = self.client.get("/api/v1/sales/").data["results"][0]
        self.assertEqual(listed["revision_count"], 1)

    def test_a_multi_line_sale_does_not_over_count_its_revisions(self):
        # The search filter joins `items`; without distinct=True a three-line
        # sale would report three revisions for one edit.
        sale = self._sell(
            items=[
                {"product": self.rice.pk, "quantity": "1", "unit_price": "10.00"},
                {"product": self.oil.pk, "quantity": "1", "unit_price": "25.00"},
            ]
        )
        self._edit(
            sale["id"],
            items=[{"product": self.rice.pk, "quantity": "1", "unit_price": "10.00"}],
        )
        listed = self.client.get("/api/v1/sales/").data["results"][0]
        self.assertEqual(listed["revision_count"], 1)

    def test_the_edit_lands_in_the_audit_log(self):
        """The list an owner actually reads. A sale quietly edited from ₪300
        to ₪30 belongs next to the voids."""
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.rice.pk, "quantity": "1", "unit_price": "10.00"}],
        )
        entry = models.AuditLog.unguarded.filter(
            action=models.AuditLog.ACTION_SALE_EDIT
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor_id, self.user.pk)
        self.assertIn("20.00", entry.summary)
        self.assertIn("10.00", entry.summary)
        self.assertEqual(entry.request["before"]["discounted_total"], "20.00")
        self.assertEqual(entry.request["after"]["discounted_total"], "10.00")

    # ── the guards ───────────────────────────────────────────────────────
    def test_an_edit_cannot_empty_a_sale(self):
        sale = self._sell()
        r = self._edit(sale["id"], items=[])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._stock(self.rice), Decimal("98.000"))

    def test_another_stores_sale_is_invisible(self):
        other_store = models.Store.objects.create(name="غيرها", slug="other")
        other_user = User.objects.create_user(
            username="them", password="pw", store=other_store, role="owner"
        )
        sale = self._sell()
        intruder = APIClient()
        intruder.force_authenticate(user=other_user)
        r = intruder.patch(
            f"/api/v1/sales/{sale['id']}/",
            {"items": [{"product": self.rice.pk, "quantity": "1", "unit_price": "1.00"}]},
            format="json",
        )
        self.assertEqual(r.status_code, 404)

    def test_a_product_from_another_store_cannot_be_smuggled_in(self):
        other_store = models.Store.objects.create(name="غيرها", slug="other")
        theirs = models.Product.objects.create(
            store=other_store, name="سكر", price=Decimal("5.00"), stock=Decimal("9")
        )
        sale = self._sell()
        r = self._edit(
            sale["id"],
            items=[{"product": theirs.pk, "quantity": "1", "unit_price": "5.00"}],
        )
        self.assertEqual(r.status_code, 400)

    def test_the_row_lock_does_not_use_an_outer_join(self):
        """PostgreSQL refuses FOR UPDATE on the nullable side of an outer join.

            NotSupportedError: FOR UPDATE cannot be applied to the nullable
            side of an outer join

        `customer` and `debt` are nullable, so ONE select_related on the locked
        query is enough to make every edit a 500 — in production only. SQLite
        has no row locks, so Django omits the FOR UPDATE clause entirely and
        the whole suite goes green. That is exactly how this shipped.

        So this asserts on the generated SQL rather than on behaviour: the
        locked query must not join anything.
        """
        from apps.store.serializers import SaleSerializer

        sql = str(
            models.Sale.objects.for_pharmacy(self.store.pk)
            .select_for_update()
            .prefetch_related("items")
            .query
        )
        self.assertNotIn("JOIN", sql.upper())

        # And the real thing, so the test cannot drift away from production.
        sale = self._sell()
        with transaction.atomic():
            locked = SaleSerializer._locked_sale(self.store.pk, sale["id"])
        self.assertEqual(locked.pk, sale["id"])

    def test_editing_a_credit_sale_still_reads_customer_and_debt(self):
        """The lock query no longer select_relates them — they must still load.

        Dropping select_related is only safe because both are read lazily a
        moment later. If either stopped resolving, a credit sale would lose its
        debt on the first edit and the customer's balance would be wrong.
        """
        sale = self._sell(payment_method="debt", customer=self.customer.pk)
        r = self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "2", "unit_price": "25.00"}],
        )
        self.assertEqual(r.status_code, 200, r.data)
        debt = models.Debt.unguarded.get(pk=sale["debt"])
        self.assertEqual(debt.total, Decimal("50.00"))
        self.assertEqual(debt.customer_id, self.customer.pk)
        # the snapshot reads sale.customer.name — the lazy load has to work
        rev = self.client.get(f"/api/v1/sales/{sale['id']}/revisions/")
        self.assertEqual(
            rev.data["results"][0]["snapshot"]["customer_name"], "أبو محمد"
        )

    # ── restoring an earlier version ─────────────────────────────────────
    def _restore(self, sale_id, version):
        return self.client.post(
            f"/api/v1/sales/{sale_id}/revisions/{version}/restore/", {}, format="json"
        )

    def test_restoring_puts_the_lines_and_the_total_back(self):
        sale = self._sell()  # أرز ×2 = 20.00
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "4", "unit_price": "25.00"}],
        )
        r = self._restore(sale["id"], 1)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(r.data["discounted_total"]), Decimal("20.00"))
        self.assertEqual([i["medication_name"] for i in r.data["items"]], ["أرز"])

    def test_restoring_moves_stock_back_too(self):
        sale = self._sell()
        self.assertEqual(self._stock(self.rice), Decimal("98.000"))
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "4", "unit_price": "25.00"}],
        )
        self.assertEqual(self._stock(self.rice), Decimal("100.000"))
        self.assertEqual(self._stock(self.oil), Decimal("46.000"))

        self._restore(sale["id"], 1)
        self.assertEqual(self._stock(self.rice), Decimal("98.000"))
        self.assertEqual(self._stock(self.oil), Decimal("50.000"))

    def test_a_restore_is_itself_recorded_as_a_version(self):
        """It is an edit, not an undo. Rewinding silently would erase the very
        record that makes in-place editing safe to allow."""
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "4", "unit_price": "25.00"}],
        )
        self._restore(sale["id"], 1)
        r = self.client.get(f"/api/v1/sales/{sale['id']}/revisions/")
        self.assertEqual([v["version"] for v in r.data["results"]], [2, 1])
        # v2 holds the state the restore replaced — the edited one
        self.assertEqual(r.data["results"][0]["snapshot"]["discounted_total"], "100.00")

    def test_restoring_the_same_version_twice_is_harmless(self):
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "4", "unit_price": "25.00"}],
        )
        self._restore(sale["id"], 1)
        r = self._restore(sale["id"], 1)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(r.data["discounted_total"]), Decimal("20.00"))
        self.assertEqual(self._stock(self.rice), Decimal("98.000"))

    def test_a_deleted_product_is_kept_as_a_named_line(self):
        """The money the customer paid must come back exactly. The catalogue
        link is a convenience — stock cannot move for a row that is gone."""
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "1", "unit_price": "25.00"}],
        )
        models.Product.unguarded.filter(pk=self.rice.pk).delete()

        r = self._restore(sale["id"], 1)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(r.data["discounted_total"]), Decimal("20.00"))
        self.assertEqual(r.data["items"][0]["medication_name"], "أرز")
        self.assertIsNone(r.data["items"][0]["product"])

    def test_restoring_a_credit_sale_rebuilds_the_debt(self):
        sale = self._sell(payment_method="debt", customer=self.customer.pk)
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "4", "unit_price": "25.00"}],
        )
        r = self._restore(sale["id"], 1)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(
            models.Debt.unguarded.get(pk=r.data["debt"]).total, Decimal("20.00")
        )

    def test_a_credit_sale_whose_customer_is_gone_restores_as_cash(self):
        # A debt owed by nobody is not a thing that should exist.
        sale = self._sell(payment_method="debt", customer=self.customer.pk)
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "1", "unit_price": "25.00"}],
        )
        models.Customer.unguarded.filter(pk=self.customer.pk).delete()
        r = self._restore(sale["id"], 1)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["payment_method"], "cash")

    def test_the_restore_lands_in_the_audit_log(self):
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "4", "unit_price": "25.00"}],
        )
        self._restore(sale["id"], 1)
        self.assertEqual(
            models.AuditLog.unguarded.filter(
                action=models.AuditLog.ACTION_SALE_EDIT
            ).count(),
            2,
        )

    def test_an_unknown_version_is_404(self):
        sale = self._sell()
        self.assertEqual(self._restore(sale["id"], 9).status_code, 404)

    def test_another_stores_sale_cannot_be_restored(self):
        other_store = models.Store.objects.create(name="غيرها", slug="other2")
        other_user = User.objects.create_user(
            username="them2", password="pw", store=other_store, role="owner"
        )
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "1", "unit_price": "25.00"}],
        )
        intruder = APIClient()
        intruder.force_authenticate(user=other_user)
        r = intruder.post(
            f"/api/v1/sales/{sale['id']}/revisions/1/restore/", {}, format="json"
        )
        self.assertEqual(r.status_code, 404)

    def test_restoring_keeps_the_receipt_code(self):
        sale = self._sell()
        self._edit(
            sale["id"],
            items=[{"product": self.oil.pk, "quantity": "1", "unit_price": "25.00"}],
        )
        r = self._restore(sale["id"], 1)
        self.assertEqual(r.data["receipt_code"], sale["receipt_code"])
        self.assertEqual(r.data["id"], sale["id"])

    def test_put_is_not_allowed(self):
        sale = self._sell()
        r = self.client.put(f"/api/v1/sales/{sale['id']}/", {}, format="json")
        self.assertEqual(r.status_code, 405)
