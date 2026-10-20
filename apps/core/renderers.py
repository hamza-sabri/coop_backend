"""JSON out, with every stored picture turned into a URL a browser can load.

Pictures on B2 are kept in the database as "b2://<key>" and must be signed on
the way out (the bucket is private). Model serializers did that field by field;
hand-built responses — the reports' drink lists, the till's catalogue, profile
cards — each had to remember to, and some did not: the picture arrived as
"b2://…" and the browser showed a broken image.

So it is done once, here, for every JSON response: any string value starting
with "b2://" is replaced by its signed URL. Nothing a client sends is touched.

Picture fields (image, logo, avatar… by name) that hold an ordinary URL also
get their blurhash appended ("#bh=…", see apps/core/blurhash.py).
"""
import re

from rest_framework.renderers import JSONRenderer

from apps.core import blurhash
from apps.core.uploads import B2_SCHEME, resolve_stored_url

_PICTURE_KEY = re.compile(r"image|logo|avatar|photo|picture|thumb", re.I)


def _resolve(value, memo, key=""):
    if isinstance(value, str):
        if value.startswith(B2_SCHEME):
            if value not in memo:
                memo[value] = resolve_stored_url(value)
            return memo[value]
        if key and value and _PICTURE_KEY.search(key) and value.startswith(("http://", "https://", "/media/")):
            if value not in memo:
                memo[value] = blurhash.with_hash(value)
            return memo[value]
        return value
    if isinstance(value, dict):
        return {k: _resolve(v, memo, k if isinstance(k, str) else "") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_resolve(v, memo, key) for v in value]
    return value


class SignedMediaJSONRenderer(JSONRenderer):
    def render(self, data, accepted_media_type=None, renderer_context=None):
        try:
            data = _resolve(data, {})
        except Exception:  # noqa: BLE001 — never fail a response over a picture
            pass
        return super().render(data, accepted_media_type, renderer_context)
