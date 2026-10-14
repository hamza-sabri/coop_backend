"""Pull what is new in Extra P straight from Extra P, and load it.

Used by `python manage.py sync_extrap <store>` during the cut-over, while the
shop still rings some sales on Extra P: each run signs in to Extra P with the
cashier account, finds every sale newer than the last one already loaded, reads
its lines and its payment (for the real time of day and cash/card), and hands
the result to `extrap.load()` — the same loader the first full import used.

Nothing in Extra P is changed: every call here is a read. Re-running is always
safe — sales already loaded are skipped by their Extra P id.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from html import unescape

import requests

log = logging.getLogger(__name__)

DEFAULT_URL = "https://coop.extrap.net"
UA = "Mozilla/5.0 (compatible; KoupSync/1.0)"
PLACEHOLDER_IMG = "zummXD2dvAtI.png"


class SyncError(Exception):
    pass


def _strip(v) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(str(v)))).strip()


def _num(v) -> float:
    try:
        return float(_strip(v).replace(",", "") or 0)
    except ValueError:
        return 0.0


class ExtraP:
    """A signed-in session against one Extra P (SalePro) shop."""

    def __init__(self, base: str = DEFAULT_URL, timeout: int = 30):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.csrf = ""
        self.perms: list[str] = []

    # ── auth ────────────────────────────────────────────────────────────
    def _token_from(self, html: str) -> str:
        m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html) or re.search(
            r'name="_token"\s+value="([^"]+)"', html
        )
        return m.group(1) if m else ""

    def login(self, username: str, password: str) -> None:
        r = self.s.get(f"{self.base}/login", timeout=self.timeout)
        r.raise_for_status()
        token = self._token_from(r.text)
        if not token:
            raise SyncError("Extra P login page changed — no _token found.")
        r = self.s.post(
            f"{self.base}/login",
            data={"_token": token, "name": username, "password": password},
            timeout=self.timeout,
            allow_redirects=True,
        )
        page = self.s.get(f"{self.base}/sales", timeout=self.timeout)
        if "/login" in page.url or 'name="password"' in page.text:
            raise SyncError("Extra P refused the username/password.")
        self.csrf = self._token_from(page.text)
        m = re.search(r"all_permission\s*=\s*(\[[^\]]*\])", page.text)
        if m:
            try:
                self.perms = json.loads(m.group(1).replace("'", '"'))
            except ValueError:
                self.perms = []
        if not self.perms:
            # The list endpoints expect the permission list the page embeds;
            # an empty one can make them error rather than answer.
            self.perms = ["sales-index", "products-index"]

    def _headers(self) -> dict:
        h = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
        if self.csrf:
            h["X-CSRF-TOKEN"] = self.csrf
        xsrf = self.s.cookies.get("XSRF-TOKEN")
        if xsrf:
            from urllib.parse import unquote

            h["X-XSRF-TOKEN"] = unquote(xsrf)
        return h

    def _post(self, path: str, data: dict) -> dict:
        r = self.s.post(f"{self.base}/{path}", data=data, headers=self._headers(), timeout=self.timeout)
        if r.status_code in (401, 419):
            raise SyncError(f"Extra P session expired ({r.status_code}).")
        r.raise_for_status()
        return r.json()

    def _get_json(self, path: str):
        r = self.s.get(f"{self.base}/{path}", headers=self._headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ── reads ───────────────────────────────────────────────────────────
    def products(self) -> list[dict]:
        out, start = [], 0
        while True:
            d = self._post("products/product-data", {
                "all_permission[]": self.perms, "draw": 1, "start": start, "length": 500,
                "order[0][column]": 2, "order[0][dir]": "asc",
            })
            rows = d.get("data") or []
            for p in rows:
                try:
                    a = json.loads(p.get("product") or "[]")
                except ValueError:
                    a = []
                if len(a) < 8:
                    continue
                img = (a[18] if len(a) > 18 else "") or ""
                img = img.split(",")[0]
                out.append({
                    "id": p["id"], "name": a[1], "code": a[2], "category": a[4], "price": a[7],
                    "image_url": f"{self.base}/images/product/{img}" if img and img != PLACEHOLDER_IMG else "",
                })
            if len(rows) < 500:
                return out
            start += 500

    def all_sales(self) -> list[dict]:
        """Every sale row (the list view: id, totals, payment type, returns).
        Light — a few hundred bytes a row — and reading all of it is what lets
        a return made today on an old sale be noticed."""
        found, start = [], 0
        base = {
            "all_permission[]": self.perms, "starting_date": "2015-01-01", "ending_date": "2099-12-31",
            "warehouse_id": 0, "sale_status": 0, "payment_status": 0,
            "order[0][column]": 1, "order[0][dir]": "asc", "draw": 1, "length": 1000,
        }
        while True:
            d = self._post("sales/sale-data", {**base, "start": start})
            rows = d.get("data") or []
            found.extend(rows)
            if len(rows) < 1000:
                break
            start += 1000
        uniq = {int(r["id"]): r for r in found}
        return [uniq[k] for k in sorted(uniq)]

    def details(self, sale_id: int):
        return self._get_json(f"sales/product_sale/{sale_id}"), self._get_json(f"sales/getpayment/{sale_id}")


def build_dump(client: ExtraP, rows: list[dict]) -> dict:
    """The same JSON the full export produced, for just these sales."""
    products = client.products()
    codes = {p["code"] for p in products}
    by_code = {p["code"]: p for p in products}

    def fetch(r):
        for attempt in range(3):
            try:
                return r, *client.details(int(r["id"]))
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise SyncError(f"could not read sale {r['id']}: {last}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = list(pool.map(fetch, rows))

    sales = []
    for r, items, pay in fetched:
        at = None
        times = (pay[0] if pay else []) or []
        if times:
            m = re.match(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2}):(\d{2})", str(times[0]))
            if m:
                at = f"{m[3]}-{m[2]}-{m[1]}T{m[4]}:{m[5]}:{m[6]}"
        if not at:
            dd, mm, yy = _strip(r["date"]).split("/")
            at = f"{yy}-{mm}-{dd}T12:00:00"
        methods = (pay[3] if pay and len(pay) > 3 else []) or []
        amounts = (pay[2] if pay and len(pay) > 2 else []) or []
        card = sum(float(a or 0) for m_, a in zip(methods, amounts) if re.search(r"card|cheque", str(m_), re.I))
        cash = sum(float(a or 0) for m_, a in zip(methods, amounts) if not re.search(r"card|cheque", str(m_), re.I))
        is_card = card > cash if methods else bool(re.search(r"card|cheque", _strip(r.get("paymentType", "")), re.I))
        lines = []
        col = (lambda i: items[i] if i < len(items) else []) if isinstance(items, list) else (lambda i: items.get(str(i), []))
        names, qtys, subs = col(0), col(1), col(6)
        for k, raw in enumerate(names):
            m = re.match(r"^(.*)\[(.*)\]\s*$", str(raw))
            name = (m.group(1) if m else str(raw)).strip()
            code = (m.group(2) if m else "").strip()
            q = float(qtys[k] or 0)
            sub = float(subs[k] or 0)
            lines.append({
                "code": code,
                "name": by_code[code]["name"] if code in codes else name,
                "qty": q,
                "unit_price": str(round(sub / q, 2) if q else sub),
            })
        try:
            total = _num(json.loads(r["sale"])[16])
        except Exception:  # noqa: BLE001
            total = sum(float(subs[k] or 0) for k in range(len(subs)))
        grand = _num(r["grand_total"])
        sale = {
            "id": int(r["id"]), "ref": _strip(r.get("reference_no", "")), "at": at,
            "payment": "card" if is_card else "cash",
            "total": str(total), "grand_total": str(grand), "items": lines,
        }
        sales.append(sale)
        returned = _num(r.get("returned_amount", 0))
        if returned > 0:
            sales.append({
                **sale, "id": f"R{sale['id']}", "ref": f"return {sale['ref']}", "is_return": True,
                "total": str(returned), "grand_total": str(returned),
            })

    cats = []
    for p in products:
        if p["category"] and p["category"] not in cats:
            cats.append(p["category"])
    return {"source": "extrap", "categories": cats, "products": products, "sales": sales}
