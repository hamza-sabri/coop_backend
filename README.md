# 🚀 django-backend-template

![Django](https://img.shields.io/badge/Django-5.2-092E20?logo=django&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![DRF](https://img.shields.io/badge/DRF-3.16-A30000)
![Postgres](https://img.shields.io/badge/Postgres-Neon-4169E1?logo=postgresql&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-3da639)

**Stop scaffolding the same backend for the hundredth time.** 🥱

This is a batteries-included seed **and a tiny CLI workflow** for **Django + DRF**
backends — the boring-but-essential 80% (auth, docs, storage, caching, tasks, a
pretty admin) already wired up and tested — plus a little ✨ magic ✨: commands
that turn *"I need a Products API"* into a real, documented, paginated endpoint in
about 15 seconds.

> **One line** sets up your whole backend — database, Sentry, storage, the works —
> then it's running locally with live API docs and a themed admin. ☕

<p align="center">
  <img src="docs/setup-demo.gif" alt="One line configures the backend (database, Sentry, storage), migrates, and it's running locally with API docs and a themed admin panel" width="860">
</p>

### 🤔 Template or CLI tool?

**Both — on purpose.** Install the `django-backend` CLI once (`curl … | sh`),
then run `django-backend new <name>` to scaffold a project anywhere. Each project
ships `manage.py` commands that automate the boring parts: `init` (configure +
set up the DB), `newapp` (create an app), `setup_model` (generate a full CRUD API
from a model). The CLI *starts* projects; the in-project commands *build* them
out. 🧰

### 🧑‍💻 + 🤖 For developers *and* agents

This is built for both. **Developers** get a clean, documented, one-click starting
point. **AI agents** get a [`CLAUDE.md`](CLAUDE.md) playbook plus those same CLI
commands they can drive end-to-end — so "spin me up a backend with these models"
actually works, whether a human or an agent is at the keyboard.

Click **`Use this template`** on GitHub and your next project starts at the finish
line.

---

## 📖 Table of contents

1. [🚀 Getting started](#-getting-started)
2. [✨ Features](#-features)
3. [🧱 Tech stack](#-tech-stack)
4. [✅ Requirements](#-requirements)
5. [🗂️ Project structure](#️-project-structure)
6. [🔧 Configuration — environment keys](#-configuration--environment-keys)
7. [🔌 Optional integrations & graceful degradation](#-optional-integrations--graceful-degradation)
8. [📊 Defaults reference](#-defaults-reference)
9. [🛠️ Management commands](#️-management-commands)
10. [🪄 The scaffolding workflow](#-the-scaffolding-workflow)
11. [🔒 API security & authorization](#-api-security--authorization)
12. [👤 User model & profile image](#-user-model--profile-image)
13. [🔐 Authentication & API usage](#-authentication--api-usage)
14. [📁 File uploads & storage](#-file-uploads--storage)
15. [📚 API documentation](#-api-documentation)
16. [🚢 Deployment](#-deployment)
17. [📝 License](#-license)

---

## 🚀 Getting started

New here? This is everything you need to go from the template to a running,
documented backend.

### 1. Install the CLI (once)

```bash
curl -fsSL https://raw.githubusercontent.com/hamza-sabri/django-backend-template/main/install.sh | sh
```

Installs a **`django-backend`** command to `~/.local/bin` — no GitHub account, no
`gh`, no project folder created. Now you (and your agents) can spin up backends
from anywhere. Update anytime with `django-backend update`.

### 2. Create a project

```bash
django-backend new my-api      # scaffold a fresh backend into ./my-api
cd my-api
python -m venv .venv && source .venv/bin/activate   # virtualenv
pip install -r requirements.txt                     # dependencies

python manage.py init          # 🧙 the wizard: writes .env + prepares your database
python manage.py migrate       # create the schema
python manage.py createsuperuser
python manage.py runserver     # 🎉  →  http://127.0.0.1:8000/api/docs/
```

<details>
<summary><b>Prefer not to install anything? · <code>init</code> options</b></summary>

- **GitHub UI** — click **`Use this template`** → *Create a new repository*, then clone.
- **GitHub CLI** (if you have `gh`) — `gh repo create my-api --template hamza-sabri/django-backend-template --clone`
- **Plain git** — `git clone https://github.com/hamza-sabri/django-backend-template my-api`
- **`init` one-liner** — `python manage.py init -d "<DATABASE_URL>" -b <bucket> --sentry <dsn> --yes`
- **DIY config** — `cp .env.example .env` and edit it by hand.
</details>

Now go poke at it:

- 🧪 Swagger UI → <http://127.0.0.1:8000/api/docs/>
- 📘 ReDoc → <http://127.0.0.1:8000/api/docs/redoc/>
- 🎨 Admin → <http://127.0.0.1:8000/admin/>
- 🔑 Login → `POST http://127.0.0.1:8000/api/v1/auth/login/` with `{username, password}`

### 🤖 …or just ask an agent to do all of it

Because of [`CLAUDE.md`](CLAUDE.md), you don't have to run any of this yourself.
Point a coding agent (Claude & friends) at a fresh clone, describe what you want,
and it reads the playbook and drives the CLI for you.

**📋 Example prompt** — copy, paste, tweak:

> *"I want to build a backend for a store. Set up the right models, the
> migrations, the APIs and the admin — everything — using this template. Do it
> all for me."*

From that one line, the agent will:

1. 🆕 Run `django-backend new store` to scaffold the project.
2. 🧙 Run `python manage.py init` to write `.env` (auto-generating `SECRET_KEY`;
   local Postgres if you didn't give it a Neon URL).
3. 🏗️ Create the domain apps (`newapp store`, …) and write the models it
   inferred — `Product`, `Supplier`, `Prescription`, `Inventory` — each
   inheriting `TimeStampedModel`.
4. ⚙️ Scaffold each API with `setup_model store Product`, adding `-a`
   (admin-only writes) or `--history` (audit trail) where it fits.
5. 🗃️ Run `migrate` and hand you a running, documented backend at `/api/docs/`
   with a themed admin at `/admin/`.

You describe the domain; the agent does the typing. ⚡

### 🧙 The setup wizard (`init`)

`python manage.py init` bootstraps your `.env` so you never hand-edit config.

**Interactive** — prompts for each value with sensible defaults:

```bash
python manage.py init
```

**Flag-driven** — a one-liner for the README / CI / repeatable setups:

```bash
python manage.py init \
  -d "postgres://user:pass@host/db?sslmode=require" \
  -b my-b2-bucket \
  --domain api.example.com \
  --yes
```

`SECRET_KEY` is auto-generated when you don't pass one; any value you omit falls
back to a default (blank = feature off). Handy flags:

**All `init` flags** (or just run `python manage.py init --help`):

| Flag | Sets | Notes |
|---|---|---|
| `-d, --database` | `DATABASE_URL` | Postgres/Neon URL. Omit → local Postgres (see below). |
| `-s, --secret` | `SECRET_KEY` | Omit to auto-generate a strong key. |
| `-b, --bucket` | `B2_BUCKET_NAME` | Backblaze B2 bucket for uploads. |
| `--b2-key-id` | `B2_KEY_ID` | B2 key ID. |
| `--b2-app-key` | `B2_APPLICATION_KEY` | B2 application key. |
| `--b2-endpoint` | `B2_ENDPOINT_URL` | B2 S3 endpoint URL. |
| `--b2-region` | `B2_REGION` | B2 region. |
| `-r, --redis` | `REDIS_URL` | Blank = in-memory cache. |
| `--sentry` | `SENTRY_DSN` | Blank = error tracking off. |
| `--domain` | `DOMAIN` | For the Dokploy/Traefik deploy. |
| `--debug` | `DEBUG=True` | Development only. |
| `--db-name` | local DB name | Default: project folder name. |
| `--db-user` / `--db-password` | local DB creds | Default `postgres` / `postgres`. |
| `--db-host` / `--db-port` | local DB host / port | Default `localhost` / `5432`. |
| `--no-create-db` | — | Don't try to create the local database. |
| `-y, --yes` | — | Non-interactive (use flags + defaults, no prompts). |
| `--force` | — | Overwrite an existing `.env`. |
| `--migrate` | — | Run migrations right after writing `.env`. |

> 🐘 **No `-d`? You still get a database.** Skip `--database` and the wizard
> configures a **local Postgres** named after your project folder (e.g. `pharma`)
> and tries to create it for you — perfect for local dev. It needs a local
> Postgres running (defaults to user/password `postgres`; override with
> `--db-user`, `--db-password`, `--db-host`, `--db-port`, `--db-name`, or skip
> creation with `--no-create-db`). **For production, always pass `-d` with your
> Neon URL.** No sqlite sneaks in — this elephant never forgets, and it's still
> faster than your ORM's N+1 queries. 🐘💨

---

## ✨ Features

Everything below is already done, wired, and green. You just build your product on top. 💪

- 🧩 **Django 5 + DRF** with a clean `config/` + `apps/` layout and one env-driven `settings.py`.
- 👤 **Custom user model** (`accounts.User`) extending `AbstractUser` — with `phone`, `display_name`, an uploaded `avatar`, and a `profile_image_url` link, ready to reshape per project.
- 🔐 **JWT auth** (simplejwt): register, login, refresh, logout (blacklist), and `me` — with refresh-token rotation.
- 📚 **Auto-generated OpenAPI docs** (drf-spectacular): Swagger UI **and** ReDoc, always in sync with your code.
- 📄 **Pagination, filtering, search & ordering** on tap, DRF-wide.
- 🕓 **Timestamps & audit trail** — every model inherits `TimeStampedModel` (`created_at`/`updated_at`), with opt-in change history (`django-simple-history`) via `setup_model --history`.
- 🪄 **A code generator**: `newapp` + `setup_model` scaffold an app and a full CRUD API (serializer, viewset, router, admin, migrations) straight from your model.
- 🎨 **Jazzmin** admin — because the default admin deserves better.
- 🐘 **Neon / Postgres** via `DATABASE_URL`. Postgres always. No sqlite. Ever. 🚫
- 🔌 **Opt-in integrations** — Sentry, Backblaze B2, Redis, Celery — each lights up only when configured and stays out of your way otherwise.
- 🛡️ **Production hardening** (HSTS, secure cookies, SSL redirect) that switches on the moment `DEBUG=False`.
- ⚙️ WhiteNoise, CORS, gunicorn, and a ready `Procfile`.

## 🧱 Tech stack

| Area | Choice |
|---|---|
| 🧩 Framework | Django 5.2, DRF 3.16 |
| 🔐 Auth | JWT (simplejwt) |
| 📚 Docs | drf-spectacular (OpenAPI 3) |
| 🎨 Admin | Jazzmin |
| 🐘 DB | Postgres (Neon) via `dj-database-url` — required, no sqlite |
| 📁 Files | Local, or Backblaze B2 via django-storages (S3 API) |
| ⚡ Cache | Local-memory, or Redis via django-redis |
| 🔄 Tasks | Celery (optional) |
| 🚨 Errors | Sentry (optional) |
| 🕓 Audit | `TimeStampedModel` base + django-simple-history (opt-in) |
| 🗂️ Static | WhiteNoise |
| 🦄 Server | gunicorn |

## ✅ Requirements

- 🐍 Python 3.11+ (3.12 recommended)
- 📦 pip / venv
- 🐘 A Postgres/Neon `DATABASE_URL` — **required in every environment** (no sqlite fallback; a Neon branch is perfect for local dev)

---

## 🗂️ Project structure

```
config/
  settings.py     single, env-driven settings with conditional optional blocks
  urls.py         routes + auto-discovery of scaffolded app routers
  celery.py       optional Celery app (safe if Celery/Redis absent)
  wsgi.py/asgi.py
apps/
  core/
    models.py             TimeStampedModel base class
    pagination.py         StandardPagination (envelope + page_size control)
    permissions.py        reusable permission classes
    storage.py            storage helpers (work with local or B2)
    sentry.py             safe capture helpers (no-op when Sentry off)
    scaffold.py           helpers shared by the generators
    management/commands/
      newapp.py            create + register a new local app
      setup_model.py       generate serializer/viewset/router/admin from a model
  accounts/
    models.py             custom User
    serializers.py        register / login / user serializers
    views.py              JWT auth endpoints
    urls.py               /api/v1/auth/*
    admin.py
manage.py
requirements.txt
Procfile
.env.example
```

🪄 New apps you create with `newapp` land under `apps/` and auto-mount under
`/api/v1/` — you never touch `config/urls.py`.

---

## 🔧 Configuration — environment keys

All configuration is via environment variables, loaded from `.env` in
development. Copy `.env.example` → `.env` and fill in what you need. **Only
`SECRET_KEY` and `DATABASE_URL` are required** — everything else has a sane
default. 👍

### 🧠 Core

| Key | Required | Default | Notes |
|---|---|---|---|
| `SECRET_KEY` | ✅ **Yes** | insecure dev key | Long random string. `python -c "import secrets;print(secrets.token_urlsafe(50))"`. |
| `DEBUG` | No | `True` | Set `False` in production (enables security hardening). |
| `ALLOWED_HOSTS` | No | `*` (dev) | Comma-separated hostnames in prod, e.g. `api.example.com`. |
| `CSRF_TRUSTED_ORIGINS` | No | empty | Comma-separated, e.g. `https://app.example.com`. |
| `USE_X_FORWARDED_PROTO` | No | `True` in prod | Trust `X-Forwarded-Proto` behind a proxy/load balancer. |
| `LANGUAGE_CODE` | No | `en-us` | |
| `TIME_ZONE` | No | `UTC` | |
| `LOG_LEVEL` | No | `INFO` | Root logger level. |

### 🐘 Database

| Key | Required | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | ✅ **Yes** | — | Postgres/Neon URL, e.g. `postgres://user:pass@host/db?sslmode=require`. The app won't start without it. **No sqlite fallback.** |
| `DB_CONN_MAX_AGE` | No | `600` | Persistent connection lifetime (seconds). |
| `DB_SSL_REQUIRE` | No | `False` | Force SSL to the DB (Neon URLs usually carry `sslmode=require` already). |

> 🗄️ *A SQL query and a NoSQL database walk into a bar. The NoSQL one leaves
> immediately — it couldn't find a table. We'll stick with Postgres, thanks.*

### 🔐 Auth / JWT

| Key | Required | Default | Notes |
|---|---|---|---|
| `ACCESS_TOKEN_LIFETIME_MINUTES` | No | `60` | Access token TTL. |
| `REFRESH_TOKEN_LIFETIME_DAYS` | No | `30` | Refresh token TTL. Rotates; old tokens blacklisted. |

### 🌐 CORS

| Key | Required | Default | Notes |
|---|---|---|---|
| `CORS_ALLOW_ALL_ORIGINS` | No | `True` in dev | Set `False` in prod and use the allow-list below. |
| `CORS_ALLOWED_ORIGINS` | No | empty | Comma-separated origins, e.g. `https://app.example.com,http://localhost:5173`. |
| `CORS_ALLOW_CREDENTIALS` | No | `True` | |

### 📄 Pagination & throttling

| Key | Required | Default | Notes |
|---|---|---|---|
| `PAGE_SIZE` | No | `30` | Default results per page (clients override with `?page_size=`, capped at 100). |
| `THROTTLE_ANON` | No | `60/min` | Anonymous rate limit. |
| `THROTTLE_USER` | No | `240/min` | Authenticated rate limit. |

### 🏷️ API metadata (shown in Swagger)

| Key | Required | Default |
|---|---|---|
| `API_TITLE` | No | `Backend API` |
| `API_DESCRIPTION` | No | template blurb |
| `API_VERSION` | No | `1.0.0` |

### 🎨 Admin branding

| Key | Default |
|---|---|
| `ADMIN_SITE_TITLE` | `Backend Admin` |
| `ADMIN_SITE_HEADER` | `Backend Admin` |
| `ADMIN_SITE_BRAND` | `Backend` |
| `ADMIN_WELCOME` | `Welcome` |
| `ADMIN_COPYRIGHT` | empty |
| `ADMIN_THEME` | `flatly` |

### 🔌 Optional integration keys

**🚨 Sentry** (error tracking) — wakes up when `SENTRY_DSN` is set.

| Key | Default | How to obtain |
|---|---|---|
| `SENTRY_DSN` | empty | sentry.io → your project → Settings → Client Keys (DSN). |
| `SENTRY_ENVIRONMENT` | `production`/`development` | Logical env label. |
| `SENTRY_RELEASE` | empty | Optional release/version tag. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.1` | Performance-trace sampling (0–1). |
| `SENTRY_PROFILES_SAMPLE_RATE` | `0.1` | Profiling sampling (0–1). |
| `SENTRY_SEND_PII` | `False` | Send request/user PII to Sentry. |

**📁 Backblaze B2** (file storage, S3-compatible) — wakes up when **all four** of
`B2_KEY_ID`, `B2_APPLICATION_KEY`, `B2_BUCKET_NAME`, `B2_ENDPOINT_URL` are set.

| Key | Default | How to obtain |
|---|---|---|
| `B2_KEY_ID` | empty | B2 console → Application Keys → keyID. |
| `B2_APPLICATION_KEY` | empty | The secret shown when you create the key (once!). |
| `B2_BUCKET_NAME` | empty | B2 console → Buckets. |
| `B2_ENDPOINT_URL` | empty | Bucket's S3 endpoint, e.g. `https://s3.eu-central-003.backblazeb2.com`. |
| `B2_REGION` | empty | Region part of the endpoint, e.g. `eu-central-003`. |
| `B2_DEFAULT_ACL` | `public-read` | Set `private` for signed-URL-only access. |
| `B2_QUERYSTRING_AUTH` | `False` | `True` to sign every URL (needed for private buckets). |

**⚡ Redis** (cache) — wakes up when `REDIS_URL` is set.

| Key | Default | Notes |
|---|---|---|
| `REDIS_URL` | empty | e.g. `redis://default:pass@host:6379/0`. |
| `CACHE_TTL` | `86400` | Default cache entry lifetime (seconds). |

**🔄 Celery** (background tasks) — wakes up when a broker is available.

| Key | Default | Notes |
|---|---|---|
| `CELERY_BROKER_URL` | falls back to `REDIS_URL` | Broker/result backend. |
| `CELERY_TASK_ALWAYS_EAGER` | `False` | `True` runs tasks inline (handy in dev / no worker). |

### ➕ How to add keys

1. **🖥️ Local dev:** put them in `.env` (git-ignored). Restart `runserver`.
2. **☁️ Production:** set them as real environment variables in your host's
   dashboard (Railway/Render/Fly/etc.) — never commit `.env`.

---

## 🔌 Optional integrations & graceful degradation

The whole philosophy: **the backend runs even when nothing optional is set up**,
and each service turns on the instant you give it keys. No key, no crash. 😌

| Integration | Enable with | If not configured |
|---|---|---|
| 🚨 Sentry | `SENTRY_DSN` | Error tracking simply off. |
| 📁 Backblaze B2 | `B2_*` (all four) | Files saved to the local filesystem (`/media`). |
| ⚡ Redis | `REDIS_URL` | In-memory cache; a Redis outage never returns a 500 (`IGNORE_EXCEPTIONS`). |

> 🧠 *There are only two hard problems in computer science: cache invalidation,
> naming things, and off-by-one errors. This template quietly handles the first;
> you're on your own for naming that `utils.py`.*
| 🔄 Celery | `CELERY_BROKER_URL` or `REDIS_URL` | Tasks run eagerly / are skipped; nothing crashes. |

---

## 📊 Defaults reference

| Setting | Default |
|---|---|
| Access token lifetime | 60 minutes |
| Refresh token lifetime | 30 days (rotates, blacklists old) |
| Page size | 30 (max 100 via `?page_size=`) |
| Anon throttle | 60/min |
| User throttle | 240/min |
| Cache TTL | 86400 s (24 h) |
| DB connection max age | 600 s |
| Time zone | UTC |
| B2 ACL | public-read |
| Default storage | local filesystem (B2 when configured) |
| Default cache | local-memory (Redis when configured) |

📦 Pagination response envelope:

```json
{
  "count": 128,
  "total_pages": 5,
  "current_page": 1,
  "page_size": 30,
  "next": "http://.../api/v1/products/?page=2",
  "previous": null,
  "results": [ /* ... */ ]
}
```

---

## 🛠️ Management commands

### 🪄 Custom commands

#### `init` — configure the project (setup wizard)

```bash
python manage.py init                              # interactive
python manage.py init -d <url> -b <bucket> --yes   # flag-driven
```

Writes your `.env` (auto-generates `SECRET_KEY`). Full flag list in
[The setup wizard](#-the-setup-wizard-init).

#### `newapp` — create a new, fully-wired app

```bash
python manage.py newapp <name>
# example
python manage.py newapp catalog
```

Creates `apps/<name>/` (models, serializers, views, urls, admin — each with the
scaffold anchors), registers `apps.<name>` in `INSTALLED_APPS`, and — via URL
auto-discovery — mounts it under `/api/v1/`. Zero edits to `config/`. ✨

#### `setup_model` — generate a full CRUD API from a model

```bash
python manage.py setup_model <app> <Model> [-a] [--history] [--no-migrations]
# reads public, writes require auth (default)
python manage.py setup_model catalog CatalogItem
# reads public, writes admin-only
python manage.py setup_model catalog CatalogItem -a
# track full change history (who / what / when)
python manage.py setup_model catalog CatalogItem --history
```

Point it at a model you already wrote and it generates + wires:

- 🧾 a `ModelSerializer` (auto read-only `id`/timestamps),
- 🎛️ a `ModelViewSet` (auth + pagination + search + ordering),
- 🔗 a router registration → `/api/v1/<models>/`,
- 🧑‍💼 an admin registration with sensible `list_display`, `search_fields`, `list_filter`,

then runs `makemigrations` (skip with `--no-migrations`).

By default the API is public to read and requires authentication to write; add
`-a` for admin-only writes. See [API security & authorization](#-api-security--authorization).
Add `--history` to record every change (who/what/when) via django-simple-history.

**All `setup_model` flags** (or run `python manage.py setup_model --help`):

| Flag | Effect |
|---|---|
| `-a, --admin` | Writes require staff/admin (reads stay public). |
| `--history` | Track full change history (django-simple-history) + history-aware admin. |
| `--no-migrations` | Skip the automatic `makemigrations` step. |

> 💡 **Every command has `--help`.** Not sure what a command does or which flags
> it takes? `python manage.py <command> --help` lists them all — try
> `init --help`, `newapp --help`, or `setup_model --help`.

♻️ It's **idempotent**: existing classes are left untouched, so re-running is
safe. Generated code slots in at `# <scaffold:...>` anchors, so your
hand-written code lives happily in the same files.

### 🐍 Common Django / DRF commands

```bash
python manage.py migrate                 # apply migrations
python manage.py makemigrations          # create migrations after model changes
python manage.py createsuperuser         # make an admin user
python manage.py runserver               # dev server
python manage.py collectstatic --noinput # gather static for prod (WhiteNoise)
python manage.py spectacular --file schema.yml   # export the OpenAPI schema
python manage.py shell                   # interactive shell
python manage.py test                    # run tests
```

### 🔄 Background worker (only if Celery configured)

```bash
celery -A config worker -l info
```

---

## 🪄 The scaffolding workflow

The party trick — from *"I need a new resource"* to a live, documented API. 🎩

<p align="center">
  <img src="docs/scaffolder-demo.gif" alt="Write a model, run setup_model, and get a full documented CRUD API in seconds" width="840">
</p>

```bash
# 1. Create the app
python manage.py newapp catalog
```

```python
# 2. Write your model in apps/catalog/models.py
from django.db import models
from apps.core.models import TimeStampedModel


class CatalogItem(TimeStampedModel):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    in_stock = models.BooleanField(default=True)

    def __str__(self):
        return self.name
```

```bash
# 3. Generate the API + migrations, then apply
python manage.py setup_model catalog CatalogItem
python manage.py migrate
```

🎉 And just like that, with zero edits to `config/`:

- `GET/POST /api/v1/products/` and `GET/PUT/PATCH/DELETE /api/v1/products/{id}/`
- Pagination, `?search=`, and `?ordering=` support
- The model registered in the admin
- Full Swagger/ReDoc docs for the new endpoints

Need another model? Add it and run `setup_model catalog <Other>` again — the
generator appends, never overwrites. 🧩

### 🕓 Timestamps & change history

Every model should inherit **`TimeStampedModel`** (`apps/core/models.py`) so each
table gets `created_at` / `updated_at` for free — `newapp` scaffolds models that
way, and `setup_model` nudges you if you forget. Need a real audit trail (who
changed what, and when)? Add **`--history`** and the model gets
`django-simple-history` tracking plus a history-aware admin:

```bash
python manage.py setup_model catalog CatalogItem --history
```

The package ships in `requirements.txt` by default, but nothing is tracked until
you opt in — because keeping history on *everything* is how you end up explaining
a 40 GB audit table to your DBA. 📼

---

## 🔒 API security & authorization

Generated endpoints ship with safe, explicit defaults — so you never *accidentally*
expose writes, and you always know exactly who can do what.

| Operation | Default | With `setup_model -a` (`--admin`) |
|---|---|---|
| **Read** (`GET` list / detail) | 🌍 Public — no token | 🌍 Public — no token |
| **Write** (`POST` / `PUT` / `PATCH` / `DELETE`) | 🔑 Any authenticated user | 🛡️ Admin (staff) only |

- **Reads are public by default.** List/retrieve need no auth. This is deliberate
  and worth telling your team — if a resource shouldn't be world-readable, tighten
  its viewset's `permission_classes`.
- **Writes always require authentication.** Create/update/delete reject anonymous
  requests out of the box.
- **`-a` / `--admin`** restricts writes to staff users (uses `IsAdminOrReadOnly`):

  ```bash
  python manage.py setup_model catalog CatalogItem -a
  ```

- The **Django admin panel** (`/admin/`) is always full-access for staff users,
  independent of the API rules above.

Need a different policy (fully public, fully locked, per-object ownership)? Edit the
generated `permission_classes`, or reuse the ready-made classes in
`apps/core/permissions.py`: `IsAdminOrReadOnly`, `IsOwnerOrReadOnly`,
`IsSelfOrAdmin`.

---

## 👤 User model & profile image

The custom user lives in `apps/accounts/models.py`, extends `AbstractUser`
(username login), and ships with:

| Field | Type | Purpose |
|---|---|---|
| `phone` | CharField | Optional phone number (indexed). |
| `display_name` | CharField | Friendly name. |
| `avatar` | ImageField | 📤 **Uploaded** profile image (B2 when configured, else local). |
| `profile_image_url` | URLField | 🔗 **External** profile image link (OAuth/CDN avatar). |
| `updated_at` | DateTime | Auto-updated timestamp. |

### 🖼️ Setting the profile image

Two independent options — use either or both:

- 🔗 **External link** — set `profile_image_url` (a string URL). Great when the
  image already lives on a CDN or comes from a social login.
- 📤 **Uploaded file** — send a file to `avatar` (multipart). Stored locally by
  default, or on Backblaze B2 once the `B2_*` keys are set.

Both are on the user serializer, so they're available on
`GET/PATCH /api/v1/auth/me/` and returned in the login response.

### 🧬 Customizing the user

Edit `apps/accounts/models.py` freely — add columns, add a custom manager, or
switch `USERNAME_FIELD` to `email`/`phone`. `AUTH_USER_MODEL` already points
here, so changes are cheap. Then:
`python manage.py makemigrations accounts && python manage.py migrate`.

---

## 🔐 Authentication & API usage

All auth endpoints live under `/api/v1/auth/`.

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/v1/auth/register/` | Create an account |
| POST | `/api/v1/auth/login/` | Get access + refresh tokens (and the user) |
| POST | `/api/v1/auth/refresh/` | Exchange a refresh token for a new access token |
| POST | `/api/v1/auth/logout/` | Blacklist a refresh token |
| GET/PATCH | `/api/v1/auth/me/` | Read or update the current user |

### 📝 Register

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/register/ \
  -H "Content-Type: application/json" \
  -d '{"username":"hamza","email":"h@example.com","password":"supersecret",
       "display_name":"Hamza","profile_image_url":"https://cdn.example.com/me.png"}'
```

### 🔑 Login

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"username":"hamza","password":"supersecret"}'
# → {"access":"<jwt>","refresh":"<jwt>","user":{...}}
```

### 🙋 Authenticated request

```bash
curl http://127.0.0.1:8000/api/v1/auth/me/ \
  -H "Authorization: Bearer <access>"
```

### 🔗 Update the profile image (external link)

```bash
curl -X PATCH http://127.0.0.1:8000/api/v1/auth/me/ \
  -H "Authorization: Bearer <access>" \
  -H "Content-Type: application/json" \
  -d '{"profile_image_url":"https://cdn.example.com/new.png"}'
```

### 📤 Upload an avatar file

```bash
curl -X PATCH http://127.0.0.1:8000/api/v1/auth/me/ \
  -H "Authorization: Bearer <access>" \
  -F "avatar=@/path/to/photo.jpg"
```

### 🔄 Refresh / logout

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/refresh/ \
  -H "Content-Type: application/json" -d '{"refresh":"<refresh>"}'

curl -X POST http://127.0.0.1:8000/api/v1/auth/logout/ \
  -H "Authorization: Bearer <access>" \
  -H "Content-Type: application/json" -d '{"refresh":"<refresh>"}'
```

---

## 📁 File uploads & storage

`avatar` and any `ImageField`/`FileField` on your scaffolded models use Django's
default storage:

- 🖥️ **No B2 configured** → files land in `/media`, served locally in dev.
- ☁️ **B2 configured** (`B2_*` keys) → files upload transparently to Backblaze B2
  over the S3 API, with clean public URLs (`AWS_QUERYSTRING_AUTH` off by default).

Helpers in `apps/core/storage.py` (`save_bytes`, `file_url`, `delete_file`,
`presigned_url`) work against whichever backend is active — your code doesn't
change between local and B2. 🎯

---

## 📚 API documentation

Powered by drf-spectacular and always in sync with your code:

- 🧪 Swagger UI → `/api/docs/`
- 📘 ReDoc → `/api/docs/redoc/`
- 🧾 Raw OpenAPI schema → `/api/schema/`

Every scaffolded endpoint is documented automatically. Export the schema for
client codegen with `python manage.py spectacular --file schema.yml`.

---

## 🚢 Deployment

Containerized and platform-agnostic. The primary target is a **Hostinger VPS
running Dokploy** (Traefik reverse proxy + automatic HTTPS), but the same image
runs on Railway, Render, Fly, Heroku, AWS, Azure, Cloud Run, or plain Docker. 🐳

The image is a **multi-stage, non-root** build. On startup `entrypoint.sh`
migrates → collects static → launches gunicorn, and the container exposes a
DB-free liveness probe at **`/healthz/`**.

### 🖥️ Dokploy on a VPS (the main path)

The `Dockerfile` + `docker-compose.yml` are wired for Dokploy's Traefik proxy —
routing and Let's Encrypt TLS are handled by the labels in the compose file.

1. In **Dokploy**, create an app from this repo (Compose type).
2. Set environment variables in the Dokploy dashboard:
   - **Required:** `SECRET_KEY`, `DATABASE_URL` (Neon), `DEBUG=False`, and
     `DOMAIN` (e.g. `api.example.com`).
   - Optional: `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `SENTRY_DSN`, `B2_*`,
     `REDIS_URL`, `GUNICORN_WORKERS`, …
3. Point your domain's DNS at the VPS, then **Deploy**.

Traefik reads the `traefik.*` labels in `docker-compose.yml`, routes `${DOMAIN}`
to the container on port 8000, issues a Let's Encrypt cert automatically, and
health-checks `/healthz/`. Postgres stays **external** (Neon via `DATABASE_URL`)
— there's no `db` service. Uncomment the Redis + Celery worker services in the
compose file only if you use them.

### 🐳 Plain Docker (any host)

```bash
docker build -t my-backend .
docker run --env-file .env -p 8000:8000 my-backend
```

### 🚉 Railway / Render / Fly / Heroku

They auto-detect the Dockerfile. For buildpack-style deploys, the included
**`Procfile`** provides `web` (gunicorn), `release` (migrate), and `worker`
(celery, if used).

### 🔑 Required env everywhere

- **Required:** `SECRET_KEY`, `DATABASE_URL` (your Neon/Postgres URL), and
  `DEBUG=False` (add `DOMAIN` for the Dokploy/Traefik path).
- **Recommended:** `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, plus any optional
  integration keys (`SENTRY_DSN`, `B2_*`, `REDIS_URL`, …).
- 🛡️ With `DEBUG=False`, security hardening (SSL redirect, HSTS, secure cookies)
  turns on automatically.

---

## 📝 License

Released under the [MIT License](LICENSE) © 2026 Hamza Sabri. Use it, fork it,
ship it, make it yours. ❤️

---

<div align="center">

**Built to be reused.** ♻️ Star it if it saved you an afternoon. ⭐

*A SQL query walks into a bar, spots two tables, and asks: “Mind if I JOIN you?” 🍻*

</div>
# Alrahmah
