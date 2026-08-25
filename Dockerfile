# =============================================================================
# Multi-stage Dockerfile — production image for Dokploy / VPS / any container host
# =============================================================================

# ── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Build deps for compiling wheels (psycopg2 etc.). Not carried to the runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps into an isolated virtualenv for a clean copy.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Runtime libs only (no build toolchain). curl powers the healthcheck.
#
# pg_dump/pg_restore power the nightly backup + restore drill. They must match
# the SERVER major version: Neon runs PostgreSQL 18, and pg_dump refuses to dump
# a server newer than itself ("server version mismatch"). Debian's own package
# is 17, so the client comes from the official PostgreSQL (PGDG) repo instead.
ARG PG_MAJOR=18
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl ca-certificates gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
         -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
         > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "postgresql-client-${PG_MAJOR}" \
    && rm -rf /var/lib/apt/lists/*

# Bring in the ready-built virtualenv from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# Non-root user (also makes Dokploy's Docker Terminal behave).
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser \
    && echo 'export PATH="/opt/venv/bin:$PATH"' >> /home/appuser/.bashrc

WORKDIR /app

COPY --chown=appuser:appuser . .

# Executable entrypoint + writable dirs for the non-root user.
RUN chmod +x entrypoint.sh \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R appuser:appuser /app/staticfiles /app/media

USER appuser

EXPOSE 8000

# DB-free liveness probe — used by Docker, Dokploy, and Traefik.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz/ || exit 1

# entrypoint.sh: migrate -> collectstatic -> gunicorn
ENTRYPOINT ["./entrypoint.sh"]
