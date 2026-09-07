"""Command-line wiring for the single-process job bot."""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
from contextlib import contextmanager
from pathlib import Path

from .config import Settings, read_registry
from .discord_output import DiscordPublisher
from .discovery import Discovery
from .http import PublicHTTP
from .providers import Providers
from .scheduling import MAX_QUIET_INTERVAL
from .service import Service
from .store import Store


@contextmanager
def process_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another bot process is already using this database") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def execute(args, settings: Settings):
    store = Store(settings.database_path)
    http = PublicHTTP(settings.http_concurrency, settings.host_interval)
    publisher = DiscordPublisher(settings)
    providers = Providers(http)
    discovery = Discovery(http, providers, store)
    service = Service(settings, store, providers, publisher, discovery)
    try:
        if args.command == "backup":
            store.backup(args.destination)
            print(f"Database backup saved to {args.destination}")
        elif args.command == "discover":
            await service.discover()
        elif args.command == "status":
            result = {
                "companies": store.connection.execute("SELECT COUNT(*) FROM companies").fetchone()[
                    0
                ],
                "mode": settings.mode,
                "backlog": store.backlog(settings.mode),
                "uncertain_deliveries": len(store.uncertain(settings.mode)),
                "last_discovery": store.metadata("last_discovery"),
                "adaptive_scheduling": {
                    **store.scheduling_status(adaptive=settings.adaptive_scheduling),
                    "scheduler_interval_seconds": settings.scan_interval,
                },
                "last_scan": dict(row)
                if (
                    row := store.connection.execute(
                        "SELECT * FROM scans WHERE mode=? ORDER BY started_at DESC LIMIT 1",
                        (settings.mode,),
                    ).fetchone()
                )
                else None,
                "failing_boards": [
                    dict(r)
                    for r in store.connection.execute(
                        "SELECT key,last_success,last_error FROM companies "
                        "WHERE last_error IS NOT NULL"
                    )
                ],
                "discovery_issues": [
                    dict(r)
                    for r in store.connection.execute(
                        "SELECT * FROM discovery_issues ORDER BY last_seen DESC LIMIT 20"
                    )
                ],
            }
            print(json.dumps(result, indent=2))
        else:
            await publisher.start()
            if args.command == "scan":
                await service.scan()
            elif args.command == "run":
                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                for signum in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(signum, stop.set)
                worker = asyncio.create_task(service.run(stop))
                stopper = asyncio.create_task(stop.wait())
                try:
                    done, _ = await asyncio.wait(
                        [worker, stopper], return_when=asyncio.FIRST_COMPLETED
                    )
                    if worker in done:
                        await worker
                    else:
                        # Interrupt in-flight I/O after a short grace period. Durable outbox
                        # entries remain available for reconciliation on the next start.
                        try:
                            await asyncio.wait_for(worker, timeout=15)
                        except TimeoutError:
                            pass
                finally:
                    worker.cancel()
                    stopper.cancel()
                    await asyncio.gather(worker, stopper, return_exceptions=True)
    finally:
        await publisher.close()
        await http.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description="Been's early-career Discord job bot")
    parser.add_argument("--env-file", default=".env")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run scans and automatic discovery continuously")
    sub.add_parser("scan", help="Run one scan and deliver its backlog (uses DEBUG setting)")
    sub.add_parser("discover", help="Find and validate new boards without posting to Discord")
    sub.add_parser("check-config", help="Validate local configuration without network access")
    sub.add_parser("status", help="Show persistent scan, delivery, and discovery status")
    backup = sub.add_parser("backup", help="Create a consistent SQLite backup")
    backup.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        settings = Settings.load(
            args.env_file,
            discord_required=args.command
            in {
                "run",
                "scan",
                "check-config",
            },
        )
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        if args.command == "check-config":
            companies = read_registry(settings.registry_path)
            sources = json.loads(settings.discovery_path.read_text())
            if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
                raise ValueError("Discovery sources must be a JSON list of URLs")
            tiers = settings.scheduling_policy.tiers
            print(
                f"Configuration valid: mode={settings.mode}, boards={len(companies)}, "
                f"max_send={settings.max_send}, interval={settings.scan_interval}s"
            )
            print(f"Quiet tiers in effect: {','.join(map(str, tiers))}")
            if tiers != settings.quiet_tiers:
                # Silently shortening the ladder would leave no way to notice.
                print(
                    f"  note: QUIET_TIERS_SECONDS={','.join(map(str, settings.quiet_tiers))} "
                    f"exceeds the {MAX_QUIET_INTERVAL}s freshness ceiling; "
                    "tiers above it were dropped."
                )
            return
        # SQLite's backup API and WAL reads work safely while the main process runs.
        if args.command in {"backup", "status"}:
            asyncio.run(execute(args, settings))
        else:
            with process_lock(settings.database_path):
                asyncio.run(execute(args, settings))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Configuration/runtime error: {exc}\n")
    except KeyboardInterrupt:
        pass
