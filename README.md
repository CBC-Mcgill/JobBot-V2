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
