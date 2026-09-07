import json
import sqlite3

import pytest

from jobbot.models import Snapshot
from jobbot.store import SCHEMA, Store


def test_v1_migration_preserves_all_history_and_modes(tmp_path, company, job, clock):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as legacy:
        legacy.executescript(SCHEMA + "PRAGMA user_version=1;")
        legacy.execute(
            "INSERT INTO companies VALUES(?,?,1,?,NULL)",
            (company.key, json.dumps(company.to_dict()), clock.now()),
        )
        legacy.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,1,?,?)",
            (
                job.key,
                company.key,
                job.identity,
                json.dumps(job.to_dict()),
                "[]",
                "SWE_Intern",
                clock.now(),
                clock.now(),
            ),
        )
        legacy.execute(
            "INSERT INTO scans(id,mode,started_at) VALUES('scan','debug',?)", (clock.now(),)
        )
        for mode, status in [("debug", "sent"), ("production", "pending")]:
            legacy.execute(
                """INSERT INTO deliveries(id,mode,kind,identity,job_key,route,payload,status,
                created_at,scan_id) VALUES(?,?,'job',?,?,?,?,?,?,'scan')""",
                (
                    mode,
                    mode,
                    job.identity,
                    job.key,
                    "SWE_Intern",
                    json.dumps(job.to_dict()),
                    status,
                    clock.now(),
                ),
            )
        legacy.execute("INSERT INTO metadata VALUES('last_discovery',?)", (clock.now(),))
        legacy.execute("INSERT INTO discovery_issues VALUES('source','error',?)", (clock.now(),))
        tables = ("jobs", "deliveries", "scans", "metadata", "discovery_issues")
        before = {table: legacy.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    store = Store(path)
    try:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert store.due_companies() == [company]
        state = store.board_state(company.key)
        assert state["scan_tier"] == state["consecutive_failures"] == 0
        assert state["last_snapshot_at"] is state["last_snapshot_hash"] is None
        assert state["etag"] is state["last_modified"] is None
        for table in tables:
            assert [tuple(r) for r in store.connection.execute(f"SELECT * FROM {table}")] == before[
                table
            ]
        assert store.backlog("debug") == 0
        assert store.backlog("production") == 1
        store.observe(company, [], "debug", Snapshot([]))
        scheduled = store.board_state(company.key)
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert reopened.board_state(company.key) == scheduled
    finally:
        reopened.close()


def test_future_schema_is_rejected(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="newer version"):
        Store(path)
