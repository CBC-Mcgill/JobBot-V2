import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from jobbot.classify import classify
from jobbot.discovery import Discovery
from jobbot.models import Snapshot
from jobbot.scheduling import DEFAULT_TIERS, after
from jobbot.service import Service
from jobbot.store import VACUUM_ROWS, Store

from .conftest import another_job
from .test_delivery import Publisher, make_service


async def test_first_run_checks_all_then_only_due_boards(settings, store, company, job, clock):
    quiet = replace(company, name="Quiet", board="quiet")
    settings.registry_path.write_text(json.dumps([company.to_dict(), quiet.to_dict()]))
    service, publisher, providers = make_service(settings, store, [])
    providers.fetch_snapshot.side_effect = lambda c, **kw: Snapshot([job] if c == company else [])
    await service.scan()
    assert providers.fetch_snapshot.call_count == 2
    assert len(publisher.messages) == 2
    assert store.board_state(company.key)["scan_tier"] == 0
    quiet_state = store.board_state(quiet.key)
    assert quiet_state["scan_tier"] == 1
    await service.scan()
    assert providers.fetch_snapshot.call_count == 2
    clock.advance(1800)
    await service.scan()
    assert providers.fetch_snapshot.call_count == 3
    assert providers.fetch_snapshot.call_args.args[0] == company
    assert store.board_state(quiet.key) == quiet_state
    assert len(publisher.messages) == 2


async def test_all_quiet_tiers_new_unrelated_and_qualifying_jobs(
    settings, store, company, job, clock
):
    service, publisher, providers = make_service(settings, store, [])
    for tier, delay in enumerate(DEFAULT_TIERS[1:], 1):
        started = clock.now()
        await service.scan()
        state = store.board_state(company.key)
        assert state["scan_tier"] == tier
        assert state["next_scan_at"] == after(started, delay)
        clock.set(state["next_scan_at"])
    unrelated = replace(job, title="Marketing Intern")
    providers.fetch_snapshot.return_value = Snapshot([unrelated])
    await service.scan()
    assert store.board_state(company.key)["scan_tier"] == len(DEFAULT_TIERS) - 1
    assert publisher.messages == []
    clock.set(store.board_state(company.key)["next_scan_at"])
    providers.fetch_snapshot.return_value = Snapshot([unrelated, another_job(job, 2)])
    started = clock.now()
    await service.scan()
    state = store.board_state(company.key)
    assert state["scan_tier"] == 0
    assert state["last_qualifying_job_at"] == started
    assert state["next_scan_at"] == after(started, 1800)
    assert len(publisher.messages) == 2
    clock.advance(1800)
    await service.scan()
    assert len(publisher.messages) == 2
    assert store.board_state(company.key)["scan_tier"] == 1


async def test_failure_preserves_snapshot_jobs_and_quiet_tier(settings, store, company, job, clock):
    service, _, providers = make_service(settings, store, [job])
    await service.scan()
    clock.advance(1800)
    await service.scan()
    previous = store.board_state(company.key)
    for failure, interval in [(1, 1800), (2, 3600)]:
        clock.set(store.board_state(company.key)["next_scan_at"])
        started = clock.now()
        providers.fetch_snapshot.side_effect = TimeoutError()
        await service.scan()
        state = store.board_state(company.key)
        assert state["consecutive_failures"] == failure
        assert state["next_scan_at"] == after(started, interval)
        for key in (
            "scan_tier",
            "last_snapshot_at",
            "last_snapshot_hash",
            "last_qualifying_job_at",
        ):
            assert state[key] == previous[key]
        assert store.snapshot_jobs(company.key) == [replace(job, description="")]
    clock.set(state["next_scan_at"])
    providers.fetch_snapshot.side_effect = None
    providers.fetch_snapshot.return_value = Snapshot([])
    await service.scan()
    state = store.board_state(company.key)
    assert state["consecutive_failures"] == 0
    assert state["last_error"] is None
    assert state["scan_tier"] == previous["scan_tier"] + 1
    assert store.snapshot_jobs(company.key) == []


async def test_304_retains_jobs_and_validators_and_mode_separation(
    settings, store, company, job, clock
):
    service, _, providers = make_service(settings, store, [job])
    modified = "Sun, 06 Sep 2026 00:00:00 GMT"
    providers.fetch_snapshot.return_value = Snapshot([job], '"v1"', modified)
    await service.scan()
    initial = store.board_state(company.key)
    clock.advance(1800)
    providers.fetch_snapshot.return_value = Snapshot(None)
    service = Service(replace(settings, debug=False), store, providers, Publisher())
    await service.scan()
    providers.fetch_snapshot.assert_awaited_with(company, etag='"v1"', last_modified=modified)
    assert len(service.publisher.messages) == 2
    state = store.board_state(company.key)
    assert state["scan_tier"] == 1
    for key in ("last_snapshot_hash", "etag", "last_modified", "last_qualifying_job_at"):
        assert state[key] == initial[key]
    assert state["last_snapshot_at"] == clock.now()
    assert store.snapshot_jobs(company.key) == [replace(job, description="")]
    clock.set(state["next_scan_at"])
    providers.fetch_snapshot.return_value = Snapshot([job])
    await service.scan()
    assert store.board_state(company.key)["etag"] is None
    assert store.board_state(company.key)["last_modified"] is None
    assert len(service.publisher.messages) == 2


async def test_uncached_304_is_failure(settings, store, company, clock):
    service, _, providers = make_service(settings, store, [])
    providers.fetch_snapshot.return_value = Snapshot(None, '"unexpected"')
    await service.scan()
    state = store.board_state(company.key)
    assert state["scan_tier"] == 0
    assert state["consecutive_failures"] == 1
    assert state["last_snapshot_at"] is None
    assert state["etag"] is None


@pytest.mark.parametrize("interval", [1800, 7200])
async def test_priority_stays_hot(settings, store, company, clock, interval):
    settings = replace(settings, priority_boards={company.key: interval})
    service, _, _ = make_service(settings, store, [])
    for _ in range(8):
        started = clock.now()
        await service.scan()
        state = store.board_state(company.key)
        assert state["priority"] == interval
        assert state["next_scan_at"] == after(started, interval)
        clock.set(state["next_scan_at"])


async def test_discovery_adds_immediately_due_without_revalidating_disabled(
    settings, store, company, clock
):
    store.upsert_company(replace(company, enabled=False))
    discovered = replace(company, board="new")
    providers = AsyncMock()
    providers.fetch.return_value = []
    providers.fetch_snapshot.return_value = Snapshot([])
    discovery = Discovery(AsyncMock(), providers, store)
    discovery.candidates = AsyncMock(return_value=[company, discovered])
    assert await discovery.run(settings.discovery_path, []) == [
        replace(discovered, verified_at=clock.now())
    ]
    providers.fetch.assert_awaited_once_with(discovered)
    assert store.board_state(discovered.key)["scan_tier"] == 0
    assert [c.key for c in store.due_companies()] == [discovered.key]
    settings.registry_path.write_text("[]")
    service = Service(settings, store, providers, Publisher(), discovery)
    await service.scan()
    await service.scan()
    assert providers.fetch_snapshot.call_count == 1
    assert store.board_state(discovered.key)["scan_tier"] == 1


async def test_scheduler_skips_unchanged_registry_and_keeps_manual_additions_immediate(
    settings, store, company, clock, monkeypatch
):
    service, _, providers = make_service(settings, store, [])
    await service.scan()
    sync = Mock(wraps=store.sync_registry)
    monkeypatch.setattr(store, "sync_registry", sync)
    await service.scan()
    sync.assert_not_called()
    added = replace(company, board="added")
    settings.registry_path.write_text(json.dumps([company.to_dict(), added.to_dict()]))
    await service.scan()
    sync.assert_called_once()
    assert providers.fetch_snapshot.call_count == 2
    assert providers.fetch_snapshot.call_args.args[0] == added


async def test_adaptive_can_be_disabled(settings, store, clock):
    service, _, providers = make_service(replace(settings, adaptive_scheduling=False), store, [])
    await service.scan()
    await service.scan()
    assert providers.fetch_snapshot.call_count == 2


async def test_run_uses_scheduler_interval(settings, store, monkeypatch):
    service, _, _ = make_service(settings, store, [])
    stop = asyncio.Event()
    waits = []

    async def wait_for(awaitable, timeout):
        awaitable.close()
        waits.append(timeout)
        stop.set()

    monkeypatch.setattr("jobbot.service.asyncio.wait_for", wait_for)
    service.scan = AsyncMock()
    await service.run(stop)
    service.scan.assert_awaited_once()
    assert waits == [1800]


def test_snapshot_and_outbox_roll_back_together(store, company, job, clock):
    store.upsert_company(company)
    previous = store.board_state(company.key)
    store.connection.execute(
        """CREATE TRIGGER reject_schedule BEFORE UPDATE OF next_scan_at ON companies
        BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="simulated write failure"):
        store.observe(company, [(job, classify(job))], "debug", Snapshot([job], '"v1"'))
    assert store.board_state(company.key) == previous
    assert store.snapshot_jobs(company.key) == []
    assert store.backlog("debug") == 0


def test_status_matches_due_queue_and_excludes_disabled(store, company, clock):
    store.upsert_company(company)
    quiet = replace(company, board="quiet")
    failing = replace(company, board="failing")
    store.upsert_company(quiet)
    store.upsert_company(failing)
    store.upsert_company(replace(company, board="disabled", enabled=False))
    store.observe(quiet, [], "debug")
    store.company_error(failing.key, "TimeoutError")
    status = store.scheduling_status()
    assert status["due_boards"] == 1
    assert status["failed_boards"] == 1
    assert status["next_board"]["key"] == company.key
    assert [t["boards"] for t in status["boards_by_tier"]] == [2, 1] + [0] * (
        len(DEFAULT_TIERS) - 2
    )
    assert store.scheduling_status(adaptive=False)["due_boards"] == 3
    clock.advance(1800)
    assert store.scheduling_status()["due_boards"] == 2


def test_reappearance_and_duplicate_identity_do_not_reset_tiers(store, company, job, clock):
    store.upsert_company(company)
    store.observe(company, [(job, classify(job))], "debug")
    store.observe(company, [], "debug")
    duplicate = replace(job, remote_id="duplicate")
    store.observe(company, [(job, classify(job)), (duplicate, classify(duplicate))], "debug")
    assert store.board_state(company.key)["scan_tier"] == 2
    assert store.backlog("debug") == 1


async def test_invalid_provider_response_preserves_cached_snapshot(settings, store, company, clock):
    import httpx

    from .test_providers import FIXTURES, api

    fixture = json.loads((FIXTURES / "ashby.json").read_text())
    responses = [
        httpx.Response(200, json=fixture, headers={"ETag": '"valid"'}),
        httpx.Response(200, json={"jobs": [], "error": "unavailable"}, headers={"ETag": '"bad"'}),
    ]
    providers = api(lambda r: responses.pop(0))
    service = Service(settings, store, providers, Publisher())
    try:
        await service.scan()
        previous = store.board_state(company.key)
        jobs = store.snapshot_jobs(company.key)
        clock.set(previous["next_scan_at"])
        await service.scan()
        state = store.board_state(company.key)
        assert state["consecutive_failures"] == 1
        assert state["etag"] == '"valid"'
        assert state["last_snapshot_hash"] == previous["last_snapshot_hash"]
        assert state["last_snapshot_at"] == previous["last_snapshot_at"]
        assert store.snapshot_jobs(company.key) == jobs
    finally:
        await providers.http.close()


async def test_descriptions_are_not_persisted(settings, store, company, job, clock):
    service, _, _ = make_service(settings, store, [job])
    await service.scan()
    stored = store.connection.execute(
        "SELECT data FROM jobs WHERE company_key=?", (company.key,)
    ).fetchone()[0]
    assert job.description not in stored
    assert json.loads(stored)["description"] == ""
    queued = store.connection.execute("SELECT payload FROM deliveries").fetchone()
    assert queued is None or job.description not in queued[0]


async def test_304_keeps_a_verdict_that_only_the_description_earned(
    settings, store, company, job, clock
):
    """Eligibility won on description text must survive a 304, which has no description
    to re-read. The recorded verdict is replayed instead of being derived again."""
    described = replace(
        job,
        title="Software Engineer",
        description="We are seeking new graduates for this role.",
    )
    service, _, providers = make_service(settings, store, [described])
    await service.scan()
    assert store.board_state(company.key)["last_snapshot_hash"]
    route = store.connection.execute("SELECT route FROM jobs").fetchone()[0]
    assert route == "SWE_New Grad"

    clock.set(store.board_state(company.key)["next_scan_at"])
    providers.fetch_snapshot.return_value = Snapshot(None, '"v1"', None)
    await service.scan()
    assert store.connection.execute("SELECT route FROM jobs").fetchone()[0] == route


async def test_prune_drops_history_but_never_the_dedup_record(settings, store, company, job, clock):
    service, _, providers = make_service(settings, store, [job])
    await service.scan()
    providers.fetch_snapshot.return_value = Snapshot([])
    clock.set(store.board_state(company.key)["next_scan_at"])
    await service.scan()

    def count(table, where="1"):
        return store.connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]

    assert (count("jobs"), count("deliveries", "kind='job'")) == (1, 1)
    assert store.prune(days=0) >= 1
    assert count("jobs") == 0
    assert count("deliveries", "kind='job'") == 1, "a sent delivery is what stops a repost"
    # The scan that just ran is not yet past the cutoff; the earlier one is.
    assert count("scans") == 1


async def test_prune_keeps_jobs_still_on_the_board(settings, store, company, job, clock):
    service, _, _ = make_service(settings, store, [job])
    await service.scan()
    store.prune(days=0)
    assert store.snapshot_jobs(company.key) == [replace(job, description="")]


async def test_304_reapplies_company_exclusion(settings, store, company, job, clock):
    """A board that never changes answers 304 forever, so a cached verdict must not
    outlive the registry. Excluding a company has to take effect on the next scan."""
    service, publisher, providers = make_service(settings, store, [job])
    await service.scan()
    assert [m["kind"] for m, _ in publisher.messages].count("job") == 1

    excluded = replace(company, overrides={"exclude": True})
    settings.registry_path.write_text(json.dumps([excluded.to_dict()]))
    service._registry_signature = None
    providers.fetch_snapshot.return_value = Snapshot(None, '"v1"', None)
    clock.set(store.board_state(company.key)["next_scan_at"])
    await service.scan()

    assert store.connection.execute("SELECT route FROM jobs").fetchone()[0] is None
    assert store.connection.execute(
        "SELECT status FROM deliveries WHERE kind='job'"
    ).fetchone()[0] == "sent"


async def test_304_reapplies_staleness_instead_of_requeuing_forever(
    settings, store, company, job, clock
):
    """A cached route must not resurrect a delivery that went stale, or the zombie
    occupies a MAX_SEND slot on every later scan and starves fresh jobs."""
    # is_stale reads the wall clock, not the fixture clock, so age the stored row.
    fresh = replace(job, published_at=datetime.now(UTC).isoformat())
    service, _, providers = make_service(settings, store, [fresh])
    await service.scan()
    aged = json.dumps({**fresh.stored(), "published_at": "2020-01-01T00:00:00+00:00"})
    with store.connection:
        store.connection.execute("UPDATE deliveries SET status='pending' WHERE kind='job'")
        store.connection.execute("UPDATE jobs SET data=?", (aged,))

    clock.set(store.board_state(company.key)["next_scan_at"])
    providers.fetch_snapshot.return_value = Snapshot(None, '"v1"', None)
    await service.scan()
    # A surviving route is what resurrects the delivery to pending on every later 304,
    # so it keeps consuming a MAX_SEND slot ahead of fresher jobs without ever posting.
    assert store.connection.execute("SELECT route FROM jobs").fetchone()[0] is None
    assert store.connection.execute(
        "SELECT status FROM deliveries WHERE kind='job'"
    ).fetchone()[0] == "cancelled"
    assert store.backlog(settings.mode) == 0


def test_prune_returns_space_to_the_uploaded_file(tmp_path, company, clock):
    """Deleting rows alone does not shrink the file that gets uploaded to Drive."""
    path = tmp_path / "vacuum.sqlite3"
    database = Store(path)
    database.upsert_company(company)
    with database.connection:
        database.connection.executemany(
            "INSERT INTO jobs VALUES(?,?,?,?,'[]',NULL,0,'2000-01-01','2000-01-01')",
            [(str(i), company.key, str(i), "x" * 4000) for i in range(VACUUM_ROWS + 1)],
        )
    database.connection.execute("VACUUM")
    database.close()
    before = path.stat().st_size

    database = Store(path)
    assert database.prune(days=1) == VACUUM_ROWS + 1
    database.close()
    assert path.stat().st_size < before / 2


def test_retiring_one_scan_row_does_not_rewrite_the_file(tmp_path, company, clock):
    # The steady state is roughly one scan row per scan. Vacuuming for that would
    # rewrite the whole database every 30 minutes.
    database = Store(tmp_path / "quiet.sqlite3")
    with database.connection:
        database.connection.execute(
            "INSERT INTO scans(id,mode,started_at,status) VALUES('x','debug',?,'finished')",
            ("2000-01-01T00:00:00+00:00",),
        )
    assert database.prune(days=1) == 1
    assert database.connection.execute("PRAGMA freelist_count").fetchone()[0] >= 0
    database.close()
