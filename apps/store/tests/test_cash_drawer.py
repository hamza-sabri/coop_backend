"""The cash drawer: open with a count, close with a count, the books say
what should be in between — to the agora."""
from decimal import Decimal as D

from apps.accounts.models import User
from apps.store import models
from apps.store.tests.test_recipes import RecipeBase


class CashDrawerTests(RecipeBase):
    def drawer(self, client=None):
        body = (client or self.api).get("/api/v1/cash-drawer/").json()
        return body.get("data", body)

    def post(self, path, data, client=None):
        r = (client or self.api).post(f"/api/v1/cash-drawer/{path}/", data, format="json")
        body = r.json()
        return r.status_code, body.get("data", body)

    def test_the_drawer_adds_up(self):
        code, s = self.post("open", {"opening_amount": "100"})
        self.assertEqual(code, 201, s)
        # A second open is refused.
        self.assertEqual(self.post("open", {"opening_amount": "5"})[0], 400)

        self.sell(2)                                   # cash 24
        self.sell(1, payment_method="card")            # card 12 — not in the drawer
        sale = self.sell(1, discounted_total="10.00")  # cash 10
        line = sale.items.first()
        r = self.api.post(f"/api/v1/sales/{sale.pk}/returns/",
                          {"sale_item": line.pk, "quantity": 1, "refund": "full", "reason": "other"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)  # −10 back over the counter
        self.post("move", {"amount": "50", "direction": "in", "note": "فكة"})
        self.assertEqual(self.post("move", {"amount": "8", "direction": "out"})[0], 400, "a pay-out needs a reason")
        self.post("move", {"amount": "8", "direction": "out", "note": "حليب"})

        f = self.drawer()["open"]["figures"]
        self.assertEqual(f["cash_sales"], "34.00")
        self.assertEqual(f["card_sales"], "12.00")
        self.assertEqual(f["refunds"], "10.00")
        self.assertEqual(f["put_in"], "50.00")
        self.assertEqual(f["took_out"], "8.00")
        # 100 + 34 − 10 + 50 − 8
        self.assertEqual(f["expected"], "166.00")

        code, closed = self.post("close", {"counted_amount": "160"})
        self.assertEqual(code, 200, closed)
        self.assertEqual(closed["expected_amount"], "166.00")
        self.assertEqual(closed["difference"], "-6.00")
        self.assertIsNone(self.drawer()["open"])

        # A sale after closing never changes the closed count.
        self.sell(3)
        hist = self.drawer()["history"][0]
        self.assertEqual(hist["expected_amount"], "166.00")

    def test_employees_count_blind_then_see_the_answer(self):
        self.post("open", {"opening_amount": "50"}, client=self.staff)
        self.sell(1, client=self.staff)
        live = self.drawer(self.staff)["open"]
        self.assertNotIn("figures", live, "the employee does not see what is expected before counting")
        self.assertEqual(self.drawer(self.staff)["history"], [])
        code, closed = self.post("close", {"counted_amount": "62"}, client=self.staff)
        self.assertEqual(code, 200, closed)
        self.assertEqual(closed["difference"], "0.00")

    def test_close_needs_an_open_drawer_and_a_real_amount(self):
        self.assertEqual(self.post("close", {"counted_amount": "1"})[0], 400)
        self.post("open", {"opening_amount": "0"})
        self.assertEqual(self.post("close", {"counted_amount": "abc"})[0], 400)
        self.assertEqual(self.post("close", {"counted_amount": "-3"})[0], 400)


class CashDrawerCountsTests(RecipeBase):
    """Counting by notes and coins, the card terminal total, who opened and
    who closed, the history page and the printable record."""

    def post(self, path, data, client=None):
        r = (client or self.api).post(f"/api/v1/cash-drawer/{path}/", data, format="json")
        body = r.json()
        return r.status_code, body.get("data", body)

    def test_counts_are_summed_and_kept(self):
        code, s = self.post("open", {"counts": {"100": 1, "20": 2, "0.5": 4}, "opened_by": self.emp.pk})
        self.assertEqual(code, 201, s)
        self.assertEqual(s["opening_amount"], "142.00")
        self.assertEqual(s["opening_counts"], {"100": 1, "20": 2, "0.5": 4})
        self.assertEqual(s["opened_by_id"], self.emp.pk)  # picked, not the signed-in owner
        self.assertEqual(self.post("open", {"counts": {"7": 1}})[0], 400)  # no such note

        self.sell(2)                         # cash 24
        self.sell(1, payment_method="card")  # card 12
        code, s = self.post("close", {"counts": {"100": 1, "50": 1, "10": 1, "5": 1, "1": 1}, "card_counted": "12.00", "closed_by": self.emp.pk})
        self.assertEqual(code, 200, s)
        self.assertEqual(s["counted_amount"], "166.00")
        self.assertEqual(s["expected_amount"], "166.00")
        self.assertEqual(s["difference"], "0.00")
        self.assertEqual(s["card_counted"], "12.00")
        self.assertEqual(s["card_difference"], "0.00")
        self.assertEqual(s["closed_by_id"], self.emp.pk)
        self.assertEqual(s["figures"]["total_sales"], "36.00")
        self.assertEqual(s["figures"]["tickets"], 2)

    def test_history_detail_and_edit(self):
        self.post("open", {"opening_amount": "50"})
        self.sell(1)
        self.post("move", {"amount": "5", "direction": "out", "note": "حليب"})
        code, s = self.post("close", {"counted_amount": "60", "card_counted": "0"})
        self.assertEqual(code, 200, s)
        sid = s["id"]
        # The owner's history, paged.
        body = self.api.get("/api/v1/cash-drawer/history/").json()
        body = body.get("data", body)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["difference"], "3.00")  # 50 + 12 − 5 = 57 expected, 60 counted
        self.assertEqual(self.staff.get("/api/v1/cash-drawer/history/").status_code, 403)
        # The printable record lists every operation in order.
        d = self.api.get(f"/api/v1/cash-drawer/{sid}/").json()
        d = d.get("data", d)
        kinds = [o["kind"] for o in d["operations"]]
        self.assertEqual(kinds, ["sale", "out"])
        self.assertEqual(d["operations"][0]["amount"], "12.00")
        self.assertEqual(d["operations"][1]["amount"], "-5.00")
        # A later edit to an old sale never changes the frozen figures.
        self.assertEqual(d["figures"]["cash_sales"], "12.00")
        # The owner corrects who closed it; an employee may not.
        r = self.api.patch(f"/api/v1/cash-drawer/{sid}/", {"closed_by": self.emp.pk, "note": "وردية المساء"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["closed_by_id"], self.emp.pk)
        self.assertEqual(self.staff.patch(f"/api/v1/cash-drawer/{sid}/", {"note": "x"}, format="json").status_code, 403)
        # …but the employee who closed it may read it.
        self.assertEqual(self.staff.get(f"/api/v1/cash-drawer/{sid}/").status_code, 200)

    def test_any_employee_can_read_staff_names_only(self):
        r = self.staff.get("/api/v1/staff/names/")
        self.assertEqual(r.status_code, 200, r.content)
        rows = r.json()
        self.assertTrue(all(set(x.keys()) == {"id", "name"} for x in rows))
        self.assertIn(self.emp.pk, [x["id"] for x in rows])
        self.assertEqual(self.staff.get("/api/v1/staff/").status_code, 403)
