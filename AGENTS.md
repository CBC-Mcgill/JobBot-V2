# Been Job Bot maintenance contract

## Package map

- `cli.py`: command parsing, process lock, and application wiring.
- `config.py`: `.env` and registry validation.
- `service.py`: scan orchestration and the publisher/provider boundary.
- `store.py`: SQLite schema/migration, snapshots, scheduling, delivery outbox, and backups.
- `providers.py`, `http.py`, `discovery.py`: public-board retrieval and discovery.
- `classify.py`: conservative eligibility and Discord routing rules.
- `discord_output.py`: Discord gateway, message rendering, and reconciliation lookup.
- `models.py`, `scheduling.py`: shared data and pure scheduling policy.

## Invariants

- Persist a complete board snapshot atomically with related outbox changes. Never treat a partial provider response as a snapshot.
- A delivery enters `sending` before Discord I/O. Do not retry an uncertain send until history proves it absent; this protects against duplicate posts.
- Debug and production delivery histories are intentionally separate.
- Registry sync disables removed manual boards without deleting jobs, deliveries, scans, or discovered boards. Keep registry and discovery JSON shapes compatible.
- Preserve SQLite schema-v1-to-v2 migration and the existing CLI, `.env` keys (including `SCAN_INTERVAL_SECONDS`), Compose interface, and database defaults.

## Commands

```bash
make lint
make test
make check-config
```

`check-config` must remain network-free. Use a temporary database and fakes/mocks for tests; never run a live Discord scan as a test.

## Tests by subsystem

- configuration/CLI: `tests/test_config.py`
- delivery and service orchestration: `tests/test_delivery.py`
- snapshots, scheduling, and migrations: `tests/test_adaptive_scan.py`, `tests/test_scheduling.py`, `tests/test_migration.py`
- providers and HTTP: `tests/test_providers.py`, `tests/test_http.py`
- classification, discovery, and embeds: `tests/test_classify.py`, `tests/test_discovery.py`, `tests/test_output.py`

## Secrets and edits

Never print, commit, or place real tokens in fixtures. Keep `.env` local; update `.env.example` whenever a supported setting changes. Document externally visible behaviour in `README.md` and `SPEC.md`. Prefer small, typed contract docstrings at cross-module entry points over new framework layers or services.
