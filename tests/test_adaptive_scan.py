import asyncio
import json
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from jobbot.classify import classify
from jobbot.discovery import Discovery
from jobbot.models import Snapshot
from jobbot.scheduling import DEFAULT_TIERS, after
from jobbot.service import Service

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
    assert store.board_state(company.key)["scan_tier"] == 6
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
        assert store.snapshot_jobs(company.key) == [job]
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
    assert store.snapshot_jobs(company.key) == [job]
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
    assert [t["boards"] for t in status["boards_by_tier"]] == [2, 1, 0, 0, 0, 0, 0]
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
