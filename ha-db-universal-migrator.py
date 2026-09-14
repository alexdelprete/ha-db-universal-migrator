#!/usr/bin/env python3
"""
Home Assistant recorder migration between database engines
Version 1.0.3 (2026-09-14)
  source:  SQLite | MariaDB | MySQL | PostgreSQL   (detected from HA's configuration)
  target:  SQLite | MariaDB | MySQL | PostgreSQL   (DST_TYPE below)

The source is detected from the stopped HA's own configuration: the script
reads recorder.db_url from configuration.yaml (resolving !secret and packages
with HA's YAML loader) and follows it. No db_url means the recorder default
SQLite file. SRC_DB_URL overrides detection.

The target schema is generated from the Home Assistant models installed in the
interpreter running this script (Base.metadata.create_all), so it is always the
schema of the HA version that last wrote the source database. No DDL is
shipped, no HA version is pinned: the script reads the source's schema_version
and refuses to run unless the installed models report the same number.

Run it with HA's own Python, with HA STOPPED for the whole duration:

  Container:  docker compose run --rm --entrypoint python3 homeassistant /config/ha-db-universal-migrator.py
  Core/venv:  source /srv/homeassistant/bin/activate && python3 ha-db-universal-migrator.py
  Supervised: docker exec -it homeassistant python3 /config/ha-db-universal-migrator.py

Flags:  --dry-run   run pre-flight only, write nothing
        --yes       skip the confirmation prompt
"""

# ---------------------------------------------------------------------------
# CONFIGURATION — edit this block only
# Every value can be overridden by an environment variable of the same name.
# ---------------------------------------------------------------------------

# --- Source: detected from the stopped Home Assistant's configuration --------

# Directory containing configuration.yaml (inside the container: /config).
# The script reads recorder.db_url from it, resolving !secret and packages
# with HA's own YAML loader. No db_url there means the default SQLite file.
HA_CONFIG_DIR = "/config"
# Optional: SQLAlchemy URL of the source, e.g. mysql://user:pw@host/db or
# sqlite:////config/home-assistant_v2.db. Empty = detect from HA_CONFIG_DIR
SRC_DB_URL   = ""

# --- Target: the database the recorder will switch to ------------------------

# Target engine: "postgresql", "mariadb", "mysql" or "sqlite"
DST_TYPE     = "postgresql"
# SQLite target only: path of the new recorder file (must not exist yet)
DST_SQLITE_PATH = "/config/home-assistant_v2.new.db"
# Hostname or IP of the target server (ignored for sqlite)
DST_HOST     = "10.0.0.xx"
# Target server port; 0 = engine default (5432 / 3306)
DST_PORT     = 0
# Name of the recorder database to populate; must be empty or not yet exist
DST_DB       = "homeassistant"
# Account the recorder will connect as; becomes owner of all tables
DST_USER     = "homeassistant"
# Password for DST_USER (empty = read from env, abort if still empty)
DST_PASSWORD = ""
# Optional: full SQLAlchemy URL of the target; overrides DST_TYPE/HOST/PORT/DB/USER/PASSWORD
DST_DB_URL   = ""

# --- Target bootstrap (optional) ---------------------------------------------

# Admin account able to create databases and users ("postgres" / "root");
# empty = skip bootstrap, expect DST_USER and DST_DB to exist, print the SQL
DST_ADMIN_USER     = ""
# Password for DST_ADMIN_USER; used only to create DST_USER and DST_DB
DST_ADMIN_PASSWORD = ""

# --- Copy behaviour ----------------------------------------------------------

# Rows fetched from the source per batch (one primary-key range). 0 = auto:
# sized per table from the measured row width and this process's memory
# limit (cgroup or RAM). Set a number to force it
BATCH_ROWS   = 0
# Rows per INSERT statement for MariaDB/MySQL/SQLite targets. 0 = auto: sized
# per table so each statement stays well under the server's max_allowed_packet.
# PostgreSQL uses COPY and ignores this
INSERT_ROWS  = 0
# Full log of every step, row counts and timings; appended, never truncated
LOG_FILE     = "/config/ha-db-universal-migrator.log"

# PostgreSQL target only: maintenance_work_mem for the index/FK rebuild phase,
# set per session (no server restart). Empty = keep the server's setting.
# Size it to the target host's free RAM: e.g. "256MB" on a 1 GB box, "1GB" on 4 GB+
PG_MAINTENANCE_WORK_MEM = ""

# --- Safety ------------------------------------------------------------------

# Abort if the target already contains any table (prevents loading over data)
REQUIRE_EMPTY_TARGET   = True
# Abort if the source shows other connections / an active writer (HA still up)
REQUIRE_QUIET_SOURCE   = True
# Skip the interactive confirmation after pre-flight (same as --yes)
ASSUME_YES             = False

# ---------------------------------------------------------------------------
# No user-serviceable parts below this line
# ---------------------------------------------------------------------------

__version__ = "1.0.3"

import argparse
import datetime as _dt
import io
import math
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path

# ---------------------------------------------------------------------------
# Environment overrides and CLI
# ---------------------------------------------------------------------------

_CONFIG_NAMES = [
    "HA_CONFIG_DIR", "SRC_DB_URL",
    "DST_TYPE", "DST_SQLITE_PATH", "DST_HOST", "DST_PORT", "DST_DB", "DST_USER",
    "DST_PASSWORD", "DST_DB_URL", "DST_ADMIN_USER", "DST_ADMIN_PASSWORD",
    "BATCH_ROWS", "INSERT_ROWS", "LOG_FILE", "PG_MAINTENANCE_WORK_MEM",
    "REQUIRE_EMPTY_TARGET", "REQUIRE_QUIET_SOURCE", "ASSUME_YES",
]
_FROM_ENV = []

def _apply_env_overrides():
    g = globals()
    for name in _CONFIG_NAMES:
        if name not in os.environ:
            continue
        raw = os.environ[name]
        default = g[name]
        if isinstance(default, bool):
            g[name] = raw.strip().lower() in ("1", "true", "yes", "y", "on")
        elif isinstance(default, int):
            g[name] = int(raw)
        else:
            g[name] = raw
        _FROM_ENV.append(name)

_apply_env_overrides()

_ap = argparse.ArgumentParser(description="Migrate the HA recorder database between engines.")
_ap.add_argument("--dry-run", action="store_true", help="pre-flight only, write nothing")
_ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
_ARGS = _ap.parse_args()
if _ARGS.yes:
    ASSUME_YES = True

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_fh = None

def log(msg=""):
    global _log_fh
    line = msg if msg == "" else f"{_dt.datetime.now().strftime('%H:%M:%S')}  {msg}"
    print(msg)
    sys.stdout.flush()
    if _log_fh is None and LOG_FILE:
        try:
            _log_fh = open(LOG_FILE, "a", encoding="utf-8")
            _log_fh.write(f"\n===== {_dt.datetime.now().isoformat(timespec='seconds')} =====\n")
        except OSError as e:
            print(f"(log file {LOG_FILE} not writable: {e}; logging to stdout only)")
            _log_fh = False
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()

def fail(msg, code=1):
    log(f"ABORT: {msg}")
    sys.exit(code)

# ---------------------------------------------------------------------------
# Phase 0a: interpreter and imports
# ---------------------------------------------------------------------------

log(f"=== ha-db-universal-migrator.py {__version__} — pre-flight ===")
log(f"Interpreter      {sys.executable}  ({sys.version.split()[0]})")

try:
    import homeassistant
    from homeassistant.components.recorder.db_schema import Base, SCHEMA_VERSION
except ImportError as e:
    log("homeassistant    NOT IMPORTABLE")
    log("")
    log("This script must run with the Python that has Home Assistant installed.")
    log("  Container:   docker compose run --rm --entrypoint python3 homeassistant /config/ha-db-universal-migrator.py")
    log("  Core/venv:   source /srv/homeassistant/bin/activate && python3 ha-db-universal-migrator.py")
    log("  Supervised:  docker exec -it homeassistant python3 /config/ha-db-universal-migrator.py")
    log("")
    log(f"Nothing was checked or written. ({e})")
    sys.exit(2)

try:
    from homeassistant.const import __version__ as HA_VERSION
except ImportError:
    HA_VERSION = getattr(homeassistant, "__version__", "unknown")
log(f"homeassistant    {HA_VERSION}  {homeassistant.__file__}")
log(f"SCHEMA_VERSION   {SCHEMA_VERSION}  (from installed models)")

import sqlite3
import sqlalchemy
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.schema import CreateIndex, DropIndex
from sqlalchemy.types import Integer

log(f"sqlalchemy       {sqlalchemy.__version__}")

try:
    import psycopg2
    _have_psycopg2 = True
except ImportError:
    _have_psycopg2 = False
try:
    import MySQLdb  # noqa: F401
    _have_mysqldb = True
except ImportError:
    _have_mysqldb = False
try:
    import pymysql  # noqa: F401
    _have_pymysql = True
except ImportError:
    _have_pymysql = False

def _modver(mod):
    v = getattr(mod, "__version__", None) or getattr(mod, "version_info", None)
    if v is None:
        return "installed"
    return str(v).split()[0] if isinstance(v, str) else ".".join(str(x) for x in v[:3])

log(f"psycopg2         {_modver(psycopg2) if _have_psycopg2 else 'not installed'}")
log(f"MySQLdb          {_modver(MySQLdb) if _have_mysqldb else 'not installed'}")
log(f"pymysql          {_modver(pymysql) if _have_pymysql else 'not installed'}")
log(f"sqlite3          {sqlite3.sqlite_version}")
if _FROM_ENV:
    log(f"from environment {', '.join(_FROM_ENV)}")

try:
    from homeassistant.components.recorder.const import DEFAULT_DB_FILE as _DEFAULT_DB_FILE
except ImportError:
    _DEFAULT_DB_FILE = "home-assistant_v2.db"

# ---------------------------------------------------------------------------
# URL helpers (dialect-neutral)
# ---------------------------------------------------------------------------

def _backend(url):
    """Normalise backend name: 'sqlite' | 'mysql' | 'postgresql'."""
    b = url.get_backend_name()
    return "mysql" if b in ("mysql", "mariadb") else b

def _fix_driver(url, what):
    """Ensure an installed DBAPI driver is selected for the URL; abort if none."""
    b = _backend(url)
    drv = url.get_driver_name()
    if b == "postgresql":
        if not _have_psycopg2:
            fail(f"psycopg2 is not installed; required for the PostgreSQL {what}")
        return url.set(drivername="postgresql+psycopg2")
    if b == "mysql":
        if drv == "pymysql" and _have_pymysql:
            pass
        elif _have_mysqldb:
            url = url.set(drivername=f"{url.get_backend_name()}+mysqldb")
        elif _have_pymysql:
            url = url.set(drivername=f"{url.get_backend_name()}+pymysql")
        else:
            fail(f"neither MySQLdb (mysqlclient) nor pymysql is installed; required for the MariaDB/MySQL {what}")
        if "charset" not in url.query:
            url = url.update_query_dict({"charset": "utf8mb4"})
        return url
    if b == "sqlite":
        return url
    fail(f"unsupported {what} backend {url.get_backend_name()!r}")

def _display(url):
    return url.render_as_string(hide_password=True)

# ---------------------------------------------------------------------------
# Phase 0b: detect the source from the stopped HA's configuration
# ---------------------------------------------------------------------------

def _find_recorder_db_url(config_dir):
    """Return (db_url, origin) from configuration.yaml (top level or any package)."""
    from homeassistant.util.yaml import load_yaml, Secrets
    cfg_path = Path(config_dir) / "configuration.yaml"
    if not cfg_path.is_file():
        fail(f"{cfg_path} not found; set HA_CONFIG_DIR, or set SRC_DB_URL to skip detection")
    try:
        cfg = load_yaml(str(cfg_path), Secrets(Path(config_dir)))
    except Exception as e:
        fail(f"could not parse {cfg_path} with HA's loader ({e}); set SRC_DB_URL to skip detection")
    if not isinstance(cfg, dict):
        return None, "configuration.yaml"
    rec = cfg.get("recorder")
    if isinstance(rec, dict) and rec.get("db_url"):
        return str(rec["db_url"]), "configuration.yaml: recorder.db_url"
    ha_section = cfg.get("homeassistant")
    packages = ha_section.get("packages") if isinstance(ha_section, dict) else None
    if isinstance(packages, dict):
        for pname, pkg in packages.items():
            if isinstance(pkg, dict):
                prec = pkg.get("recorder")
                if isinstance(prec, dict) and prec.get("db_url"):
                    return str(prec["db_url"]), f"package {pname}: recorder.db_url"
    return None, "no recorder.db_url configured"

log("")
if SRC_DB_URL:
    src_url_text, src_origin = SRC_DB_URL, "SRC_DB_URL (override)"
else:
    src_url_text, src_origin = _find_recorder_db_url(HA_CONFIG_DIR)
    if src_url_text is None:
        src_url_text = f"sqlite:///{Path(HA_CONFIG_DIR) / _DEFAULT_DB_FILE}"
        src_origin += " -> recorder default SQLite"

try:
    src_url = make_url(src_url_text)
except Exception as e:
    fail(f"recorder db_url is not a valid SQLAlchemy URL: {e}")

SRC_BACKEND = _backend(src_url)
if SRC_BACKEND == "sqlite":
    p = src_url.database or ""
    if not os.path.isabs(p):
        p = str(Path(HA_CONFIG_DIR) / p)
        src_url = src_url.set(database=p)
    if not os.path.isfile(p):
        fail(f"recorder SQLite file does not exist: {p}")
src_url = _fix_driver(src_url, "source")
log(f"source origin    {src_origin}")

# ---------------------------------------------------------------------------
# Phase 0c: target configuration
# ---------------------------------------------------------------------------

def _placeholder(v):
    return not v or "xx" in str(v)

DST_TYPE = DST_TYPE.strip().lower()
if DST_DB_URL:
    try:
        dst_url = make_url(DST_DB_URL)
    except Exception as e:
        fail(f"DST_DB_URL is not a valid SQLAlchemy URL: {e}")
    DST_TYPE = dst_url.get_backend_name()
else:
    if DST_TYPE == "sqlite":
        dst_url = make_url(f"sqlite:///{DST_SQLITE_PATH}")
    elif DST_TYPE in ("postgresql", "mariadb", "mysql"):
        if _placeholder(DST_HOST):
            fail("DST_HOST is not set")
        if not DST_PASSWORD:
            fail("DST_PASSWORD is empty (set it in the config block or via environment)")
        drivername = "postgresql" if DST_TYPE == "postgresql" else "mysql"
        dst_url = URL.create(drivername, username=DST_USER, password=DST_PASSWORD,
                             host=DST_HOST, port=DST_PORT or None, database=DST_DB)
    else:
        fail(f"DST_TYPE must be postgresql, mariadb, mysql or sqlite (got {DST_TYPE!r})")
DST_BACKEND = _backend(dst_url)
dst_url = _fix_driver(dst_url, "target")
if DST_ADMIN_USER and not DST_ADMIN_PASSWORD:
    fail("DST_ADMIN_USER is set but DST_ADMIN_PASSWORD is empty")
if (BATCH_ROWS and BATCH_ROWS < 1000) or (INSERT_ROWS and INSERT_ROWS < 100):
    fail("BATCH_ROWS / INSERT_ROWS are unreasonably small (use 0 for auto)")

# Same database on both sides?
if SRC_BACKEND == DST_BACKEND:
    if SRC_BACKEND == "sqlite":
        same = os.path.abspath(src_url.database) == os.path.abspath(dst_url.database)
    else:
        same = ((src_url.host or "").lower() == (dst_url.host or "").lower()
                and (src_url.port or 0) == (dst_url.port or 0)
                and src_url.database == dst_url.database)
    if same:
        fail("source and target are the same database; nothing to migrate")

# ---------------------------------------------------------------------------
# Phase 0d: source
# ---------------------------------------------------------------------------

try:
    src_engine = create_engine(src_url, future=True)
    with src_engine.connect() as c:
        if SRC_BACKEND == "sqlite":
            src_server = "SQLite " + c.execute(text("select sqlite_version()")).scalar()
        elif SRC_BACKEND == "postgresql":
            src_server = "PostgreSQL " + c.execute(text("SHOW server_version")).scalar()
        else:
            src_server = c.execute(text("select version()")).scalar()
except Exception as e:
    fail(f"cannot connect to source {_display(src_url)}: {e}")

log("")
log(f"Source  {_display(src_url)}   {src_server}")

with src_engine.connect() as c:
    try:
        src_change = c.execute(text(
            "SELECT schema_version, change_id, changed FROM schema_changes ORDER BY change_id DESC LIMIT 1"
        )).one_or_none()
    except Exception as e:
        fail(f"source has no readable schema_changes table; is this a recorder database? ({e})")
    if src_change is None:
        fail("source schema_changes is empty; cannot determine its schema version")
    src_schema_version = int(src_change[0])
    log(f"  schema_version  {src_schema_version}   (schema_changes change_id {src_change[1]}, {src_change[2]})")

# Quiet-source check
src_other, src_quiet_note = 0, ""
if SRC_BACKEND == "sqlite":
    try:
        _c = sqlite3.connect(src_url.database, timeout=0.0)
        _c.execute("BEGIN IMMEDIATE")
        _c.rollback()
        _c.close()
    except sqlite3.OperationalError:
        src_other, src_quiet_note = 1, "another process holds a write lock on the file"
else:
    with src_engine.connect() as c:
        try:
            if SRC_BACKEND == "postgresql":
                src_other = c.execute(text(
                    "SELECT COUNT(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()")).scalar()
                src_quiet_note = f"{src_other} other session(s) on this database"
            else:
                src_other = c.execute(text(
                    "SELECT COUNT(*) FROM information_schema.PROCESSLIST "
                    "WHERE ID <> CONNECTION_ID() AND DB = :db"), {"db": src_url.database}).scalar()
                src_quiet_note = f"{src_other} other session(s) on {src_url.database} (needs PROCESS privilege to see all users)"
        except Exception as e:
            src_other, src_quiet_note = 0, f"session list not readable ({e}); assuming quiet"
log(f"  other writers   {src_other}   {src_quiet_note}".rstrip())
if REQUIRE_QUIET_SOURCE and src_other:
    fail("source is not quiet — stop Home Assistant (and anything else) before migrating")

# Table inventory from the models, in FK-dependency order
tables = list(Base.metadata.sorted_tables)
src_insp = inspect(src_engine)
src_tables = set(src_insp.get_table_names())
src_q = src_engine.dialect.identifier_preparer.quote

plan = []
total_rows = 0
with src_engine.connect() as c:
    for t in tables:
        if t.name not in src_tables:
            log(f"  {t.name:<24} MISSING ON SOURCE (model defines it; will be created empty)")
            plan.append({"table": t, "present": False, "rows": 0, "cols": [], "src_only": [], "dst_only": []})
            continue
        src_cols = [col["name"] for col in src_insp.get_columns(t.name)]
        dst_cols = [col.name for col in t.columns]
        common = [n for n in dst_cols if n in src_cols]
        src_only = [n for n in src_cols if n not in dst_cols]
        dst_only = [n for n in dst_cols if n not in src_cols]
        n = c.execute(text(f"SELECT COUNT(*) FROM {src_q(t.name)}")).scalar()
        total_rows += n
        plan.append({"table": t, "present": True, "rows": n, "cols": common,
                     "src_only": src_only, "dst_only": dst_only})
        extra = ""
        if src_only:
            extra += f"   source-only: {','.join(src_only)} (skipped)"
        if dst_only:
            extra += f"   target-only: {','.join(dst_only)}"
        log(f"  {t.name:<24}{n:>14,} rows{extra}")

for p in plan:
    for name in p["dst_only"]:
        col = p["table"].columns[name]
        if not col.nullable and col.server_default is None and not col.autoincrement:
            fail(f"{p['table'].name}.{name} exists only on the target and is NOT NULL without a default; "
                 "the copy cannot satisfy it. Source and target schema versions are probably different.")

log(f"  {'total':<24}{total_rows:>14,} rows  ({len(tables)} tables)")

# ---------------------------------------------------------------------------
# Phase 0e: target bootstrap (optional) and checks
# ---------------------------------------------------------------------------

log("")
bootstrap = "manual"

def _manual_sql():
    if DST_BACKEND == "postgresql":
        return [f"CREATE ROLE \"{dst_url.username}\" LOGIN PASSWORD '...';",
                f"CREATE DATABASE \"{dst_url.database}\" OWNER \"{dst_url.username}\" ENCODING 'UTF8' TEMPLATE template0;"]
    if DST_BACKEND == "mysql":
        return [f"CREATE DATABASE `{dst_url.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;",
                f"CREATE USER '{dst_url.username}'@'%' IDENTIFIED BY '...';",
                f"GRANT ALL PRIVILEGES ON `{dst_url.database}`.* TO '{dst_url.username}'@'%';"]
    return []

if DST_ADMIN_USER and DST_BACKEND != "sqlite":
    bootstrap = "admin"
    try:
        if DST_BACKEND == "postgresql":
            admin_url = dst_url.set(username=DST_ADMIN_USER, password=DST_ADMIN_PASSWORD, database="postgres")
        else:
            admin_url = dst_url.set(username=DST_ADMIN_USER, password=DST_ADMIN_PASSWORD)._replace(database=None)
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
        with admin_engine.connect() as c:
            if DST_BACKEND == "postgresql":
                role_exists = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": dst_url.username}).scalar()
                db_exists = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": dst_url.database}).scalar()
                if not role_exists and not _ARGS.dry_run:
                    c.execute(text(f'CREATE ROLE "{dst_url.username}" LOGIN PASSWORD :p'), {"p": dst_url.password})
                if not db_exists and not _ARGS.dry_run:
                    c.execute(text(f'CREATE DATABASE "{dst_url.database}" OWNER "{dst_url.username}" '
                                   "ENCODING 'UTF8' TEMPLATE template0"))
            else:
                role_exists = c.execute(text("SELECT 1 FROM mysql.user WHERE user = :u LIMIT 1"),
                                        {"u": dst_url.username}).scalar()
                db_exists = c.execute(text("SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :d"),
                                      {"d": dst_url.database}).scalar()
                if not db_exists and not _ARGS.dry_run:
                    c.execute(text(f"CREATE DATABASE `{dst_url.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
                if not role_exists and not _ARGS.dry_run:
                    c.execute(text(f"CREATE USER '{dst_url.username}'@'%' IDENTIFIED BY :p"), {"p": dst_url.password})
                if not _ARGS.dry_run:
                    c.execute(text(f"GRANT ALL PRIVILEGES ON `{dst_url.database}`.* TO '{dst_url.username}'@'%'"))
                    c.execute(text("FLUSH PRIVILEGES"))
        admin_engine.dispose()
        suffix = "  (dry-run: would create)" if _ARGS.dry_run else ""
        if not role_exists:
            log(f"  bootstrap       created user {dst_url.username}{suffix}")
        if not db_exists:
            log(f"  bootstrap       created database {dst_url.database}{suffix}")
        if _ARGS.dry_run and not (role_exists and db_exists):
            log(f"Target  {_display(dst_url)}   (not yet created; dry-run)")
            log("")
            log("Dry run: bootstrap would create the target; nothing else can be checked until it exists.")
            sys.exit(0)
    except Exception as e:
        fail(f"bootstrap as {DST_ADMIN_USER} failed: {e}")

dst_warnings = []
dst_max_packet = None
if DST_BACKEND == "sqlite":
    dst_server = f"SQLite {sqlite3.sqlite_version}"
    dst_path = dst_url.database
    dst_tables = 0
    if os.path.exists(dst_path):
        try:
            _c = sqlite3.connect(dst_path)
            dst_tables = _c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
            _c.close()
        except sqlite3.DatabaseError as e:
            fail(f"target file exists and is not a usable SQLite database: {e}")
    if not os.access(os.path.dirname(dst_path) or ".", os.W_OK):
        fail(f"target directory is not writable: {os.path.dirname(dst_path)}")
    dst_engine = create_engine(dst_url, future=True)
    log(f"Target  {_display(dst_url)}   {dst_server}")
    log(f"  tables {dst_tables}   bootstrap: n/a")
else:
    try:
        dst_engine = create_engine(dst_url, future=True)
        with dst_engine.connect() as c:
            if DST_BACKEND == "postgresql":
                dst_server = "PostgreSQL " + c.execute(text("SHOW server_version")).scalar()
                dst_encoding = c.execute(text("SHOW server_encoding")).scalar()
                dst_tables = c.execute(text(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'")).scalar()
                ok_db = c.execute(text("SELECT has_database_privilege(current_user, current_database(), 'CREATE')")).scalar()
                ok_schema = c.execute(text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")).scalar()
                log(f"Target  {_display(dst_url)}   {dst_server}")
                log(f"  encoding {dst_encoding}   tables {dst_tables}   role CREATE db={ok_db} schema={ok_schema}   bootstrap: {bootstrap}")
                if dst_encoding.upper() != "UTF8":
                    fail(f"target database encoding is {dst_encoding}, must be UTF8")
                if not (ok_db and ok_schema):
                    fail(f"{dst_url.username} lacks CREATE on the database or the public schema; make it the database owner")
            else:
                dst_server = c.execute(text("SELECT version()")).scalar()
                dst_charset = c.execute(text(
                    "SELECT DEFAULT_CHARACTER_SET_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :d"),
                    {"d": dst_url.database}).scalar()
                dst_tables = c.execute(text(
                    "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = :d"),
                    {"d": dst_url.database}).scalar()
                dst_default_engine = c.execute(text("SELECT @@default_storage_engine")).scalar()
                dst_max_packet = int(c.execute(text("SELECT @@max_allowed_packet")).scalar())
                log(f"Target  {_display(dst_url)}   {dst_server}")
                log(f"  charset {dst_charset}   tables {dst_tables}   default engine {dst_default_engine}   bootstrap: {bootstrap}")
                if (dst_charset or "").lower() != "utf8mb4":
                    fail(f"target database charset is {dst_charset}, must be utf8mb4")
                if (dst_default_engine or "").lower() != "innodb":
                    dst_warnings.append(f"default storage engine is {dst_default_engine}; tables are created as InnoDB explicitly")
                m = re.match(r"(\d+)\.(\d+)\.(\d+)", dst_server or "")
                if m and "mariadb" in dst_server.lower():
                    maj, mnr, pat = (int(x) for x in m.groups())
                    fixed = {(10, 5): 17, (10, 6): 9, (10, 7): 5, (10, 8): 4}
                    if (maj, mnr) in fixed and pat < fixed[(maj, mnr)]:
                        dst_warnings.append(f"MariaDB {maj}.{mnr}.{pat} has the recorder purge/history performance regression "
                                            f"fixed in {maj}.{mnr}.{fixed[(maj, mnr)]}; upgrade the server")
    except SystemExit:
        raise
    except Exception as e:
        log(f"Target  {_display(dst_url)}   UNREACHABLE")
        log("")
        log("Create the user and database as your database admin, then re-run:")
        for s in _manual_sql():
            log(f"  {s}")
        log("or set DST_ADMIN_USER / DST_ADMIN_PASSWORD to let the script do it.")
        fail(str(e))

if REQUIRE_EMPTY_TARGET and dst_tables:
    fail(f"target already has {dst_tables} table(s); drop them (or the database/file), or set REQUIRE_EMPTY_TARGET = False")
for w in dst_warnings:
    log(f"  WARNING: {w}")

# ---------------------------------------------------------------------------
# Phase 0e2: batch sizing inputs (memory limit of this process, packet limit of the target)
# ---------------------------------------------------------------------------

def _memory_limit():
    """(bytes, origin) — cgroup v2, cgroup v1, then physical RAM."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(path).read().strip()
            if v != "max":
                n = int(v)
                if 0 < n < (1 << 60):
                    return n, "cgroup limit"
        except (OSError, ValueError):
            pass
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"), "physical RAM"
    except (OSError, ValueError, AttributeError):
        return 1 << 30, "assumed 1 GiB"

_mem_limit, _mem_origin = _memory_limit()
MEM_BUDGET = max(64 << 20, min(512 << 20, _mem_limit // 10))

def _fmt_bytes(n):
    return f"{n / (1 << 20):,.0f} MB" if n < (1 << 30) * 4 else f"{n / (1 << 30):,.1f} GB"

_auto_batch = BATCH_ROWS == 0
_auto_insert = INSERT_ROWS == 0
log("")
log(f"Sizing  memory {_fmt_bytes(_mem_limit)} ({_mem_origin}) -> batch budget {_fmt_bytes(MEM_BUDGET)}"
    + (f";  max_allowed_packet {_fmt_bytes(dst_max_packet)}" if dst_max_packet else ""))
log(f"  BATCH_ROWS {'auto (per table)' if _auto_batch else f'{BATCH_ROWS:,} (fixed)'}   "
    f"INSERT_ROWS {'auto (per table)' if _auto_insert else f'{INSERT_ROWS:,} (fixed)'}"
    + ("   (INSERT_ROWS unused: PostgreSQL target uses COPY)" if DST_BACKEND == "postgresql" else ""))

# ---------------------------------------------------------------------------
# Phase 0f: the gate
# ---------------------------------------------------------------------------

log("")
if src_schema_version != int(SCHEMA_VERSION):
    log(f"Schema versions DIFFER: source {src_schema_version} vs installed models {SCHEMA_VERSION}")
    log("The installed Home Assistant is not the version that last wrote the source database.")
    log("Do not upgrade HA between taking the source offline and migrating. Nothing was written.")
    sys.exit(3)
log(f"Schema versions match ({src_schema_version} == {SCHEMA_VERSION})  OK")

seq_tables = [p for p in plan if len(p["table"].primary_key.columns) == 1
              and isinstance(list(p["table"].primary_key.columns)[0].type, Integer)]
n_idx = sum(len(t.indexes) for t in tables)
log("")
log(f"Plan: {SRC_BACKEND} -> {DST_BACKEND}: create {len(tables)} tables from installed models, copy {total_rows:,} rows,")
log(f"      rebuild {n_idx} indexes and the foreign keys, reset {len(seq_tables)} ID counters.")

if _ARGS.dry_run:
    log("")
    log("Dry run complete. Nothing was written.")
    sys.exit(0)

if not ASSUME_YES:
    try:
        answer = input("HA must be stopped. Continue? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        log("Cancelled by user. Nothing was written.")
        sys.exit(0)
else:
    log("Confirmation skipped (ASSUME_YES / --yes).")

# ---------------------------------------------------------------------------
# Phase 1: create the target schema from the installed models
# ---------------------------------------------------------------------------

t0 = time.time()
log("")
log("=== Create target schema ===")
try:
    Base.metadata.create_all(dst_engine)
except Exception as e:
    fail(f"create_all failed: {e}")
dst_insp = inspect(dst_engine)
log(f"created {len(dst_insp.get_table_names())} tables from models (SCHEMA_VERSION {SCHEMA_VERSION})")
dq = dst_engine.dialect.identifier_preparer.quote

# ---------------------------------------------------------------------------
# Phase 2: drop FKs and indexes (captured for rebuild) — dialect-neutral
# ---------------------------------------------------------------------------

log("")
log("=== Drop constraints for bulk load ===")
fk_defs = []   # dicts from the inspector, plus table name
if DST_BACKEND != "sqlite":
    for t in tables:
        for fk in dst_insp.get_foreign_keys(t.name):
            if fk.get("name"):
                fk_defs.append({"table": t.name, **fk})
    with dst_engine.begin() as c:
        for fk in fk_defs:
            if DST_BACKEND == "postgresql":
                c.execute(text(f"ALTER TABLE {dq(fk['table'])} DROP CONSTRAINT {dq(fk['name'])}"))
            else:
                c.execute(text(f"ALTER TABLE {dq(fk['table'])} DROP FOREIGN KEY {dq(fk['name'])}"))
with dst_engine.begin() as c:
    for t in tables:
        for idx in t.indexes:
            c.execute(DropIndex(idx))
log(f"dropped {len(fk_defs)} foreign keys and {n_idx} indexes (rebuilt after the copy)")

# ---------------------------------------------------------------------------
# Phase 3: copy
# ---------------------------------------------------------------------------

_NUL_STRIPPED = 0
_UTC = _dt.timezone.utc

def _fmt_pg(v):
    """One value in PostgreSQL COPY text format."""
    global _NUL_STRIPPED
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "\\\\x" + bytes(v).hex()
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        return repr(v)
    if isinstance(v, (int, Decimal)):
        return str(v)
    if isinstance(v, _dt.datetime):
        if v.tzinfo is None:
            return v.isoformat(sep=" ") + "+00"
        return v.astimezone(_UTC).isoformat(sep=" ")
    if isinstance(v, (_dt.date, _dt.time)):
        return v.isoformat()
    s = str(v)
    if "\x00" in s:
        _NUL_STRIPPED += 1
        s = s.replace("\x00", "")
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

def _conv_dbapi(v):
    """One value as a DBAPI parameter for MySQL/SQLite executemany."""
    if isinstance(v, memoryview):
        return bytes(v)
    if isinstance(v, _dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(_UTC).replace(tzinfo=None)
        if DST_BACKEND == "sqlite":
            return v.strftime("%Y-%m-%d %H:%M:%S.%f")
        return v
    if DST_BACKEND == "sqlite":
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, Decimal):
            return float(v)
    return v

def _load_batch(cur, tname, colnames, rows, insert_rows):
    if DST_BACKEND == "postgresql":
        buf = io.StringIO()
        for row in rows:
            buf.write("\t".join(_fmt_pg(v) for v in row))
            buf.write("\n")
        buf.seek(0)
        collist = ", ".join(dq(n) for n in colnames)
        cur.copy_expert(f"COPY {dq(tname)} ({collist}) FROM STDIN", buf)
        return
    ph = "?" if DST_BACKEND == "sqlite" else "%s"
    sql = (f"INSERT INTO {dq(tname)} ({', '.join(dq(n) for n in colnames)}) "
           f"VALUES ({', '.join([ph] * len(colnames))})")
    for i in range(0, len(rows), insert_rows):
        chunk = [tuple(_conv_dbapi(v) for v in r) for r in rows[i:i + insert_rows]]
        cur.executemany(sql, chunk)

def _row_bytes(row):
    """Approximate wire size of one row as the loader will send it."""
    n = 2 * len(row)
    for v in row:
        if v is None:
            n += 2
        elif isinstance(v, (bytes, bytearray, memoryview)):
            n += 2 * len(v) + 4
        elif isinstance(v, str):
            n += len(v) + v.count("\\") + 2
        elif isinstance(v, float):
            n += 18
        elif isinstance(v, (int, Decimal)):
            n += 12
        else:
            n += 32
    return n

def _clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))

SAMPLE_ROWS = 2_000

def _size_for(avg_row_bytes):
    """(batch_rows, insert_rows) for a table given its average encoded row size."""
    batch = BATCH_ROWS or _clamp(MEM_BUDGET // (avg_row_bytes * 8), 5_000, 200_000)
    if DST_BACKEND == "mysql":
        limit = (dst_max_packet or (16 << 20)) // 2
        insert = INSERT_ROWS or _clamp(limit // int(avg_row_bytes * 1.3), 100, 20_000)
    else:
        insert = INSERT_ROWS or batch
    return batch, min(insert, batch)

log("")
log("=== Copy ===")
raw = dst_engine.raw_connection()
raw_cur = raw.cursor()
if DST_BACKEND == "postgresql":
    raw_cur.execute("SET TIME ZONE 'UTC'")
    raw_cur.execute("SET synchronous_commit = off")
elif DST_BACKEND == "mysql":
    raw_cur.execute("SET SESSION foreign_key_checks = 0")
    raw_cur.execute("SET SESSION unique_checks = 0")
    raw_cur.execute("SET SESSION time_zone = '+00:00'")
else:
    raw_cur.execute("PRAGMA synchronous = OFF")
    raw_cur.execute("PRAGMA journal_mode = MEMORY")

copied = {}
with src_engine.connect().execution_options(stream_results=True) as sc:
    for p in plan:
        t = p["table"]
        if not p["present"] or p["rows"] == 0:
            copied[t.name] = 0
            log(f"  {t.name:<24} skipped (no source rows)")
            continue
        colnames = p["cols"]
        cols = [t.c[n] for n in colnames]
        pk_cols = list(t.primary_key.columns)
        keyset = len(pk_cols) == 1 and isinstance(pk_cols[0].type, Integer) and pk_cols[0].name in colnames
        n_done = 0
        t_start = time.time()
        t_report = t_start
        batch, insert = (BATCH_ROWS or SAMPLE_ROWS), (INSERT_ROWS or 500)
        sized = not (_auto_batch or _auto_insert)
        try:
            if keyset:
                pk = pk_cols[0]
                pk_idx = colnames.index(pk.name)
                last = None
                while True:
                    q = select(*cols).order_by(pk).limit(batch)
                    if last is not None:
                        q = q.where(pk > last)
                    rows = sc.execute(q).all()
                    if not rows:
                        break
                    if not sized:
                        avg = max(1, sum(_row_bytes(r) for r in rows) // len(rows))
                        batch, insert = _size_for(avg)
                        sized = True
                        if p["rows"] > len(rows):
                            log(f"  {t.name:<24} ~{avg:,} B/row -> batch {batch:,}"
                                + (f", insert {insert:,}" if DST_BACKEND != "postgresql" else ""))
                    _load_batch(raw_cur, t.name, colnames, rows, insert)
                    n_done += len(rows)
                    last = rows[-1][pk_idx]
                    if time.time() - t_report >= 30:
                        t_report = time.time()
                        rate = n_done / max(t_report - t_start, 0.001)
                        log(f"  {t.name:<24}{n_done:>14,} / {p['rows']:,}   {rate:,.0f} rows/s")
            else:
                result = sc.execute(select(*cols))
                for rows in result.partitions(batch):
                    if not sized:
                        avg = max(1, sum(_row_bytes(r) for r in rows) // len(rows))
                        batch, insert = _size_for(avg)
                        sized = True
                    _load_batch(raw_cur, t.name, colnames, rows, insert)
                    n_done += len(rows)
            raw.commit()
        except Exception as e:
            raw.rollback()
            log("")
            log(f"load into {t.name} failed after {n_done:,} rows: {e}")
            log("The target is now partial. Drop the target database/file (or all its tables) and re-run.")
            sys.exit(4)
        copied[t.name] = n_done
        log(f"  {t.name:<24}{n_done:>14,} rows  in {time.time() - t_start:,.1f}s")

raw_cur.close()
raw.close()
if _NUL_STRIPPED:
    log(f"note: {_NUL_STRIPPED} string value(s) contained NUL bytes, which PostgreSQL cannot store; they were removed")

# ---------------------------------------------------------------------------
# Phase 4: rebuild indexes, FKs, ID counters; analyze
# ---------------------------------------------------------------------------

log("")
log("=== Rebuild indexes and foreign keys ===")
with dst_engine.begin() as c:
    if DST_BACKEND == "postgresql" and PG_MAINTENANCE_WORK_MEM:
        c.execute(text(f"SET LOCAL maintenance_work_mem = '{PG_MAINTENANCE_WORK_MEM}'"))
    for t in tables:
        for idx in t.indexes:
            t_i = time.time()
            c.execute(CreateIndex(idx))
            log(f"  index {idx.name:<48} {time.time() - t_i:,.1f}s")
    for fk in fk_defs:
        t_i = time.time()
        cols = ", ".join(dq(n) for n in fk["constrained_columns"])
        refcols = ", ".join(dq(n) for n in fk["referred_columns"])
        opts = fk.get("options") or {}
        extra = ""
        if opts.get("ondelete"):
            extra += f" ON DELETE {opts['ondelete']}"
        if opts.get("onupdate"):
            extra += f" ON UPDATE {opts['onupdate']}"
        c.execute(text(f"ALTER TABLE {dq(fk['table'])} ADD CONSTRAINT {dq(fk['name'])} "
                       f"FOREIGN KEY ({cols}) REFERENCES {dq(fk['referred_table'])} ({refcols}){extra}"))
        log(f"  fk    {fk['name']:<48} {time.time() - t_i:,.1f}s")

log("")
log("=== Reset ID counters ===")
if DST_BACKEND == "sqlite":
    log("  not needed on SQLite (rowid-based)")
else:
    with dst_engine.begin() as c:
        for p in seq_tables:
            t = p["table"]
            pk = list(t.primary_key.columns)[0]
            mx = c.execute(text(f"SELECT MAX({dq(pk.name)}) FROM {dq(t.name)}")).scalar()
            if DST_BACKEND == "postgresql":
                seq = c.execute(text("SELECT pg_get_serial_sequence(:t, :c)"),
                                {"t": dq(t.name), "c": pk.name}).scalar()
                if not seq:
                    continue
                if mx is None:
                    c.execute(text("SELECT setval(:s, 1, false)"), {"s": seq})
                else:
                    c.execute(text("SELECT setval(:s, :v, true)"), {"s": seq, "v": int(mx)})
                log(f"  {t.name:<24} {seq}  -> {int(mx) if mx is not None else 1:,}")
            else:
                nxt = (int(mx) + 1) if mx is not None else 1
                c.execute(text(f"ALTER TABLE {dq(t.name)} AUTO_INCREMENT = {nxt}"))
                log(f"  {t.name:<24} AUTO_INCREMENT -> {nxt:,}")

log("")
log("=== Analyze ===")
with dst_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
    if DST_BACKEND == "sqlite":
        c.execute(text("ANALYZE"))
    else:
        for t in tables:
            c.execute(text(f"ANALYZE TABLE {dq(t.name)}" if DST_BACKEND == "mysql" else f"ANALYZE {dq(t.name)}"))
log("done")

# ---------------------------------------------------------------------------
# Phase 5: verify
# ---------------------------------------------------------------------------

log("")
log("=== Verify ===")
mismatch = 0
with dst_engine.connect() as dc:
    for p in plan:
        t = p["table"]
        n_src = p["rows"]
        n_dst = dc.execute(text(f"SELECT COUNT(*) FROM {dq(t.name)}")).scalar()
        flag = "OK" if n_src == n_dst else "MISMATCH"
        if flag != "OK":
            mismatch += 1
        log(f"  {t.name:<24}{n_src:>14,}  ->  {n_dst:>14,}   {flag}")
    dst_version = dc.execute(text(
        "SELECT schema_version FROM schema_changes ORDER BY change_id DESC LIMIT 1")).scalar()
    log(f"  schema_changes last row on target: schema_version {dst_version}")
    if dst_version is None or int(dst_version) != int(SCHEMA_VERSION):
        mismatch += 1
        log("  MISMATCH: target schema_changes does not report the installed SCHEMA_VERSION")

log("")
elapsed = time.time() - t0
if mismatch:
    log(f"Migration completed with {mismatch} mismatch(es) in {elapsed/60:,.1f} min. Do NOT switch the recorder; investigate.")
    sys.exit(5)

if DST_BACKEND == "postgresql":
    new_url = f"postgresql://{dst_url.username}:<password>@{dst_url.host}:{dst_url.port or 5432}/{dst_url.database}"
elif DST_BACKEND == "mysql":
    new_url = f"mysql://{dst_url.username}:<password>@{dst_url.host}:{dst_url.port or 3306}/{dst_url.database}?charset=utf8mb4"
else:
    new_url = f"sqlite:///{dst_url.database}"

log(f"Migration completed in {elapsed/60:,.1f} min with no mismatches.")
log("")
log("Next steps:")
log("  1. In configuration.yaml, point the recorder at the new database:")
log("       recorder:")
log(f"         db_url: {new_url}")
log("  2. Start Home Assistant and watch the log for recorder warnings on first start.")
log("  3. Keep the source database read-only until you are satisfied; it is your rollback.")
