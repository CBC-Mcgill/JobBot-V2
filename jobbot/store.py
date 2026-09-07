import json
import sqlite3
import uuid
from pathlib import Path

from .models import Classification, Company, Job, Snapshot, now
from .scheduling import SchedulingPolicy, after

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    key TEXT PRIMARY KEY, data TEXT NOT NULL, manual INTEGER NOT NULL DEFAULT 0,
    last_success TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    key TEXT PRIMARY KEY, company_key TEXT NOT NULL, identity TEXT NOT NULL,
    data TEXT NOT NULL, classification TEXT NOT NULL, route TEXT,
    active INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company_key);
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
    boards_ok INTEGER NOT NULL DEFAULT 0, boards_failed INTEGER NOT NULL DEFAULT 0,
    jobs_seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS deliveries (
    id TEXT PRIMARY KEY, mode TEXT NOT NULL, kind TEXT NOT NULL,
    identity TEXT NOT NULL, job_key TEXT, route TEXT, payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
    attempted_at TEXT, channel_id INTEGER, message_id INTEGER, sent_at TEXT,
    scan_id TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
    UNIQUE(mode, kind, identity)
);
CREATE INDEX IF NOT EXISTS delivery_queue ON deliveries(mode, kind, status, created_at);
CREATE TABLE IF NOT EXISTS discovery_issues (
    source TEXT PRIMARY KEY, error TEXT NOT NULL, last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

SCHEDULE_COLUMNS = {
    "next_scan_at": "TEXT NOT NULL DEFAULT ''",
    "scan_tier": "INTEGER NOT NULL DEFAULT 0",
    "last_qualifying_job_at": "TEXT",
    "last_snapshot_at": "TEXT",
    "last_snapshot_hash": "TEXT",
    # Zero means normal; a positive value is the priority board's maximum interval.
    "priority": "INTEGER NOT NULL DEFAULT 0",
    "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
    "etag": "TEXT",
    "last_modified": "TEXT",
}


class Store:
    """SQLite persistence for registry state, complete board snapshots, and the outbox.

    Public methods are intentionally the application's persistence contract.  A scan
    only changes a board through :meth:`observe`, which commits the snapshot and its
    delivery work together.
    """

    def __init__(self, path: Path):
        """Open and migrate the database in place, retaining all historical records."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA synchronous=FULL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self.connection.close()
            raise ValueError("Database was created by a newer version of the bot")
        self.connection.executescript(SCHEMA)
        if version < SCHEMA_VERSION:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                # A concurrent status reader may have completed the migration while we waited.
                if self.connection.execute("PRAGMA user_version").fetchone()[0] < 2:
                    for column, definition in SCHEDULE_COLUMNS.items():
                        self.connection.execute(
                            f"ALTER TABLE companies ADD COLUMN {column} {definition}"
                        )
                    self.connection.execute("UPDATE companies SET next_scan_at=?", (now(),))
                    self.connection.execute("CREATE INDEX companies_due ON companies(next_scan_at)")
                    self.connection.execute(
                        "CREATE INDEX deliveries_job ON deliveries(mode,kind,job_key)"
                    )
                    self.connection.execute("PRAGMA user_version = 2")
        self.policy = SchedulingPolicy()

    # Schema and migration -------------------------------------------------

    def close(self) -> None:
        self.connection.close()

    # Registry synchronization --------------------------------------------

    def upsert_company(self, company: Company, manual: bool = True) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO companies(key,data,manual,next_scan_at,priority) VALUES(?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET data=excluded.data, manual=excluded.manual
                WHERE excluded.manual=1 OR companies.manual=0""",
                (
                    company.key,
                    json.dumps(company.to_dict()),
                    int(manual),
                    now(),
                    self.policy.priority(company),
                ),
            )

    def companies(self, *, include_disabled: bool = False) -> list[Company]:
        companies = [
            Company(**json.loads(r[0]))
            for r in self.connection.execute("SELECT data FROM companies ORDER BY key")
        ]
        return [c for c in companies if c.enabled or include_disabled]

    # Board snapshots and scheduling --------------------------------------

    def configure_scheduling(self, policy: SchedulingPolicy) -> None:
        self.policy = policy
        timestamp = now()
        with self.connection:
            for row in self.connection.execute("SELECT * FROM companies").fetchall():
                priority = policy.priority(Company(**json.loads(row["data"])))
                tier = policy.cap_tier(row["scan_tier"], priority)
                if priority == row["priority"] and tier == row["scan_tier"]:
                    continue
                next_scan = row["next_scan_at"]
                if priority and not row["consecutive_failures"]:
                    next_scan = min(
                        next_scan, after(row["last_snapshot_at"] or timestamp, priority)
                    )
                self.connection.execute(
                    "UPDATE companies SET priority=?,scan_tier=?,next_scan_at=? WHERE key=?",
                    (priority, tier, next_scan, row["key"]),
                )

    def due_companies(self, timestamp: str | None = None) -> list[Company]:
        return [
            Company(**json.loads(row[0]))
            for row in self.connection.execute(
                """SELECT data FROM companies WHERE next_scan_at<=?
                AND json_extract(data,'$.enabled')=1 ORDER BY next_scan_at,key""",
                (timestamp or now(),),
            )
        ]

    def board_state(self, key: str) -> dict:
        """Return persisted scheduling and HTTP-validator state for one board."""
        row = self.connection.execute("SELECT * FROM companies WHERE key=?", (key,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown board: {key}")
        return dict(row)

    def snapshot_jobs(self, key: str) -> list[Job]:
        return [
            Job(**json.loads(row[0]))
            for row in self.connection.execute(
                "SELECT data FROM jobs WHERE company_key=? AND active=1 ORDER BY key", (key,)
            )
        ]

    def scheduling_status(self, *, adaptive: bool = True, timestamp: str | None = None) -> dict:
        timestamp = timestamp or now()
        rows = self.connection.execute(
            """SELECT key,next_scan_at,scan_tier,priority,consecutive_failures FROM companies
            WHERE json_extract(data,'$.enabled')=1 ORDER BY next_scan_at,key"""
        ).fetchall()
        return {
            "enabled": adaptive,
            "due_boards": sum(row["next_scan_at"] <= timestamp or not adaptive for row in rows),
            "boards_by_tier": [
                {
                    "tier": tier,
                    "interval_seconds": delay,
                    "boards": sum(row["scan_tier"] == tier for row in rows),
                }
                for tier, delay in enumerate(self.policy.tiers)
            ],
            "next_board": dict(rows[0]) if rows else None,
            "failed_boards": sum(row["consecutive_failures"] > 0 for row in rows),
            "priority_boards": sum(row["priority"] > 0 for row in rows),
        }

    def sync_registry(self, companies: list[Company]) -> None:
        """Apply manual registry changes without deleting jobs or delivery history."""
        keys = {c.key for c in companies}
        # Removing a manual registry entry disables it without deleting delivery history.
        for row in self.connection.execute(
            "SELECT key,data FROM companies WHERE manual=1"
        ).fetchall():
            if row["key"] not in keys:
                data = json.loads(row["data"])
                data["enabled"] = False
                self.upsert_company(Company(**data))
        for company in companies:
            self.upsert_company(company)
        self.configure_scheduling(self.policy)
        with self.connection:
            self.connection.execute(
                """UPDATE deliveries SET status='cancelled' WHERE kind='job' AND status='pending'
                AND job_key IN (SELECT j.key FROM jobs j JOIN companies c ON c.key=j.company_key
                WHERE json_extract(c.data,'$.enabled')=0)"""
            )

    def company_error(self, key: str, error: str) -> None:
        with self.connection:
            state = self.board_state(key)
            failures = state["consecutive_failures"] + 1
            self.connection.execute(
                """UPDATE companies SET last_error=?,consecutive_failures=?,next_scan_at=?
                WHERE key=?""",
                (error, failures, after(now(), self.policy.retry(failures)), key),
            )

    # Discovery metadata ---------------------------------------------------

    def discovery_issue(self, source: str, error: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO discovery_issues VALUES(?,?,?)", (source, error, now())
            )

    def metadata(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, value))

    def observe(
        self,
        company: Company,
        results: list[tuple[Job, Classification]],
        mode: str,
        snapshot: Snapshot | None = None,
    ) -> int:
        """Commit a complete snapshot, schedule, validators and outbox changes together."""
        timestamp = now()
        snapshot = snapshot or Snapshot([job for job, _ in results])
        with self.connection:
            state = self.board_state(company.key)
            if snapshot.jobs is None and not state["last_snapshot_hash"]:
                raise ValueError("HTTP 304 has no complete cached snapshot")
            known = self.connection.execute(
                "SELECT key,identity FROM jobs WHERE company_key=?", (company.key,)
            ).fetchall()
            known_keys = {row["key"] for row in known}
            known_identities = {row["identity"] for row in known}
            qualifying = 0
            self.connection.execute("UPDATE jobs SET active=0 WHERE company_key=?", (company.key,))
            for job, classification in results:
                if job.company_key != company.key:
                    raise ValueError("Snapshot contains a job from another board")
                route = classification.route if classification.eligible else None
                if route and job.key not in known_keys and job.identity not in known_identities:
                    qualifying += 1
                known_keys.add(job.key)
                known_identities.add(job.identity)
                self.connection.execute(
                    """INSERT INTO jobs VALUES(?,?,?,?,?,?,1,?,?)
                    ON CONFLICT(key) DO UPDATE SET identity=excluded.identity, data=excluded.data,
                    classification=excluded.classification, route=excluded.route, active=1,
                    last_seen=excluded.last_seen""",
                    (
                        job.key,
                        company.key,
                        job.identity,
                        json.dumps(job.to_dict()),
                        json.dumps(classification.reasons),
                        route,
                        timestamp,
                        timestamp,
                    ),
                )
                if not route:
                    continue
                self._queue_job(job, route, mode, timestamp)
                self.connection.execute(
                    """UPDATE deliveries SET route=?,payload=?,status='pending'
                    WHERE mode=? AND job_key=? AND kind='job'
                    AND status IN ('pending','cancelled')""",
                    (route, json.dumps(job.to_dict()), mode, job.key),
                )
            self.connection.execute(
                """UPDATE deliveries SET status='cancelled' WHERE kind='job' AND status='pending'
                AND job_key IN (SELECT key FROM jobs WHERE company_key=?
                AND (active=0 OR route IS NULL))""",
                (company.key,),
            )
            unchanged = snapshot.jobs is None
            if unchanged:
                qualifying = 0
            tier, interval = self.policy.success(
                state["scan_tier"], bool(qualifying), state["priority"]
            )
            self.connection.execute(
                """UPDATE companies SET last_success=?,last_error=NULL,consecutive_failures=0,
                scan_tier=?,next_scan_at=?,last_qualifying_job_at=?,last_snapshot_at=?,
                last_snapshot_hash=?,etag=?,last_modified=? WHERE key=?""",
                (
                    timestamp,
                    tier,
                    after(timestamp, interval),
                    timestamp if qualifying else state["last_qualifying_job_at"],
                    timestamp,
                    state["last_snapshot_hash"] if unchanged else snapshot.digest,
                    (snapshot.etag or state["etag"]) if unchanged else snapshot.etag,
                    (snapshot.last_modified or state["last_modified"])
                    if unchanged
                    else snapshot.last_modified,
                    company.key,
                ),
            )
        return qualifying

    # Delivery outbox ------------------------------------------------------

    def _queue_job(self, job: Job, route: str, mode: str, timestamp: str) -> None:
        # A provider ID remains delivered even if its application URL changes.
        existing = self.connection.execute(
            "SELECT id FROM deliveries WHERE mode=? AND kind='job' AND job_key=?",
            (mode, job.key),
        ).fetchone()
        if not existing:
            self.connection.execute(
                """INSERT OR IGNORE INTO deliveries
                (id,mode,kind,identity,job_key,route,payload,created_at)
                VALUES(?,?,'job',?,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    mode,
                    job.identity,
                    job.key,
                    route,
                    json.dumps(job.to_dict()),
                    timestamp,
                ),
            )

    def queue_active_jobs(self, mode: str) -> None:
        """Seed a mode's outbox from complete cached snapshots, including sleeping boards."""
        with self.connection:
            rows = self.connection.execute(
                """SELECT j.data,j.route FROM jobs j JOIN companies c ON c.key=j.company_key
                WHERE j.active=1 AND j.route IS NOT NULL AND json_extract(c.data,'$.enabled')=1
                AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.mode=? AND d.kind='job'
                AND d.job_key=j.key)
                AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.mode=? AND d.kind='job'
                AND d.identity=j.identity)""",
                (mode, mode),
            ).fetchall()
            timestamp = now()
            for row in rows:
                self._queue_job(Job(**json.loads(row["data"])), row["route"], mode, timestamp)

    def new_scan(self, mode: str) -> str:
        scan_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                "INSERT INTO scans(id,mode,started_at) VALUES(?,?,?)", (scan_id, mode, now())
            )
        return scan_id

    # Scan history ---------------------------------------------------------

    def scan_progress(self, scan_id: str, ok: int, failed: int, seen: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE scans SET boards_ok=?,boards_failed=?,jobs_seen=? WHERE id=?",
                (ok, failed, seen, scan_id),
            )

    def unfinished_scans(self, mode: str) -> list[str]:
        return [
            r[0]
            for r in self.connection.execute(
                "SELECT id FROM scans WHERE mode=? AND status='running' ORDER BY started_at",
                (mode,),
            )
        ]

    def finish_scan(self, scan_id: str, mode: str, backlog: int) -> bool:
        # Do not summarize an ambiguous send until it can be reconciled.
        if self.connection.execute(
            "SELECT 1 FROM deliveries WHERE scan_id=? AND status='sending'", (scan_id,)
        ).fetchone():
            return False
        counts = dict(
            self.connection.execute(
                """SELECT route,COUNT(*) FROM deliveries
            WHERE scan_id=? AND kind='job' AND status='sent' GROUP BY route""",
                (scan_id,),
            ).fetchall()
        )
        with self.connection:
            if counts:
                self.connection.execute(
                    """INSERT OR IGNORE INTO deliveries(id,mode,kind,identity,payload,created_at)
                    VALUES(?,?,'summary',?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        mode,
                        scan_id,
                        json.dumps({"counts": counts, "backlog": backlog}),
                        now(),
                    ),
                )
            self.connection.execute(
                "UPDATE scans SET status='finished',finished_at=? WHERE id=?", (now(), scan_id)
            )
        return True

    def pending(self, mode: str, kind: str, limit: int = 100) -> list[dict]:
        return [
            dict(r)
            for r in self.connection.execute(
                """SELECT * FROM deliveries WHERE mode=? AND kind=? AND status='pending'
            ORDER BY created_at,id LIMIT ?""",
                (mode, kind, limit),
            )
        ]

    def uncertain(self, mode: str) -> list[dict]:
        return [
            dict(r)
            for r in self.connection.execute(
                "SELECT * FROM deliveries WHERE mode=? AND status='sending' ORDER BY attempted_at",
                (mode,),
            )
        ]

    def begin_send(self, delivery_id: str, channel_id: int, scan_id: str | None) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE deliveries SET status='sending',attempted_at=?,channel_id=?,
                scan_id=?,attempts=attempts+1,last_error=NULL WHERE id=? AND status='pending'""",
                (now(), channel_id, scan_id, delivery_id),
            )

    def sent(self, delivery_id: str, message_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET status='sent',message_id=?,sent_at=?,last_error=NULL "
                "WHERE id=?",
                (message_id, now(), delivery_id),
            )

    def send_error(self, delivery_id: str, error: str, definitive: bool = False) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET last_error=?,status=? WHERE id=?",
                (error, "pending" if definitive else "sending", delivery_id),
            )

    def retry(self, delivery_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET status='pending',scan_id=NULL WHERE id=?", (delivery_id,)
            )

    def backlog(self, mode: str) -> int:
        return self.connection.execute(
            """SELECT COUNT(*) FROM deliveries
            WHERE mode=? AND kind='job' AND status IN ('pending','sending')""",
            (mode,),
        ).fetchone()[0]

    # Backup ---------------------------------------------------------------

    def backup(self, destination: Path) -> None:
        """Create a consistent SQLite backup, including data still in the WAL."""
        if destination.exists():
            raise ValueError("Backup destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(destination) as target:
            self.connection.backup(target)
