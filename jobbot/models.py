"""Shared immutable-ish records and stable identifiers used across the bot."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_url(url: str) -> str:
    """Normalize application URLs for durable cross-provider deduplication."""
    parts = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"source", "ref", "referrer", "lever-source", "gh_src"}
    ]
    path = parts.path.rstrip("/")
    if parts.hostname and parts.hostname.endswith(("ashbyhq.com", "lever.co")):
        path = path.removesuffix("/apply").removesuffix("/application")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), "")
    )


@dataclass(frozen=True)
class Company:
    name: str
    provider: str
    board: str
    region: str = "global"
    career_url: str = ""
    overrides: dict | None = None
    provenance: str = "manual"
    verified_at: str | None = None
    enabled: bool = True

    def __post_init__(self):
        # Discovery lifts names from third-party tables. Padding widens the embed's bold
        # run and defeats the priority-board name match, which compares casefolded names.
        object.__setattr__(self, "name", self.name.strip())

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.region}:{self.board}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Job:
    provider: str
    company_key: str
    remote_id: str
    company: str
    title: str
    description: str
    url: str
    apply_url: str
    location: str = "Not specified"
    workplace: str = ""
    employment_type: str = ""
    published_at: str | None = None
    compensation: str | None = None
    department: str = ""

    @property
    def key(self) -> str:
        return f"{self.company_key}:{self.remote_id}"

    @property
    def identity(self) -> str:
        return canonical_url(self.apply_url or self.url)

    def to_dict(self) -> dict:
        return asdict(self)

    def stored(self) -> dict:
        # Only classify() reads the description and no embed renders it, so persisting it
        # would ship megabytes of dead text to Drive on every scan. The key is kept so
        # Job(**stored) still round-trips.
        return {**asdict(self), "description": ""}


@dataclass(frozen=True)
class Classification:
    kind: str | None
    experience: str | None
    reasons: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return bool(self.kind and self.experience)

    @property
    def route(self) -> str:
        return f"{self.kind}_{self.experience}"


@dataclass(frozen=True)
class Snapshot:
    # None means HTTP 304; an empty list is a complete, empty board.
    jobs: list[Job] | None
    etag: str | None = None
    last_modified: str | None = None

    @property
    def digest(self) -> str | None:
        if self.jobs is None:
            return None
        data = [job.to_dict() for job in sorted(self.jobs, key=lambda job: job.key)]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
