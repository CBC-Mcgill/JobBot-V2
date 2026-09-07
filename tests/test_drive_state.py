from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jobbot.drive_state import read_manifest, verify_current, write_next_manifest


def _write_database(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def test_manifest_promotes_database_and_retains_one_fallback(tmp_path: Path) -> None:
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    manifest = tmp_path / "current.json"
    _write_database(first, b"first")
    manifest.write_text(write_next_manifest(None, first))
    _write_database(second, b"second")

    next_manifest = write_next_manifest(manifest, second)
    parsed = json.loads(next_manifest)

    assert [item["sha256"] for item in parsed["objects"]] == [
        hashlib.sha256(b"second").hexdigest(),
        hashlib.sha256(b"first").hexdigest(),
    ]


def test_verify_current_rejects_incomplete_or_wrong_download(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite3"
    manifest = tmp_path / "current.json"
    _write_database(database, b"complete")
    manifest.write_text(write_next_manifest(None, database))

    verify_current(manifest, database)
    _write_database(database, b"truncated")
    with pytest.raises(ValueError, match="does not match"):
        verify_current(manifest, database)


def test_manifest_rejects_unexpected_remote_path(tmp_path: Path) -> None:
    manifest = tmp_path / "current.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "objects": [{"path": "../other.sqlite3", "sha256": "0" * 64, "size": 0}],
            }
        )
    )

    with pytest.raises(ValueError, match="Invalid Drive state object"):
        read_manifest(manifest)
