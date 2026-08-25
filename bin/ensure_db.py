#!/usr/bin/env python
"""Make sure the database in DATABASE_URL exists and we can log into it.

Run before migrate. Uses psycopg2 (already a backend dependency) rather than
psql, because a Postgres installed by Postgres.app or Docker often has no psql
on PATH and failing on that would be a silly reason to stop.

If the app role or database is missing we create them, connecting as whichever
local superuser will have us — on macOS that is usually your own username.

Also checks Redis, and says so plainly when it is not there: the template
activates Redis the moment REDIS_URL is set, so an unreachable Redis is the
kind of thing that half-works and wastes an afternoon.
"""
import getpass
import io
import os
import socket
import sys
from urllib.parse import unquote, urlparse

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

DIM, GOLD, GREEN, RED, OFF = "\033[2m", "\033[33m", "\033[32m", "\033[31m", "\033[0m"


def say(msg):
    print(f"{GOLD}▸{OFF} {msg}")


def parse(url):
    u = urlparse(url)
    return {
        "host": u.hostname or "localhost",
        "port": u.port or 5432,
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "dbname": (u.path or "/").lstrip("/") or "postgres",
    }


def can_connect(**kw):
    try:
        psycopg2.connect(connect_timeout=4, **kw).close()
        return True, None
    except psycopg2.Error as e:
        return False, e


def admin_connection(host, port):
    """Find a local role that may create roles and databases.

    Unix sockets first. On macOS a Homebrew or Postgres.app server almost
    always trusts your own account over the socket, while the TCP listener
    wants a password nobody has set — so trying TCP first is how this fails
    for no reason.
    """
    try:
        me = getpass.getuser()
    except Exception:
        me = ""
    users, seen = [], set()
    for u in (os.getenv("PGUSER"), os.getenv("USER"), me, "postgres"):
        if u and u not in seen:
            seen.add(u); users.append(u)
    sockets = [None, "/tmp", "/var/run/postgresql", "/private/tmp"]
    attempts = []
    for sock in sockets:
        if sock and not os.path.isdir(sock):
            continue
        for user in users:
            for dbname in dict.fromkeys(("postgres", user)):
                kw = {"user": user, "dbname": dbname, "port": port, "connect_timeout": 4}
                if sock:
                    kw["host"] = sock
                try:
                    c = psycopg2.connect(**kw)
                    c.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                    return c, f"{user} via {sock or 'socket'}"
                except psycopg2.Error as e:
                    attempts.append(f"{user}@{sock or 'socket'}/{dbname}: "
                                    f"{str(e).strip().splitlines()[0]}")
    # then TCP
    for user in users:
        for dbname in dict.fromkeys(("postgres", user)):
            try:
                c = psycopg2.connect(host=host, port=port, user=user,
                                     dbname=dbname, connect_timeout=4)
                c.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                return c, f"{user}@{host}"
            except psycopg2.Error as e:
                attempts.append(f"{user}@{host}/{dbname}: "
                                f"{str(e).strip().splitlines()[0]}")
    return None, attempts


def ensure_postgres(url):
    cfg = parse(url)
    ok, err = can_connect(**cfg)
    if ok:
        say(f"postgres ok {DIM}({cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['dbname']}){OFF}")
        return True

    # Is the server even there? A missing role answers; a missing server does not.
    msg = str(err).lower()
    unreachable = ("connection refused", "could not connect", "no such file or directory",
                   "timeout expired", "could not translate host name", "is the server running")
    if any(m in msg for m in unreachable):
        print(f"{RED}✗{OFF} nothing is answering on {cfg['host']}:{cfg['port']}.")
        print(f"  {DIM}{str(err).strip().splitlines()[0]}{OFF}")
        print("  Start Docker Desktop and re-run ./dev.sh, or start your local Postgres:")
        print(f"    {DIM}brew services start postgresql@16{OFF}")
        return False

    say(f"setting up the database {DIM}(role/db missing){OFF}")
    conn, admin = admin_connection(cfg["host"], cfg["port"])
    if conn is None:
        print(f"{RED}✗{OFF} Postgres is running but no local superuser would let me in.")
        for line in (admin or [])[:4]:
            print(f"    {DIM}tried {line}{OFF}")
        print("  Create the role and database yourself, then re-run:")
        print(f"    createuser -s {cfg['user']}")
        print(f"    createdb -O {cfg['user']} {cfg['dbname']}")
        print(f"    psql -c \"ALTER ROLE {cfg['user']} PASSWORD '{cfg['password']}'\"")
        return False

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (cfg["user"],))
        if not cur.fetchone():
            cur.execute(
                f'CREATE ROLE "{cfg["user"]}" LOGIN CREATEDB PASSWORD %s', (cfg["password"],)
            )
            say(f'created role "{cfg["user"]}"')
        else:
            cur.execute(f'ALTER ROLE "{cfg["user"]}" LOGIN PASSWORD %s', (cfg["password"],))
            say(f'reset the password on existing role "{cfg["user"]}"')

        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (cfg["dbname"],))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{cfg["dbname"]}" OWNER "{cfg["user"]}"')
            say(f'created database "{cfg["dbname"]}"')
    conn.close()

    ok, err = can_connect(**cfg)
    if not ok:
        print(f"{RED}✗{OFF} still cannot connect: {err}")
        return False
    say(f"postgres ready {DIM}(set up as {admin}){OFF}")
    return True


def ensure_redis(url, env_path):
    """Reachable or not — but never silently half-on.

    The template switches Redis on the moment REDIS_URL is set, so a set-but-
    unreachable Redis fails at the first cache call instead of at startup.
    We comment the line out when nothing answers and put it back when it does,
    so the same .env works with or without Docker up.
    """
    if not url:
        # already disabled — put it back if Redis has since appeared
        commented = read_env(env_path, "REDIS_URL", commented=True)
        if commented:
            s_ = io.open(env_path, encoding="utf-8").read()
            u_ = urlparse(commented)
            probe = socket.socket(); probe.settimeout(1.5)
            try:
                probe.connect((u_.hostname or "localhost", u_.port or 6379))
                io.open(env_path, "w", encoding="utf-8").write(
                    s_.replace(DISABLED_PREFIX + "REDIS_URL=", "REDIS_URL=")
                      .replace(DISABLED_PREFIX + "CELERY_BROKER_URL=", "CELERY_BROKER_URL="))
                say("redis is back — re-enabled in .env")
                return commented
            except OSError:
                pass
            finally:
                probe.close()
        return None
    u = urlparse(url)
    host, port = u.hostname or "localhost", u.port or 6379
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((host, port))
        # Something answering on 6379 is not necessarily Redis. Ask it.
        s.sendall(b"PING\r\n")
        reply = s.recv(64)
        if not reply.startswith(b"+PONG"):
            print(f"{GOLD}▸{OFF} something is on {host}:{port} but it is not Redis "
                  f"{DIM}(said {reply[:24]!r}) — disabling{OFF}")
            raise OSError("not redis")
        say(f"redis ok {DIM}({host}:{port}){OFF}")
        return url
    except OSError:
        print(f"{GOLD}▸{OFF} no redis on {host}:{port} — "
              f"{DIM}disabled in .env, using the in-memory cache{OFF}")
        txt = io.open(env_path, encoding="utf-8").read()
        io.open(env_path, "w", encoding="utf-8").write(
            txt.replace("\nREDIS_URL=", "\n" + DISABLED_PREFIX + "REDIS_URL=")
               .replace("\nCELERY_BROKER_URL=", "\n" + DISABLED_PREFIX + "CELERY_BROKER_URL="))
        return ""
    finally:
        s.close()


DISABLED_PREFIX = "# auto-disabled by dev.sh (nothing listening) # "


def read_env(path, key, commented=False):
    """Read KEY from a .env without needing it exported first."""
    prefix = (DISABLED_PREFIX if commented else "") + key + "="
    try:
        for line in io.open(path, encoding="utf-8"):
            line = line.rstrip("\n")
            if line.startswith(prefix):
                return line[len(prefix):].strip()
    except FileNotFoundError:
        pass
    return ""


def drop_database(url):
    """Used by `dev.sh --fresh`. Works with or without Docker."""
    cfg = parse(url)
    conn, _ = admin_connection(cfg["host"], cfg["port"])
    if conn is None:
        print(f"{RED}✗{OFF} cannot drop {cfg['dbname']} — no superuser connection")
        return False
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (cfg["dbname"],))
        cur.execute(f'DROP DATABASE IF EXISTS "{cfg["dbname"]}"')
        cur.execute(f'CREATE DATABASE "{cfg["dbname"]}" OWNER "{cfg["user"]}"')
    conn.close()
    say(f'dropped and recreated "{cfg["dbname"]}"')
    return True


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")

    db = read_env(env_path, "DATABASE_URL") or os.getenv("DATABASE_URL", "")
    if not db:
        print(f"{RED}✗{OFF} DATABASE_URL is not set — check coop-backend/.env")
        sys.exit(1)
    if not ensure_postgres(db):
        sys.exit(1)
    if "--drop" in sys.argv and not drop_database(db):
        sys.exit(1)
    ensure_redis(read_env(env_path, "REDIS_URL"), env_path)
    print(f"{GREEN}✓{OFF} services ready")
