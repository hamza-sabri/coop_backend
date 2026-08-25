"""Today's takings, where "today" does not start at midnight.

The shop is still selling at 1am and cashes up in the morning, so a sale rung
at 00:30 belongs to the day that is still running. Counting by calendar date
would split one night across two figures and match the drawer in neither.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.store import models

URL = "/api/v1/sales/day_summary/"


@override_settings(TIME_ZONE="Asia/Hebron", BUSINESS_DAY_START_HOUR=4)
class DaySummaryTests(APITestCase):
    def setUp(self):
        self.store = models.Store.objects.create(name="المودة", slug="almawdah")
        self.user = User.objects.create_user(
            username="owner", password="pw", store=self.store, role="owner"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.rice = models.Product.objects.create(
            store=self.store, name="أرز", price=Decimal("10.00"), stock=Decimal("500")
        )
        # The cards follow the shop's OWN quick-tap groups — there is no
        # built-in list of what to total. Two here, standing in for whatever a
        # given trade cares about.
        self.topup = models.Product.objects.create(
            store=self.store, name="تعبئة كرت", price=Decimal("1.00"), stock=Decimal("0")
        )
        self.smoke_a = models.Product.objects.create(
            store=self.store, name="دخان امبريال", price=Decimal("26.00"), stock=Decimal("50")
        )
        self.smoke_b = models.Product.objects.create(
            store=self.store, name="سيجارة حلل", price=Decimal("2.00"), stock=Decimal("50")
        )
        models.Store.objects.filter(pk=self.store.pk).update(
            pos_quick_groups=[
                {"key": "topup", "label": "جوال", "icon": "smartphone",
                 "product_ids": [self.topup.id]},
                {"key": "smoke", "label": "دخان", "icon": "cigarette",
                 "product_ids": [self.smoke_a.id, self.smoke_b.id]},
            ]
        )

    # ── helpers ──────────────────────────────────────────────────────────
    def _sale_at(self, when, items, is_return=False):
        """A sale stamped at a wall-clock moment in the shop's own timezone."""
        sale = models.Sale.objects.create(
            store=self.store, payment_method="cash", is_return=is_return
        )
        total = Decimal("0.00")
        by_name = {
            "أرز": self.rice,
            "تعبئة كرت جوال": self.topup,
            "شحن رصيد": self.topup,
            "دخان امبريال": self.smoke_a,
            "دخان عربي": self.smoke_a,
            "سيجارة حلل": self.smoke_b,
        }
        for name, qty, price in items:
            models.SaleItem.objects.create(
                sale=sale,
                product=by_name.get(name),
                medication_name=name,
                quantity=Decimal(str(qty)),
                unit_price=Decimal(str(price)),
            )
            total += Decimal(str(qty)) * Decimal(str(price))
        sale.total = total
        sale.discounted_total = total
        sale.save()
        # created_at is auto_now_add, so it has to be forced afterwards.
        models.Sale.unguarded.filter(pk=sale.pk).update(created_at=when)
        return sale

    def _at(self, day_offset, hour, minute=0):
        base = timezone.localtime().replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        return base + timedelta(days=day_offset)

    def _get(self):
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def _amount(self, data, key):
        return Decimal(
            str(next(g["amount"] for g in data["groups"] if g["key"] == key))
        )

    # ── the boundary ─────────────────────────────────────────────────────
    def test_a_sale_after_midnight_belongs_to_the_day_still_running(self):
        """00:30 is last night's takings, not a new day's."""
        with mock.patch.object(
            timezone, "localtime", return_value=self._at(0, 1, 0)
        ):
            self._sale_at(self._at(0, 0, 30), [("أرز", 1, "10.00")])
            data = self._get()
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("10.00"))
        self.assertEqual(data["total"]["count"], 1)

    def test_the_day_rolls_over_at_four_not_at_midnight(self):
        now = self._at(0, 10, 0)  # mid-morning: today's day started at 04:00
        with mock.patch.object(timezone, "localtime", return_value=now):
            self._sale_at(self._at(0, 3, 30), [("أرز", 1, "10.00")])  # before
            self._sale_at(self._at(0, 4, 30), [("أرز", 2, "10.00")])  # after
            data = self._get()
        # Only the 04:30 sale counts.
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("20.00"))
        self.assertEqual(data["total"]["count"], 1)

    def test_a_sale_from_the_previous_evening_is_excluded_after_rollover(self):
        now = self._at(0, 9, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            self._sale_at(self._at(-1, 21, 0), [("أرز", 5, "10.00")])
            data = self._get()
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("0.00"))

    def test_the_window_it_reports_is_the_window_it_used(self):
        now = self._at(0, 10, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            data = self._get()
        start = datetime.fromisoformat(data["day_start"])
        end = datetime.fromisoformat(data["day_end"])
        self.assertEqual(start.hour, 4)
        self.assertEqual((end - start), timedelta(days=1))
        self.assertEqual(data["cutover_hour"], 4)

    @override_settings(BUSINESS_DAY_START_HOUR=6)
    def test_the_rollover_hour_is_configurable(self):
        now = self._at(0, 10, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            self._sale_at(self._at(0, 5, 0), [("أرز", 1, "10.00")])  # before 06
            data = self._get()
        self.assertEqual(data["cutover_hour"], 6)
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("0.00"))

    # ── the groups ───────────────────────────────────────────────────────
    def test_it_splits_out_jawwal_and_cigarettes(self):
        now = self._at(0, 12, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            self._sale_at(self._at(0, 9, 0), [("تعبئة كرت جوال", 1, "50.00")])
            self._sale_at(self._at(0, 10, 0), [("دخان امبريال", 2, "26.00")])
            self._sale_at(self._at(0, 11, 0), [("سيجارة حلل", 3, "2.00")])
            self._sale_at(self._at(0, 11, 30), [("أرز", 1, "10.00")])
            data = self._get()

        self.assertEqual(self._amount(data, "topup"), Decimal("50.00"))
        # 52.00 + 6.00 — both are tobacco under different catalogue names.
        self.assertEqual(self._amount(data, "smoke"), Decimal("58.00"))
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("118.00"))

    def test_شحن_رصيد_counts_as_jawwal_too(self):
        """The older catalogue row, before the جوال button existed."""
        now = self._at(0, 12, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            self._sale_at(self._at(0, 9, 0), [("شحن رصيد", 1, "20.00")])
            data = self._get()
        self.assertEqual(self._amount(data, "topup"), Decimal("20.00"))

    def test_a_group_counts_receipts_not_lines(self):
        # "How many customers bought cigarettes" — one receipt with two
        # tobacco lines is one customer.
        now = self._at(0, 12, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            self._sale_at(
                self._at(0, 9, 0),
                [("دخان عربي", 1, "5.00"), ("سيجارة حلل", 1, "2.00")],
            )
            data = self._get()
        smoke = next(g for g in data["groups"] if g["key"] == "smoke")
        self.assertEqual(smoke["count"], 1)

    def test_a_return_subtracts(self):
        now = self._at(0, 12, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            self._sale_at(self._at(0, 9, 0), [("دخان عربي", 4, "5.00")])
            self._sale_at(self._at(0, 10, 0), [("دخان عربي", 1, "5.00")], is_return=True)
            data = self._get()
        self.assertEqual(self._amount(data, "smoke"), Decimal("15.00"))
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("15.00"))

    def test_it_sends_the_group_membership_so_the_till_can_apply_it_offline(self):
        """The till adds its own queued sales to these figures during a cut.

        It must group them the SAME way the server does, so it is told which
        products belong to each card rather than keeping its own copy of the
        rule — two copies drift the first time the shop rearranges a group.
        """
        now = self._at(0, 12, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            data = self._get()
        smoke = next(g for g in data["groups"] if g["key"] == "smoke")
        self.assertEqual(
            sorted(smoke["product_ids"]), sorted([self.smoke_a.id, self.smoke_b.id])
        )

    def test_a_shop_that_has_configured_nothing_gets_no_cards(self):
        """No built-in list: which lines matter is a fact about the trade."""
        models.Store.objects.filter(pk=self.store.pk).update(pos_quick_groups=[])
        now = self._at(0, 12, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            data = self._get()
        self.assertEqual(data["groups"], [])

    def test_an_empty_day_reports_zero_rather_than_nothing(self):
        now = self._at(0, 12, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            data = self._get()
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("0.00"))
        self.assertEqual(data["total"]["count"], 0)
        self.assertEqual(
            sorted(g["key"] for g in data["groups"]), ["smoke", "topup"]
        )
        self.assertEqual(self._amount(data, "topup"), Decimal("0.00"))

    # ── periods ──────────────────────────────────────────────────────────
    def _get_period(self, **params):
        r = self.client.get(URL, params)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_week_covers_the_last_seven_trading_days(self):
        now = self._at(0, 12, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            self._sale_at(self._at(-6, 10, 0), [("أرز", 1, "10.00")])   # inside
            self._sale_at(self._at(-7, 10, 0), [("أرز", 9, "10.00")])   # outside
            data = self._get_period(period="week")
        self.assertEqual(data["period"], "week")
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("10.00"))

    def test_month_starts_on_the_first_at_the_rollover_hour(self):
        now = self._at(0, 12, 0).replace(day=15)
        with mock.patch.object(timezone, "localtime", return_value=now):
            data = self._get_period(period="month")
        start = datetime.fromisoformat(data["day_start"])
        self.assertEqual(start.day, 1)
        self.assertEqual(start.hour, 4)  # never midnight

    def test_a_custom_range_is_inclusive_of_both_ends(self):
        now = self._at(0, 12, 0)
        day = now.date()
        with mock.patch.object(timezone, "localtime", return_value=now):
            self._sale_at(self._at(-2, 10, 0), [("أرز", 1, "10.00")])
            self._sale_at(self._at(0, 10, 0), [("أرز", 2, "10.00")])
            data = self._get_period(
                **{
                    "from": (day - timedelta(days=2)).isoformat(),
                    "to": day.isoformat(),
                }
            )
        self.assertEqual(data["period"], "custom")
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("30.00"))

    def test_a_backwards_range_is_read_the_right_way_round(self):
        now = self._at(0, 12, 0)
        day = now.date()
        with mock.patch.object(timezone, "localtime", return_value=now):
            self._sale_at(self._at(-1, 10, 0), [("أرز", 1, "10.00")])
            data = self._get_period(
                **{"from": day.isoformat(), "to": (day - timedelta(days=1)).isoformat()}
            )
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("10.00"))

    def test_an_unknown_period_falls_back_to_the_day(self):
        now = self._at(0, 12, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            data = self._get_period(period="decade")
        self.assertEqual(data["period"], "day")

    def test_a_custom_range_still_cuts_at_the_rollover_not_midnight(self):
        now = self._at(0, 12, 0)
        day = now.date()
        with mock.patch.object(timezone, "localtime", return_value=now):
            # 02:00 belongs to the PREVIOUS trading day, so a range that starts
            # today must not include it.
            self._sale_at(self._at(0, 2, 0), [("أرز", 5, "10.00")])
            data = self._get_period(**{"from": day.isoformat(), "to": day.isoformat()})
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("0.00"))

    # ── scoping ──────────────────────────────────────────────────────────
    def test_another_stores_takings_are_invisible(self):
        other = models.Store.objects.create(name="غيرها", slug="other")
        sale = models.Sale.objects.create(store=other, payment_method="cash")
        models.SaleItem.objects.create(
            sale=sale, medication_name="دخان عربي",
            quantity=Decimal("9"), unit_price=Decimal("5.00"),
        )
        sale.total = sale.discounted_total = Decimal("45.00")
        sale.save()
        now = self._at(0, 12, 0)
        with mock.patch.object(timezone, "localtime", return_value=now):
            models.Sale.unguarded.filter(pk=sale.pk).update(
                created_at=self._at(0, 9, 0)
            )
            data = self._get()
        self.assertEqual(Decimal(str(data["total"]["amount"])), Decimal("0.00"))
        self.assertEqual(self._amount(data, "smoke"), Decimal("0.00"))
