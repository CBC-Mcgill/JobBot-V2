"""Describe and verify immutable SQLite snapshots stored in a private Drive folder.

The workflow publishes the database before it commits this small manifest.  A
manifest therefore only points at a fully uploaded object, while retaining one
older object as a recovery fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_RETAINED_OBJECTS = 2


@dataclass(frozen=True)
class DriveObject:
    path: str
    sha256: str
    size: int

    def as_dict(self) -> dict[str, str | int]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


def file_digest(path: Path) -> tuple[str, int]:
    """Return the SHA-256 and byte length of *path* without loading it into memory."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _object_from_data(value: Any) -> DriveObject:
    if not isinstance(value, dict):
        raise ValueError("Drive state object must be an object")
    path = value.get("path")
    sha256 = value.get("sha256")
    size = value.get("size")
    if (
        not isinstance(path, str)
        or not path.startswith("jobs-")
        or not path.endswith(".sqlite3")
        or "/" in path
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
        or not isinstance(size, int)
        or size < 0
    ):
        raise ValueError("Invalid Drive state object")
    return DriveObject(path=path, sha256=sha256, size=size)


def read_manifest(path: Path) -> list[DriveObject]:
    """Read a checked-in manifest and return its current object first."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Cannot read Drive state manifest {path}") from error
    if data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("objects"), list):
        raise ValueError("Unsupported Drive state manifest")
    objects = [_object_from_data(value) for value in data["objects"]]
    if not objects or len(objects) > MAX_RETAINED_OBJECTS:
        raise ValueError("Drive state manifest must retain one or two objects")
    if len({item.path for item in objects}) != len(objects):
        raise ValueError("Drive state manifest has duplicate paths")
    return objects


def write_next_manifest(previous: Path | None, database: Path) -> str:
    """Build a manifest that promotes *database* while keeping one old object."""
    sha256, size = file_digest(database)
    current = DriveObject(path=f"jobs-{sha256}.sqlite3", sha256=sha256, size=size)
    retained = [current]
    if previous is not None and previous.exists():
        for item in read_manifest(previous):
            if item.path != current.path:
                retained.append(item)
            if len(retained) == MAX_RETAINED_OBJECTS:
                break
    return json.dumps(
        {"schema_version": SCHEMA_VERSION, "objects": [item.as_dict() for item in retained]},
        indent=2,
        sort_keys=True,
    ) + "\n"


def verify_current(manifest: Path, database: Path) -> None:
    """Raise ValueError unless *database* is the manifest's current complete object."""
    current = read_manifest(manifest)[0]
    sha256, size = file_digest(database)
    if (sha256, size) != (current.sha256, current.size):
        raise ValueError("Downloaded database does not match the Drive state manifest")


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    current_path = commands.add_parser("current-path")
    current_path.add_argument("manifest", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("database", type=Path)
    next_manifest = commands.add_parser("next-manifest")
    next_manifest.add_argument("previous", type=Path)
    next_manifest.add_argument("database", type=Path)
    retained = commands.add_parser("retained-paths")
    retained.add_argument("manifest", type=Path)
    args = parser.parse_args()

    if args.command == "current-path":
        print(read_manifest(args.manifest)[0].path)
    elif args.command == "verify":
        verify_current(args.manifest, args.database)
    elif args.command == "next-manifest":
        print(write_next_manifest(args.previous, args.database), end="")
    elif args.command == "retained-paths":
        print(*(item.path for item in read_manifest(args.manifest)), sep="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
