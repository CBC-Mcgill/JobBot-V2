import json
import os

import pytest

from jobbot.cli import process_lock
from jobbot.config import Settings, read_registry


def test_defaults_and_invalid_values(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key in {"DEBUG", "MAX_SEND", "DISCORD_TOKEN"}:
            monkeypatch.delenv(key)
    settings = Settings.load(str(tmp_path / "missing.env"), discord_required=False)
    assert settings.debug is True
    assert settings.max_send == 50
    monkeypatch.setenv("MAX_SEND", "-1")
    with pytest.raises(ValueError, match="MAX_SEND"):
        Settings.load(str(tmp_path / "missing.env"), discord_required=False)
    monkeypatch.setenv("MAX_SEND", "5")
    monkeypatch.setenv("DEBUG", "maybe")
    with pytest.raises(ValueError, match="DEBUG"):
        Settings.load(str(tmp_path / "missing.env"), discord_required=False)


def test_duplicate_registry_and_invalid_override(settings, company):
    settings.registry_path.write_text(json.dumps([company.to_dict(), company.to_dict()]))
    with pytest.raises(ValueError, match="Duplicate"):
        read_registry(settings.registry_path)
    item = company.to_dict()
    item["overrides"] = {"kind": "Recruiting"}
    settings.registry_path.write_text(json.dumps([item]))
    with pytest.raises(ValueError, match="override"):
        read_registry(settings.registry_path)


def test_process_lock_prevents_concurrent_delivery(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    with process_lock(path):
        with pytest.raises(ValueError, match="Another bot process"):
            with process_lock(path):
                pass


@pytest.fixture
def scheduling_env(monkeypatch, tmp_path):
    keys = (
        "ACTIVE_SCAN_INTERVAL_SECONDS",
        "SCAN_INTERVAL_SECONDS",
        "ADAPTIVE_SCHEDULING",
        "QUIET_TIERS_SECONDS",
        "PRIORITY_SCAN_INTERVAL_SECONDS",
        "PRIORITY_BOARDS",
        "FAILURE_RETRY_SECONDS",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    return str(tmp_path / "missing.env")


def test_adaptive_defaults_alias_and_precedence(scheduling_env, monkeypatch):
    settings = Settings.load(scheduling_env, discord_required=False)
    assert settings.adaptive_scheduling
    assert settings.scan_interval == settings.quiet_tiers[0] == 1800
    assert settings.quiet_tiers[-1] == 2592000
    assert settings.failure_retry == 1800
    assert settings.priority_scan_interval == 7200
    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "3600")
    assert Settings.load(scheduling_env, discord_required=False).scan_interval == 3600
    monkeypatch.setenv("ACTIVE_SCAN_INTERVAL_SECONDS", "1800")
    monkeypatch.setenv("ADAPTIVE_SCHEDULING", "false")
    monkeypatch.setenv("PRIORITY_BOARDS", "ashby:global:example=1800")
    settings = Settings.load(scheduling_env, discord_required=False)
    assert settings.scan_interval == 1800
    assert not settings.adaptive_scheduling
    assert settings.priority_boards == {"ashby:global:example": 1800}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("QUIET_TIERS_SECONDS", ""),
        ("QUIET_TIERS_SECONDS", "1800,1800"),
        ("QUIET_TIERS_SECONDS", "7200,1800"),
        ("QUIET_TIERS_SECONDS", "0,1800"),
        ("QUIET_TIERS_SECONDS", "-1,1800"),
        ("QUIET_TIERS_SECONDS", "1800,no"),
        ("ACTIVE_SCAN_INTERVAL_SECONDS", "0"),
        ("SCAN_INTERVAL_SECONDS", "-10"),
        ("PRIORITY_SCAN_INTERVAL_SECONDS", "0"),
        ("PRIORITY_SCAN_INTERVAL_SECONDS", "86400"),
        ("FAILURE_RETRY_SECONDS", "-1"),
        ("ADAPTIVE_SCHEDULING", "sometimes"),
        ("PRIORITY_BOARDS", "nvidia"),
    ],
)
def test_invalid_scheduling_settings(scheduling_env, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match=key):
        Settings.load(scheduling_env, discord_required=False)
