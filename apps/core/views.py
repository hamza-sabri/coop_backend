from django.http import JsonResponse


def healthz(request):
    """Liveness probe.

    Returns 200 without touching the database or any external service, so
    container/orchestrator/Traefik health checks reflect "process is up" rather
    than "everything downstream is healthy."
    """
    return JsonResponse({"status": "ok"})
