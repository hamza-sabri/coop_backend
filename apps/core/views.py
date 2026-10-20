from django.http import JsonResponse


def healthz(request):
    """Liveness probe.

    Returns 200 without touching the database or any external service, so
    container/orchestrator/Traefik health checks reflect "process is up" rather
    than "everything downstream is healthy."
    """
    return JsonResponse({"status": "ok"})


def small_image(request, token: str):
    """GET /api/v1/img/<token>.webp — a picture's small copy (apps/core/blurhash.py).

    The address never changes for a given picture (a new upload gets a new
    token), so browsers may keep it for a year without asking again. Public
    like the signed links it replaces; the token is 128 random bits.
    """
    from django.http import Http404, HttpResponse

    from apps.core.models import ImageHash

    data = ImageHash.objects.filter(token=token).values_list("small", flat=True).first()
    if not data:
        raise Http404
    resp = HttpResponse(bytes(data), content_type="image/webp")
    resp["Cache-Control"] = "public, max-age=31536000, immutable"
    resp["Cross-Origin-Resource-Policy"] = "cross-origin"
    return resp
