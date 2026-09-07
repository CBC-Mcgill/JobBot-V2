import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from jobbot.classify import classify
from jobbot.models import Snapshot
from jobbot.service import Service
from jobbot.store import Store

from .conftest import another_job, force_due


class Publisher:
    def __init__(self):
        self.messages = []
        self.remote = {}
        self.fail = False
        self.history_available = True

    async def send(self, delivery, channel_id):
        self.messages.append((delivery, channel_id))
        message_id = len(self.messages)
        self.remote[delivery["id"]] = message_id
        if self.fail:
            raise TimeoutError("Response lost after Discord accepted the message")
        return message_id

    async def find_sent(self, delivery):
        if not self.history_available:
            raise PermissionError("Cannot read history")
        return self.remote.get(delivery["id"])


def make_service(settings, store, jobs):
    providers = AsyncMock()
    providers.fetch_snapshot.return_value = Snapshot(jobs)
    publisher = Publisher()
    return Service(settings, store, providers, publisher), publisher, providers


async def test_first_run_cap_backlog_and_no_duplicate_posts(settings, store, job):
    jobs = [another_job(job, n) for n in range(5)]
    service, publisher, _ = make_service(settings, store, jobs)
    await service.scan()
    assert [m[0]["kind"] for m in publisher.messages] == ["job", "job", "summary"]
    assert store.backlog("debug") == 3
    assert all(channel == settings.test_channel for _, channel in publisher.messages)
    await service.scan()
    await service.scan()
    assert store.backlog("debug") == 0
    messages = len(publisher.messages)
    await service.scan()
    assert len(publisher.messages) == messages
    posted = [m[0]["identity"] for m in publisher.messages if m[0]["kind"] == "job"]
    assert len(posted) == len(set(posted)) == 5


async def test_debug_does_not_consume_production_history(settings, store, job):
    service, publisher, _ = make_service(settings, store, [job])
    await service.scan()
    production = replace(settings, debug=False)
    service2, publisher2, _ = make_service(production, store, [job])
    await service2.scan()
    assert len(publisher.messages) == len(publisher2.messages) == 2
    assert publisher2.messages[0][1] == production.channels["SWE_Intern"]
    assert publisher2.messages[1][1] == production.posting_channel


async def test_restart_recovers_accepted_job_without_resending(settings, store, job):
    service, publisher, _ = make_service(settings, store, [job])
    publisher.fail = True
    await service.scan()
    assert len(store.uncertain("debug")) == 1
    assert len(publisher.messages) == 1
    # New SQLite connection simulates a fresh process with persisted outbox state.
    reopened = Store(settings.database_path)
    try:
        publisher.fail = False
        service.store = reopened
        await service.scan()
        assert len(publisher.messages) == 2
        assert publisher.messages[-1][0]["kind"] == "summary"
        assert reopened.backlog("debug") == 0
    finally:
        reopened.close()


async def test_inaccessible_history_does_not_trigger_duplicate(settings, store, job):
    service, publisher, _ = make_service(settings, store, [job])
    publisher.fail = True
    await service.scan()
    publisher.fail = False
    publisher.history_available = False
    await service.scan()
    assert len(publisher.messages) == 1
    assert store.backlog("debug") == 1


async def test_confirmed_absent_send_is_retried(settings, store, job):
    service, publisher, _ = make_service(settings, store, [job])
    publisher.fail = True
    await service.scan()
    publisher.remote.clear()
    publisher.fail = False
    await service.scan()
    assert [m[0]["kind"] for m in publisher.messages] == ["job", "job", "summary"]
    assert store.backlog("debug") == 0


async def test_closed_queued_job_cancelled_but_outage_does_not_close(settings, store, company, job):
    store.upsert_company(company)
    store.observe(company, [(job, classify(job))], "debug")
    service, publisher, providers = make_service(settings, store, [])
    force_due(store)
    await service.scan()
    assert publisher.messages == []
    assert store.backlog("debug") == 0
    # Reappearance before any delivery can queue the job again.
    store.observe(company, [(job, classify(job))], "debug")
    providers.fetch_snapshot.side_effect = TimeoutError("Board offline")
    force_due(store)
    await service.scan()
    assert publisher.messages[0][0]["kind"] == "job"
    assert store.connection.execute("SELECT active FROM jobs").fetchone()[0] == 1


async def test_reclassification_of_pending_jobs(settings, store, company, job):
    store.upsert_company(company)
    store.observe(company, [(job, classify(job))], "debug")
    senior = replace(job, title="Senior Software Engineer")
    service, publisher, _ = make_service(settings, store, [senior])
    force_due(store)
    await service.scan()
    assert publisher.messages == []
    assert store.backlog("debug") == 0


async def test_partial_outage_preserves_other_boards(settings, store, company, job):
    other = replace(company, board="other")
    settings.registry_path.write_text(json.dumps([company.to_dict(), other.to_dict()]))
    service, publisher, providers = make_service(settings, store, [])

    async def fetch(c, **kwargs):
        if c.key == other.key:
            raise TimeoutError()
        return Snapshot([job])

    providers.fetch_snapshot.side_effect = fetch
    await service.scan()
    assert len(publisher.messages) == 2
    row = store.connection.execute("SELECT boards_ok,boards_failed FROM scans").fetchone()
    assert tuple(row) == (1, 1)


async def test_no_overlapping_scan(settings, store, job):
    service, publisher, providers = make_service(settings, store, [job])
    async with service.scan_lock:
        await service.scan()
    providers.fetch_snapshot.assert_not_called()
    assert publisher.messages == []


def test_canonical_url_dedup_and_changed_url_no_repost(store, company, job):
    store.upsert_company(company)
    store.observe(company, [(job, classify(job))], "debug")
    delivery = store.pending("debug", "job")[0]
    store.sent(delivery["id"], 123)
    altered = replace(job, apply_url=job.apply_url + "?new_application=1")
    duplicate = replace(job, remote_id="duplicate", apply_url=job.apply_url + "?utm_source=list")
    store.observe(
        company, [(altered, classify(altered)), (duplicate, classify(duplicate))], "debug"
    )
    assert store.backlog("debug") == 0


def test_sqlite_backup_includes_wal(settings, store, company, job, tmp_path):
    store.upsert_company(company)
    store.observe(company, [(job, classify(job))], "debug")
    target = tmp_path / "backup.sqlite3"
    store.backup(target)
    backup = Store(target)
    try:
        assert backup.backlog("debug") == 1
    finally:
        backup.close()
    with pytest.raises(ValueError, match="already exists"):
        store.backup(target)
