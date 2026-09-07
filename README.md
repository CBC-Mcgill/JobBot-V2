# Claude Builder Club Job Bot

This bot finds early-career technical jobs for the Claude Builder Club Discord and posts the ones worth sharing. It scans the public job boards behind **Ashby**, **Greenhouse**, and **Lever**, classifies clearly eligible internships and new-grad roles, then routes each post to the appropriate Discord channel.

The initial board list is built from the public SimplifyJobs new-grad and internship listings. The bot reads the individual company boards itself; it does not repost entries from SimplifyJobs.

## What it posts

Only roles with clear early-career evidence are eligible. The supported routes are:

| Role family | Internship channel | New-grad channel |
| --- | --- | --- |
| Software engineering | `SWE_INTERNS` | `SWE_NG` |
| Quantitative/trading | `QUANT_INTERNS` | `QUANT_NG` |
| AI / machine learning | `AI_INTERNS` | `AI_NG` |

The classifier deliberately ignores ambiguous, senior, and unrelated listings. Each Discord post includes the role, company, location, application link, and compensation when the board provides it.

## Why it is safe to run repeatedly

SQLite stores a complete snapshot for every successful board fetch and a durable delivery outbox. The bot records a post as `sending` before contacting Discord. If that request is interrupted or uncertain, it searches Discord history for its marker before trying again. That prevents duplicate posts across restarts.

Debug and production delivery histories are separate:

- `DEBUG=true` sends every preview to `TEST_CHANNEL` and never pings roles.
- `DEBUG=false` sends to the six route channels above and posts scan summaries in `POSTING_CHANNEL`.

## Configure locally

Requires Python 3.12+.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

Fill in `.env`. Use a private test channel and leave `DEBUG=true` for the first live scan. The Discord bot needs permission to view the configured channels, send messages, embed links, and read channel history (needed to reconcile uncertain sends).

Validate configuration without contacting Discord or any job board:

```bash
make check-config
```

Then make a safe first scan:

```bash
.venv/bin/jobbot scan
```

Useful commands:

```bash
jobbot scan                 # scan due boards once and drain up to MAX_SEND posts
jobbot run                  # run scans and discovery continuously
jobbot discover             # find and validate additional public boards; never posts
jobbot status               # inspect backlog, last scan, scheduling, and failures
jobbot backup backup.sqlite3
```

`make lint` and `make test` run the project checks. Never commit `.env`, the SQLite database, or backups.

## Board list and scheduling

`config/companies.json` is the checked-in board registry. It currently contains Ashby, Greenhouse, and Lever boards sourced from SimplifyJobs’ public new-grad and internship trackers. `config/discovery.json` lists the public source pages used to discover additions.

The bot fetches a whole board at once. A failed or incomplete response never replaces the previous snapshot. Quiet boards are scanned less often over time; a new eligible role makes the board active again. `ACTIVE_SCAN_INTERVAL_SECONDS`, `QUIET_TIERS_SECONDS`, and `PRIORITY_BOARDS` control that policy; see [`.env.example`](.env.example) for every supported setting.

To add a board manually, add a record to `config/companies.json` with a supported provider, board identifier, and `global` or `eu` region, then run `make check-config`. Removing a manual record disables that board but keeps its history. The bot also supports `jsonld` for manually registered career pages, though it is not one of the three current board sources.

## Deploy with GitHub Actions

Use a **self-hosted GitHub Actions runner** for production. GitHub-hosted runners are ephemeral, while this bot must retain its SQLite database between scans to avoid treating already-seen jobs as new. Keep the database outside the checked-out repository on the runner host, for example `/var/lib/claude-builder-club-jobbot/jobs.sqlite3`.

1. Install and label a Linux self-hosted runner for this repository, for example with the label `jobbot`. Ensure its runner user can create and write `/var/lib/claude-builder-club-jobbot`.
2. In the repository, add these **Actions secrets**: `DISCORD_TOKEN`, `SWE_INTERNS`, `SWE_NG`, `QUANT_INTERNS`, `QUANT_NG`, `AI_INTERNS`, `AI_NG`, `POSTING_CHANNEL`, `TEST_CHANNEL`, `ROLE_ID_LOVES_NOTIFICATIONS`, and `ROLE_ID_JOB_PING`.
3. Add `.github/workflows/jobbot.yml` with the workflow below. Start with `DEBUG=true`; after confirming posts in the test channel, change it to `false` in the workflow (or replace it with a non-secret Actions variable).
4. Run **Actions → Job bot → Run workflow** once. The first production scan may queue active eligible roles, so review the debug run before switching production on.

```yaml
name: Job bot

on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: claude-builder-club-jobbot
  cancel-in-progress: false

jobs:
  scan:
    runs-on: [self-hosted, linux, jobbot]
    timeout-minutes: 20
    env:
      DATABASE_PATH: /var/lib/claude-builder-club-jobbot/jobs.sqlite3
      COMPANY_REGISTRY: config/companies.json
      DISCOVERY_SOURCES: config/discovery.json
      DEBUG: "true"
      DISCORD_TOKEN: ${{ secrets.DISCORD_TOKEN }}
      SWE_INTERNS: ${{ secrets.SWE_INTERNS }}
      SWE_NG: ${{ secrets.SWE_NG }}
      QUANT_INTERNS: ${{ secrets.QUANT_INTERNS }}
      QUANT_NG: ${{ secrets.QUANT_NG }}
      AI_INTERNS: ${{ secrets.AI_INTERNS }}
      AI_NG: ${{ secrets.AI_NG }}
      POSTING_CHANNEL: ${{ secrets.POSTING_CHANNEL }}
      TEST_CHANNEL: ${{ secrets.TEST_CHANNEL }}
      ROLE_ID_LOVES_NOTIFICATIONS: ${{ secrets.ROLE_ID_LOVES_NOTIFICATIONS }}
      ROLE_ID_JOB_PING: ${{ secrets.ROLE_ID_JOB_PING }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install --upgrade pip
      - run: python -m pip install .
      - run: jobbot check-config
      - run: jobbot scan
```

The workflow intentionally runs `jobbot scan`, not `jobbot run`: Actions schedules each invocation, and the process exits after one scan. GitHub can delay scheduled workflows, so this is a best-effort 30-minute cadence rather than an exact timer. For a continuously running deployment, use the included Docker Compose setup instead.

## Docker deployment

For an always-on host, Docker Compose is simpler than a scheduled runner:

```bash
docker compose up -d --build
docker compose logs -f
docker compose exec jobbot jobbot status
```

Compose keeps the database in a named volume and reads the local `.env` plus `config/` directory. See `SPEC.md` for persistence, recovery, discovery, and scheduling behaviour.
