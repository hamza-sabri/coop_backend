"""Arabic-aware search, the same rule the app's search boxes use (lib/search.ts):

    أ إ آ ٱ ا are one letter · ة ه · ى ي ئ · ؤ و
    harakat and tatweel (ـ) are skipped · ٠١٢… are 0 1 2…

A term with Arabic letters becomes a case-insensitive regex in which each of
those letters is a class ("[اأإآٱ]") and harakat may sit between any two
letters. A term without Arabic (a barcode, a phone) is searched exactly as
before, so numeric look-ups keep their plain icontains.
"""
from __future__ import annotations

import operator
import re
from functools import reduce

from django.db import models
from rest_framework.filters import SearchFilter

_HARAKAT = "ً-ٰٟـ"
_STRIP = re.compile(f"[{_HARAKAT}]")
_CLASSES = {
    "ا": "اأإآٱ",
    "ه": "هة",
    "ي": "يىئ",
    "و": "وؤ",
}
_FOLD = {c: k for k, v in _CLASSES.items() for c in v}
_ARABIC = re.compile("[؀-ۿ]")
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def latin_digits(s: str) -> str:
    return (s or "").translate(_DIGITS)


def has_arabic(s: str) -> bool:
    return bool(_ARABIC.search(s or ""))


def arabic_pattern(term: str) -> str:
    """A regex (Python re and PostgreSQL ARE alike) matching `term` anywhere,
    with the letter families above treated as one letter."""
    term = _STRIP.sub("", latin_digits(term)).strip()
    gap = f"[{_HARAKAT}]*"
    parts = []
    for ch in term:
        base = _FOLD.get(ch)
        if base:
            parts.append(f"[{_CLASSES[base]}]")
        elif ch.isspace():
            parts.append(r"\s+")
        else:
            parts.append(re.escape(ch))
    return gap.join(parts)


def text_q(field: str, term: str) -> models.Q:
    """`field` contains `term`, Arabic-aware. Use instead of field__icontains."""
    term = latin_digits(term)
    if has_arabic(term):
        return models.Q(**{f"{field}__iregex": arabic_pattern(term)})
    return models.Q(**{f"{field}__icontains": term})


class ArabicSearchFilter(SearchFilter):
    """DRF's ?search=, with Arabic terms matched the way people type them.
    Prefixed fields (=exact, ^startswith) are left exactly as they were."""

    def get_search_terms(self, request):
        return [latin_digits(t) for t in super().get_search_terms(request)]

    def filter_queryset(self, request, queryset, view):
        search_fields = self.get_search_fields(view, request)
        terms = self.get_search_terms(request)
        if not search_fields or not terms:
            return queryset
        lookups = [self.construct_search(str(f), queryset) for f in search_fields]

        def one(lookup, term):
            if lookup.endswith("__icontains") and has_arabic(term):
                return models.Q(**{lookup[: -len("__icontains")] + "__iregex": arabic_pattern(term)})
            return models.Q(**{lookup: term})

        base = queryset
        cond = reduce(operator.and_, (reduce(operator.or_, (one(lk, t) for lk in lookups)) for t in terms))
        queryset = queryset.filter(cond)
        if self.must_call_distinct(queryset, search_fields):
            queryset = base.filter(models.Exists(queryset.filter(pk=models.OuterRef("pk"))))
        return queryset
