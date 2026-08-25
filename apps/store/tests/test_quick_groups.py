"""The POS's quick-tap cards, as the shopkeeper arranged them.

Stored on the STORE, not in the browser. This is how the shop works, not a
preference of one machine: clearing the browser cache, or standing at a second
till, must not lose it — that is a phone call to the freelancer.
"""
from decimal import Decimal

from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.store import models

URL = "/api/v1/store/quick-groups/"


class QuickGroupsTests(APITestCase):
    def setUp(self):
        self.store = models.Store.objects.create(name="المودة", slug="almawdah")
        self.user = User.objects.create_user(
            username="owner", password="pw", store=self.store, role="owner"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _put(self, groups):
        return self.client.put(URL, {"groups": groups}, format="json")

    def test_a_new_store_starts_empty_so_the_app_uses_its_defaults(self):
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["groups"], [])

    def test_it_round_trips(self):
        groups = [
            {"key": "smoke", "label": "دخان", "icon": "cigarette", "product_ids": [1, 2, 3]},
            {"key": "eggs", "label": "بيض", "icon": "egg", "product_ids": [9]},
        ]
        self.assertEqual(self._put(groups).status_code, 200)
        self.assertEqual(self.client.get(URL).data["groups"], groups)

    def test_it_survives_the_browser_being_wiped(self):
        """The whole point: a second device sees the same layout."""
        self._put([{"label": "دخان", "product_ids": [1]}])
        other = APIClient()
        other.force_authenticate(
            user=User.objects.create_user(
                username="cashier", password="pw", store=self.store, role="staff"
            )
        )
        self.assertEqual(len(other.get(URL).data["groups"]), 1)

    def test_a_cashier_can_rearrange_the_counter(self):
        # No money and no permissions here; owner-only would mean a phone call
        # every time the shop moves things around.
        cashier = APIClient()
        cashier.force_authenticate(
            user=User.objects.create_user(
                username="c2", password="pw", store=self.store, role="staff"
            )
        )
        self.assertEqual(
            cashier.put(URL, {"groups": [{"label": "بيض", "product_ids": [1]}]},
                        format="json").status_code,
            200,
        )

    def test_duplicate_products_in_a_group_are_collapsed(self):
        r = self._put([{"label": "دخان", "product_ids": [5, 5, 7, 5]}])
        self.assertEqual(r.data["groups"][0]["product_ids"], [5, 7])

    def test_the_order_the_owner_chose_is_kept(self):
        r = self._put([{"label": "دخان", "product_ids": [9, 3, 7]}])
        self.assertEqual(r.data["groups"][0]["product_ids"], [9, 3, 7])

    def test_junk_ids_are_dropped_rather_than_rejecting_the_whole_layout(self):
        # A product deleted later must not make the layout unsaveable.
        r = self._put([{"label": "دخان", "product_ids": [1, "abc", None, 4]}])
        self.assertEqual(r.data["groups"][0]["product_ids"], [1, 4])

    def test_a_group_needs_a_name(self):
        self.assertEqual(self._put([{"label": "  ", "product_ids": []}]).status_code, 400)

    def test_absurd_sizes_are_refused(self):
        self.assertEqual(self._put([{"label": f"g{i}"} for i in range(20)]).status_code, 400)
        self.assertEqual(
            self._put([{"label": "دخان", "product_ids": list(range(100))}]).status_code,
            400,
        )

    def test_not_a_list_is_refused(self):
        self.assertEqual(self.client.put(URL, {"groups": "nope"}, format="json").status_code, 400)

    def test_another_store_has_its_own(self):
        other_store = models.Store.objects.create(name="أخرى", slug="other")
        other = APIClient()
        other.force_authenticate(
            user=User.objects.create_user(
                username="o", password="pw", store=other_store, role="owner"
            )
        )
        self._put([{"label": "دخان", "product_ids": [1]}])
        self.assertEqual(other.get(URL).data["groups"], [])

    def test_anonymous_is_refused(self):
        anon = APIClient()
        self.assertIn(anon.get(URL).status_code, (401, 403))
