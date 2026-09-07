# Been Job Bot

Been Job Bot watches public company job boards for early-career SWE, Quant, and AI/ML listings, then posts eligible roles to a Discord server. It is a small, single-process Python application: a JSON registry says what to scan and SQLite remembers complete board snapshots, scheduling state, and a durable delivery outbox.

`SPEC.md` is the concise behavioural reference. `AGENTS.md` is the maintenance contract for contributors and coding agents.

## How it works

```text
companies.json -> provider adapters -> classify -> SQLite snapshot + outbox -> Discord
       ^                                  |                  |
discovery.json -> discover + validate ----+          status / backup / recovery
```

Each board is fetched as a complete snapshot. Eligible unseen roles are queued before Discord is contacted. A delivery stays in the outbox until it is confirmed sent; an ambiguous request is reconciled from Discord history before it is ever retried. Debug and production have separate delivery history, so a preview never consumes a real post.

## Local setup and safe first check

Python 3.12 or newer is required. Create a virtual environment, install the project, and make a local configuration file:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

Fill in the Discord token and IDs in `.env`, keeping `DEBUG=true` for the first run. Then validate files and settings without starting Discord or making a network request:

```bash
make check-config
# equivalent: .venv/bin/jobbot check-config
```

`scan` and `run` authenticate with Discord. The first live scan in debug mode posts only to `TEST_CHANNEL`, with the intended production destination shown in each preview.

## Routine commands

```bash
jobbot check-config             # parse .env and both JSON configuration files
jobbot scan                     # one scan; sends at most MAX_SEND queued jobs
jobbot run                      # scan and discover continuously
jobbot status                   # JSON: backlog, scans, failures, scheduling, discovery issues
jobbot discover                 # find and validate additional public boards; does not post
jobbot backup data/backups/jobs-$(date -u +%Y%m%dT%H%M%SZ).sqlite3
```

`make install`, `make lint`, `make test`, and `make check-config` provide the same repeatable developer workflow. `check-config` requires Discord values to be present, but performs no Discord network check.

## Configuration

All supported settings and their defaults are annotated in `.env.example`. `DEBUG=true` keeps delivery history in debug mode and routes all posts to `TEST_CHANNEL`; production uses category channels and `POSTING_CHANNEL` summaries. `DATABASE_PATH`, `COMPANY_REGISTRY`, and `DISCOVERY_SOURCES` are local paths. Adaptive scheduling is enabled by default; `ACTIVE_SCAN_INTERVAL_SECONDS` takes precedence over legacy `SCAN_INTERVAL_SECONDS`, while `ADAPTIVE_SCHEDULING=false` fetches every enabled board on every loop. `PRIORITY_BOARDS` pins specific board keys. `HTTP_CONCURRENCY` and `HTTP_HOST_INTERVAL_SECONDS` limit outbound requests.

Never commit `.env`, Discord tokens, IDs intended to remain private, or database backups containing operational history.

## Registry and discovery

`config/companies.json` is a JSON list of company records. Manual entries need a supported `provider` (`ashby`, `greenhouse`, `lever`, or `jsonld`), a `board`, and a `region` of `global` or `eu`. Optional `overrides` can contain only `kind`, `experience`, and `exclude`. Run `jobbot check-config` after each edit.

`config/discovery.json` is a JSON list of public source URLs. `jobbot discover` extracts supported boards, validates them before adding them to SQLite, and records failures for `jobbot status`. Manual registry sync does not erase discovered boards; removing a manual entry disables it and retains its history.

## Docker

The Compose workflow is unchanged:

```bash
docker compose up -d --build
docker compose logs -f
docker compose exec jobbot jobbot status
docker compose down
```

Keep `.env`, `config/`, and `data/` available to the Compose project as configured in `compose.yaml`. Use `docker compose run --rm jobbot jobbot check-config` before a first deployment if desired.

## Backup and recovery

Use `jobbot backup DESTINATION` rather than copying a live SQLite file: it includes data in SQLite's WAL safely. Store backups outside the working tree and verify one by opening it with `jobbot status` through a temporary `DATABASE_PATH`.

After an interrupted process, start the bot normally. It reconciles deliveries left in `sending` state before retrying them, and recovers pending scan summaries. Do not delete the database merely to clear a queue: inspect `jobbot status`, correct a configuration or permission problem, and restart. A new database intentionally treats currently active eligible jobs as a first-run backlog.
