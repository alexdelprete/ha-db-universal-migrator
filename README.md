# ha-db-universal-migrator

Migrate the Home Assistant **recorder** database between engines — **SQLite, MariaDB, MySQL, PostgreSQL** — in any direction, keeping the full history: states, events, long-term statistics, everything.

Home Assistant's own position is that [changing the recorder database is not supported and loses history](https://www.home-assistant.io/integrations/recorder/). This script exists because that doesn't have to be true.

## Supported migrations

Any engine to any other engine. Sources are rows, targets are columns.

| source ↓ / target → | SQLite | MariaDB | MySQL | PostgreSQL |
|---|:---:|:---:|:---:|:---:|
| **SQLite** | ◐ | ✅ | ☑ | ✅ |
| **MariaDB** | ☑ | ◐ | ☑ | ✅ **production** |
| **MySQL** | ☑ | ☑ | ◐ | ☑ |
| **PostgreSQL** | ✅ | ☑ | ☑ | ◐ |

| | |
|---|---|
| ✅ **tested** | run end-to-end against a real server, every table compared value by value afterwards: strings with escapes and Unicode, booleans, binary context IDs, floats, timezone-aware timestamps, the self-referencing foreign key on `states` |
| ✅ **production** | the above, plus a real Home Assistant instance migrated and running on the result (see *A real run*) |
| ☑ **supported** | same code path as a tested combination, not exercised against a server |
| ◐ **same engine** | allowed only between different databases (another server, another file); the script refuses to migrate a database onto itself |

MariaDB and MySQL share one code path (SQLAlchemy backend `mysql`). Engine versions HA supports: MariaDB ≥ 10.3, MySQL ≥ 8.0, PostgreSQL ≥ 12, SQLite ≥ 3.40.1.

## What makes it different

- **No DDL is shipped, no HA version is pinned.** The target schema is generated at run time from the recorder models installed in the Home Assistant you are running (`Base.metadata.create_all`), which is exactly how the recorder creates a fresh database. Whatever schema version your HA is on, that is the schema you get.
- **Refuses to guess.** The source's `schema_version` (from its `schema_changes` table) must equal the installed models' `SCHEMA_VERSION`, or nothing is written.
- **Source is detected, not configured.** It reads `recorder.db_url` from your `configuration.yaml` using HA's own YAML loader — `!secret`, `!include` and packages resolve exactly as HA resolves them. No `db_url` means the recorder's default SQLite file.
- **Column-level diff before the copy.** Columns present on only one side (legacy leftovers from years of migrations) are reported and skipped; a target-only `NOT NULL` column aborts the run before any data moves.
- **Fast.** PostgreSQL targets load via `COPY`; MariaDB/MySQL/SQLite via batched multi-row inserts. Indexes and foreign keys are dropped for the load and rebuilt afterwards. Batch sizes are auto-sized per table from the measured row width, the process's memory limit and the target's `max_allowed_packet`.
- **Verified.** Per-table row counts are compared source vs target at the end; a mismatch exits non-zero and tells you not to switch.
- **Dry-run first.** `--dry-run` executes the entire pre-flight — imports, source detection, connections, row counts, column diff, target checks, schema-version gate — and writes nothing.

## Prerequisites

| | |
|---|---|
| Home Assistant **stopped** for the whole run | The one hard rule. Guarantees the source, its dump and `SCHEMA_VERSION` agree. The script checks for other sessions on the source and aborts if it finds any. |
| Run with HA's own Python | The `homeassistant` package must be importable — that is where the schema comes from. See *Usage* for each install type. |
| DB drivers | `psycopg2` for PostgreSQL, `mysqlclient` (`MySQLdb`) or `pymysql` for MariaDB/MySQL. The official HA container image ships both. |
| Network reach | From where the script runs to both the source and the target server. |
| Target database | Either created by you (`UTF8` on PostgreSQL, `utf8mb4` on MariaDB/MySQL, owned by the HA account) or created by the script if you give it admin credentials. |
| A backup of the source | The script never modifies the source, but take a dump anyway. It is your rollback. |
| Free disk on the target | At least the logical size of the source database, plus WAL headroom on PostgreSQL. |

No superuser is needed for the migration itself. The only privileged operations — creating the database and the account — are either done by you beforehand or by the optional `DST_ADMIN_*` bootstrap, which is used for those two statements and nothing else.

## Configuration

Edit the block at the top of the script. Every value can also be set as an environment variable of the same name.

```python
# Source: detected from HA's configuration
HA_CONFIG_DIR = "/config"        # directory containing configuration.yaml
SRC_DB_URL    = ""               # override only if detection can't parse your config

# Target
DST_TYPE      = "postgresql"     # postgresql | mariadb | mysql | sqlite
DST_HOST      = "10.0.0.10"
DST_PORT      = 0                # 0 = engine default
DST_DB        = "homeassistant"
DST_USER      = "homeassistant"
DST_PASSWORD  = ""
DST_DB_URL    = ""               # or a full SQLAlchemy URL instead of the fields above
DST_SQLITE_PATH = "/config/home-assistant_v2.new.db"   # sqlite target only

# Optional bootstrap: creates DST_USER and DST_DB if missing, then never used again
DST_ADMIN_USER     = ""          # "postgres" / "root"
DST_ADMIN_PASSWORD = ""

# Copy behaviour
BATCH_ROWS   = 0                 # 0 = auto per table
INSERT_ROWS  = 0                 # 0 = auto per table (MariaDB/MySQL/SQLite targets)
LOG_FILE     = "/config/ha-db-universal-migrator.log"
PG_MAINTENANCE_WORK_MEM = ""     # e.g. "1GB" for the index rebuild on a PostgreSQL target

# Safety
REQUIRE_EMPTY_TARGET = True      # abort if the target already has tables
REQUIRE_QUIET_SOURCE = True      # abort if the source has other sessions
ASSUME_YES           = False     # skip the confirmation prompt (same as --yes)
```

Passwords with special characters go in `DST_PASSWORD` literally — the script builds the URL and encodes them. If you use `DST_DB_URL` or `SRC_DB_URL` instead, percent-encode `@ : / ? # %` yourself.

## Usage

Put the script in HA's config directory, then run it with HA's interpreter:

```bash
# Container (docker compose)
docker compose run --rm --entrypoint python3 homeassistant /config/ha-db-universal-migrator.py --dry-run
docker compose run --rm --entrypoint python3 homeassistant /config/ha-db-universal-migrator.py

# Core / venv
source /srv/homeassistant/bin/activate
python3 ha-db-universal-migrator.py --dry-run

# Supervised
docker exec -it homeassistant python3 /config/ha-db-universal-migrator.py --dry-run
```

Flags: `--dry-run` runs pre-flight only and writes nothing; `--yes` skips the confirmation prompt.

Recommended sequence:

1. Stop Home Assistant.
2. Dump the source database.
3. `--dry-run`. Read the report.
4. Real run. Answer `y`.
5. On "no mismatches": point `recorder.db_url` at the new database, start Home Assistant, watch the log for recorder warnings on first start.
6. Keep the source until you are satisfied.

A failed load leaves the target partial: drop the target database (or file) and re-run. There is no resume.

## A real run

MariaDB 11.8 → PostgreSQL 18.6, Home Assistant 2026.9.2, schema version 53, on a Proxmox homelab (both databases in LXCs on the same node; the PostgreSQL LXC had 4 vCPU / 4 GB / NVMe-backed ZFS, `PG_MAINTENANCE_WORK_MEM = "1GB"`, `max_parallel_maintenance_workers = 3`).

| | |
|---|---|
| Rows | 31,835,998 across 13 tables — `states` 11.5M, `statistics` 15.3M, `statistics_short_term` 3.6M |
| Source size | 5.4 GB logical |
| Copy | `states` 153 s at ~75k rows/s; `statistics` 162 s at ~95k rows/s |
| Index + FK rebuild | ~70 s for 20 indexes and 7 foreign keys |
| **Script total** | **6.9 minutes**, 13/13 tables matched |
| HA downtime, stop to start | 18 minutes including the dump and post-checks |

On first start the recorder logged only `Ended unfinished session` for the run it had left open on the old database, and carried on. HA's System Information panel reported *Oldest run start time* from twelve days earlier — the history had come along.

## What it does, step by step

1. **Pre-flight** — import `homeassistant`, print version and `SCHEMA_VERSION`; detect the source from `configuration.yaml`; connect to it; read `schema_changes`; check for other sessions; count rows and diff columns per table against the models; bootstrap or verify the target; assert schema versions are equal; print the plan; ask for confirmation.
2. **Create target schema** from the installed models.
3. **Drop** foreign keys and indexes on the target (captured via the SQLAlchemy inspector, rebuilt from the model metadata — no engine-specific catalog queries).
4. **Copy** every table in foreign-key dependency order (`Base.metadata.sorted_tables`), keyset-paginated on the integer primary key, with per-table auto-sized batches.
5. **Rebuild** indexes and foreign keys; reset sequences / `AUTO_INCREMENT` to the real high-water marks; `ANALYZE`.
6. **Verify** row counts per table and the target's `schema_version`.

## Known limitations

- **Sessions visible on a MariaDB/MySQL source** require the `PROCESS` privilege to include other users' connections. Without it the quiet check sees only the HA account's own sessions — which is the case that matters when HA was the only client.
- **`mysqlclient` builds one multi-row statement per batch** and does not split it; the auto-sized `INSERT_ROWS` keeps it under `max_allowed_packet`. `pymysql` splits on its own.
- **A freshly created schema is not guaranteed identical to a migrated one** at the same version — HA's migrations have occasionally left columns behind. The column diff handles the common case (source-only leftovers are skipped). A target-only `NOT NULL` column without a default aborts.
- **TimescaleDB** and other extensions are not involved; the recorder uses plain SQL and so does this script.
- **No resume.** A failed copy means drop the target and re-run.

## License

MIT. See `LICENSE`.

## Acknowledgements

The idea of resetting sequences after the copy and the "let HA create the schema, then load data only" pattern come from years of community write-ups on the Home Assistant forum. This script's contribution is doing both from the installed models instead of a shipped DDL file, and doing it for every engine pair.
