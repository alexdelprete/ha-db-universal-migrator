# Changelog

## 1.0.3 — 2026-09-14

- Renamed to `ha-db-universal-migrator`. No functional change.

## 1.0.2 — 2026-09-14

- Driver version reporting made defensive (`mysqlclient` builds without `__version__`).
- First production run: MariaDB 11.8 → PostgreSQL 18.6, 31.8M rows, 6.9 min.

## 1.0.1 — 2026-09-14

- Read the HA version from `homeassistant.const` (top-level `__version__` does not exist in the real package).

## 1.0.0 — 2026-09-14

- Initial release. SQLite / MariaDB / MySQL / PostgreSQL as source and target; schema from the installed HA models; source detected from `configuration.yaml`; per-table auto-sized batches; dialect-neutral constraint rebuild; row-count verification.
