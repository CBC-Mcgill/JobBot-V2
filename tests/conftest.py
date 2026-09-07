import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from jobbot.config import CHANNEL_KEYS, Settings
from jobbot.models import Company, Job
from jobbot.store import Store


@pytest.fixture
def company():
    return Company("Example", "ashby", "example")


@pytest.fixture
def job(company):
    return Job(
        "ashby",
        company.key,
        "one",
        "Example",
        "Software Engineer Intern",
        "Build software with our engineering team.",
        "https://jobs.example/one",
        "https://jobs.example/one/apply",
        location="Toronto, Canada",
    )


@pytest.fixture
def settings(tmp_path, company):
    registry = tmp_path / "companies.json"
    registry.write_text(json.dumps([company.to_dict()]))
    discovery = tmp_path / "discovery.json"
    discovery.write_text("[]")
    return Settings(
        "test-token",
        {route: i + 100 for i, route in enumerate(CHANNEL_KEYS)},
        posting_channel=200,
        test_channel=300,
        roles=(400, 401),
        registry_path=registry,
        discovery_path=discovery,
        database_path=tmp_path / "jobs.sqlite3",
        max_send=2,
    )


@pytest.fixture
def store(settings):
    database = Store(settings.database_path)
    yield database
    database.close()


def another_job(job, number):
    return replace(
        job,
        remote_id=str(number),
        url=f"https://jobs.example/{number}",
        apply_url=f"https://jobs.example/{number}",
    )


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        value = datetime(2026, 9, 6, tzinfo=UTC)

        def now(self):
            return self.value.isoformat()

        def advance(self, seconds):
            self.value += timedelta(seconds=seconds)

        def set(self, timestamp):
            self.value = datetime.fromisoformat(timestamp)

    clock = Clock()
    for module in ("store", "service", "discovery"):
        monkeypatch.setattr(f"jobbot.{module}.now", clock.now)
    return clock


def force_due(store):
    with store.connection:
        store.connection.execute("UPDATE companies SET next_scan_at=''")
