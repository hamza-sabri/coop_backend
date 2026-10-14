"""A sensible default icon for a category, from its name.

The keys are the frontend's set (components/pos/category-circles.tsx); the
owner can always pick another one from the menu page.
"""
from __future__ import annotations

import re

# First match wins — the more specific phrases come before the broad ones.
RULES: list[tuple[str, str]] = [
    (r"ايس ?تي|ice ?tea|iced ?tea", "cup-soda"),
    (r"موهيتو|mojito|كوكتيل|cocktail|موكتيل", "martini"),
    (r"ميلك ?شيك|milk ?shake|شيك", "ice-cream"),
    (r"ايس ?كريم|ice ?cream|بوظه|جيلاتو|gelato", "ice-cream-bowl"),
    (r"سموذي|smoothie|عصير|عصاير|عصائر|juice|ليموناد|lemonade", "citrus"),
    (r"فواكه|فاكهه|fruit", "cherry"),
    (r"هيلثي|healthy|صحي|دايت|diet|كيتو|keto", "heart-pulse"),
    (r"بروتين|protein", "dumbbell"),
    (r"مكس|mix|خاص|special|سجنتشر|signature", "sparkles"),
    (r"شاي|tea|اعشاب|herbal|ماتشا|matcha", "leaf"),
    (r"غازي|soda|مشروبات غازيه", "cup-soda"),
    (r"مياه|ماء|water", "glass-water"),
    (r"بارد|cold|iced|ايس|فرابيه|frappe", "snowflake"),
    (r"قهوه|كوفي|coffee|اسبريسو|espresso|ساخن|hot|لاتيه|latte", "coffee"),
    (r"كيك|cake|تشيز", "cake-slice"),
    (r"سويت|sweet|حلو|حلويات|dessert|حلا", "cake-slice"),
    (r"كوكيز|cookie|بسكوت", "cookie"),
    (r"دونات|donut", "donut"),
    (r"شوكولا|chocolate|سكاكر|candy", "candy"),
    (r"كرواسون|croissant|معجنات|pastr|مخبوزات|bakery", "croissant"),
    (r"ساندويش|سندويش|sandwich|برجر|burger", "sandwich"),
    (r"بيتزا|pizza", "pizza"),
    (r"سلطه|salad", "salad"),
    (r"شوربه|soup", "soup"),
    (r"سناك|snack|مقرمشات|فشار|popcorn", "popcorn"),
    (r"حليب|milk", "milk"),
    (r"اكل|طعام|food|وجبات|meal", "utensils"),
]


def _fold(s: str) -> str:
    s = (s or "").lower()
    s = re.sub("[ً-ْـ]", "", s)  # harakat, tatweel
    return s.translate(str.maketrans("أإآةى", "اااهي"))


def guess(name: str) -> str:
    n = _fold(name)
    for pattern, key in RULES:
        if re.search(_fold(pattern), n):
            return key
    return "coffee"
