"""Configuration loading and validation for environment and registry files."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .models import Company
from .scheduling import DEFAULT_TIERS, SchedulingPolicy, parse_priority_boards

CHANNEL_KEYS = {
    "SWE_Intern": "SWE_INTERNS",
    "SWE_New Grad": "SWE_NG",
    "Quant_Intern": "QUANT_INTERNS",
    "Quant_New Grad": "QUANT_NG",
    "AI/ML_Intern": "AI_INTERNS",
    "AI/ML_New Grad": "AI_NG",
}


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def boolean(name: str, default: bool = True) -> bool:
    value = os.getenv(name, str(default)).lower().strip()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1", "yes"}


@dataclass(frozen=True)
class Settings:
    token: str
    channels: dict[str, int]
    posting_channel: int
    test_channel: int
    roles: tuple[int, int]
    debug: bool = True
    max_send: int = 50
    database_path: Path = Path("data/jobs.sqlite3")
    registry_path: Path = Path("config/companies.json")
    discovery_path: Path = Path("config/discovery.json")
    scan_interval: int = 1800
    discovery_interval: int = 86400
    http_concurrency: int = 8
    host_interval: float = 0.25
    adaptive_scheduling: bool = True
    quiet_tiers: tuple[int, ...] = DEFAULT_TIERS
    priority_scan_interval: int = 7200
    priority_boards: dict[str, int] = field(default_factory=dict)
    failure_retry: int = 1800

    def __post_init__(self):
        if type(self.scan_interval) is not int or self.scan_interval <= 0:
            raise ValueError("ACTIVE_SCAN_INTERVAL_SECONDS must be a positive integer")
        _ = self.scheduling_policy  # Validate settings constructed without .env, too.

    @property
    def scheduling_policy(self) -> SchedulingPolicy:
        return SchedulingPolicy(
            self.quiet_tiers, self.priority_scan_interval, self.priority_boards, self.failure_retry
        )

    @property
    def mode(self) -> str:
        return "debug" if self.debug else "production"

    def destination(self, route: str) -> int:
        return self.test_channel if self.debug else self.channels[route]

    @classmethod
    def load(cls, env_file: str = ".env", discord_required: bool = True) -> "Settings":
        load_dotenv(env_file, override=False)
        debug = boolean("DEBUG")
        priority_interval = positive_int("PRIORITY_SCAN_INTERVAL_SECONDS", 7200)
        try:
            tiers = tuple(
                int(t.strip())
                for t in os.getenv("QUIET_TIERS_SECONDS", ",".join(map(str, DEFAULT_TIERS))).split(
                    ","
                )
            )
        except ValueError as exc:
            raise ValueError("QUIET_TIERS_SECONDS must contain positive integers") from exc

        def snowflake(key: str, required: bool = False) -> int:
            value = os.getenv(key, "").strip()
            if not value and not required:
                return 0
            if not value.isdecimal() or int(value) <= 0:
                raise ValueError(f"{key} must be a positive Discord ID")
            return int(value)

        token = os.getenv("DISCORD_TOKEN", "").strip()
        if discord_required and not token:
            raise ValueError("DISCORD_TOKEN is required")
        host_interval = float(os.getenv("HTTP_HOST_INTERVAL_SECONDS", "0.25"))
        if not 0 < host_interval <= 60:
            raise ValueError("HTTP_HOST_INTERVAL_SECONDS must be between 0 and 60")
        return cls(
            token=token,
            channels={
                route: snowflake(key, discord_required) for route, key in CHANNEL_KEYS.items()
            },
            posting_channel=snowflake("POSTING_CHANNEL", discord_required),
            test_channel=snowflake("TEST_CHANNEL", discord_required and debug),
            roles=(
                snowflake("ROLE_ID_JOB_PING", discord_required and not debug),
                snowflake("ROLE_ID_LOVES_NOTIFICATIONS", discord_required and not debug),
            ),
            debug=debug,
            max_send=positive_int("MAX_SEND", 50),
            database_path=Path(os.getenv("DATABASE_PATH", "data/jobs.sqlite3")),
            registry_path=Path(os.getenv("COMPANY_REGISTRY", "config/companies.json")),
            discovery_path=Path(os.getenv("DISCOVERY_SOURCES", "config/discovery.json")),
            scan_interval=positive_int(
                "ACTIVE_SCAN_INTERVAL_SECONDS"
                if "ACTIVE_SCAN_INTERVAL_SECONDS" in os.environ
                else "SCAN_INTERVAL_SECONDS",
                1800,
            ),
            discovery_interval=positive_int("DISCOVERY_INTERVAL_SECONDS", 86400),
            http_concurrency=positive_int("HTTP_CONCURRENCY", 8),
            host_interval=host_interval,
            adaptive_scheduling=boolean("ADAPTIVE_SCHEDULING"),
            quiet_tiers=tiers,
            priority_scan_interval=priority_interval,
            priority_boards=parse_priority_boards(
                os.getenv("PRIORITY_BOARDS", ""), priority_interval
            ),
            failure_retry=positive_int("FAILURE_RETRY_SECONDS", 1800),
        )


def read_registry(path: Path) -> list[Company]:
    """Read the supported, stable company-registry JSON shape."""
    companies = [Company(**item) for item in json.loads(path.read_text())]
    seen = set()
    for company in companies:
        if company.provider not in {"ashby", "greenhouse", "lever", "jsonld"}:
            raise ValueError(f"Unsupported provider for {company.name}: {company.provider}")
        if not company.board or not company.name or company.region not in {"global", "eu"}:
            raise ValueError(f"Invalid company: {company.name or company.key!r}")
        if company.key in seen:
            raise ValueError(f"Duplicate board: {company.key}")
        seen.add(company.key)
        overrides = company.overrides or {}
        if set(overrides) - {"kind", "experience", "exclude"}:
            raise ValueError(f"Unknown override for {company.name}")
        if "kind" in overrides and overrides["kind"] not in {"SWE", "Quant", "AI/ML"}:
            raise ValueError(f"Invalid kind override for {company.name}")
        if "experience" in overrides and overrides["experience"] not in {"Intern", "New Grad"}:
            raise ValueError(f"Invalid experience override for {company.name}")
        if "exclude" in overrides and not isinstance(overrides["exclude"], bool):
            raise ValueError(f"Invalid exclude override for {company.name}")
    return companies
