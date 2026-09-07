# Behavioural reference

This reference describes the user-visible promises of Been Job Bot. Setup and routine operations are in `README.md`; implementation ownership is in `AGENTS.md`.

## Listings and messages

The bot reads enabled Ashby, Greenhouse, Lever, and JSON-LD career boards from the registry. It conservatively selects explicit early-career SWE, Quant, and AI/ML roles and routes each eligible listing to its category and experience channel. It renders one Discord embed per listing with application links and available compensation.

In production, a scan summary is posted to `POSTING_CHANNEL` and can mention the two configured notification roles. In debug mode every message goes to `TEST_CHANNEL`, shows the production destination it would have used, and does not ping roles.

## Durable delivery

Each listing has a persistent identity and delivery history. Debug and production use separate histories. The bot records an item as sending before the Discord request; if the result is uncertain, it searches Discord history for its durable marker before retrying. This prevents duplicate posts across restarts. `MAX_SEND` limits job posts per scan; queued work drains on later scans even if no board is due.

## Snapshots and scheduling

Every successful provider response is parsed as a complete snapshot. A changed board updates jobs, qualification, and pending delivery state together. An HTTP 304 reuses the prior complete snapshot. Failed, invalid, or incomplete responses preserve the previous snapshot and validators.

With adaptive scheduling enabled, quiet boards progress through the configured `QUIET_TIERS_SECONDS` intervals (defaults: 30 minutes through 3 days). A genuinely new qualifying listing resets a board to the first tier. No tier may exceed half the seven-day publication-freshness window, so a sleeping board is always re-checked while its listings are still deliverable. Tiers configured beyond that ceiling are dropped. Failures retry with bounded backoff. Priority boards use their configured interval; Nvidia and Amazon are pinned only when actually present in the registry. `ADAPTIVE_SCHEDULING=false` fetches all enabled boards each loop. `ACTIVE_SCAN_INTERVAL_SECONDS` overrides the legacy `SCAN_INTERVAL_SECONDS` alias.

## Registry, discovery, and persistence

The manual registry is synchronized when its file changes. Removing a manual board disables it while retaining history; discovered boards and history are never deleted by that sync. Discovery reads a list of public source URLs, extracts supported boards, validates each one, and records failures for status reporting.

SQLite databases created by schema v1 migrate in place to v2 without losing jobs, deliveries, scans, or discovery records. `jobbot backup` uses SQLite's backup API so a live WAL database is captured consistently.

When scheduled through GitHub Actions, the database payload is stored as a checksum-verified immutable
object in a private Google Drive folder. Git tracks a manifest for the current object and one fallback;
the workflow uploads an object before committing its manifest, so an interrupted publish leaves the last
committed state available.
