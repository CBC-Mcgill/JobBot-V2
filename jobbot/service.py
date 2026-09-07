import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Protocol

from .classify import classify, is_stale
from .config import Settings, read_registry
from .discord_output import definitive_failure
from .discovery import Discovery
from .models import Company, Job, Snapshot, now
from .store import Store

log = logging.getLogger(__name__)


class Publisher(Protocol):
    """The durable-outbox boundary used by :class:`Service`."""

    async def send(self, delivery: dict, channel_id: int) -> int: ...

    async def find_sent(self, delivery: dict) -> int | None: ...


class SnapshotProvider(Protocol):
    """Fetches one complete board snapshot, or an HTTP-304 snapshot marker."""

    async def fetch_snapshot(
        self, company: Company, *, etag: str | None = None, last_modified: str | None = None
    ) -> Snapshot: ...


class Service:
    """Coordinates registry state, board snapshots, the durable outbox, and delivery."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        providers: SnapshotProvider,
        publisher: Publisher,
        discovery: Discovery | None = None,
    ):
        self.settings = settings
        self.store = store
        self.providers = providers
        self.publisher = publisher
        self.discovery = discovery
        self.scan_lock = asyncio.Lock()
        self._registry_signature = None
        self.store.configure_scheduling(settings.scheduling_policy)

    def load_registry(self) -> None:
        """Synchronize a changed manual registry without touching discovered boards."""
        stat = self.settings.registry_path.stat()
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if signature == self._registry_signature:
            return
        self.store.sync_registry(read_registry(self.settings.registry_path))
        self._registry_signature = signature

    async def reconcile(self) -> None:
        """Resolve ambiguous Discord sends before anything can be retried."""
        for delivery in self.store.uncertain(self.settings.mode):
            try:
                message_id = await self.publisher.find_sent(delivery)
                if message_id is None:
                    self.store.retry(delivery["id"])
                else:
                    self.store.sent(delivery["id"], message_id)
            except Exception as exc:
                # Inaccessible history must never be interpreted as proof of non-delivery.
                self.store.send_error(delivery["id"], type(exc).__name__)
                log.warning(
                    "Delivery reconciliation deferred: %s (%s)", delivery["id"], type(exc).__name__
                )

    async def send(self, delivery: dict, scan_id: str | None = None) -> bool:
        """Deliver one queued item, retaining uncertainty unless failure is definitive."""
        settings = self.settings
        channel_id = (
            settings.destination(delivery["route"])
            if delivery["kind"] == "job"
            else (settings.test_channel if settings.debug else settings.posting_channel)
        )
        self.store.begin_send(delivery["id"], channel_id, scan_id)
        try:
            message_id = await self.publisher.send(delivery, channel_id)
            self.store.sent(delivery["id"], message_id)
            return True
        except Exception as exc:
            self.store.send_error(delivery["id"], type(exc).__name__, definitive_failure(exc))
            log.warning("Discord delivery failed: %s (%s)", delivery["id"], type(exc).__name__)
            return False

    def _companies_to_scan(self) -> list[Company]:
        """Return due boards, or every enabled board when adaptive scheduling is off."""
        return (
            self.store.due_companies()
            if self.settings.adaptive_scheduling
            else self.store.companies()
        )

    async def _fetch_board(
        self, company: Company
    ) -> tuple[Company, Snapshot | None, Exception | None]:
        """Fetch one board with persisted cache validators; never raise from a task."""
        try:
            state = self.store.board_state(company.key)
            snapshot = await self.providers.fetch_snapshot(
                company,
                etag=state["etag"] if state["last_snapshot_hash"] else None,
                last_modified=state["last_modified"] if state["last_snapshot_hash"] else None,
            )
            return company, snapshot, None
        except Exception as exc:
            return company, None, exc

    async def _scan_boards(self, scan_id: str) -> tuple[int, int, int]:
        """Fetch, classify, and atomically observe each selected board."""
        ok, failed, seen = 0, 0, 0
        semaphore = asyncio.Semaphore(self.settings.http_concurrency)

        async def fetch(company: Company) -> tuple[Company, Snapshot | None, Exception | None]:
            async with semaphore:
                return await self._fetch_board(company)

        tasks = [asyncio.create_task(fetch(company)) for company in self._companies_to_scan()]
        try:
            for task in asyncio.as_completed(tasks):
                company, snapshot, error = await task
                jobs = []
                if error is None:
                    try:
                        jobs = (
                            self.store.snapshot_jobs(company.key)
                            if snapshot.jobs is None
                            else snapshot.jobs
                        )
                        results = [(job, classify(job, company.overrides)) for job in jobs]
                        self.store.observe(company, results, self.settings.mode, snapshot)
                    except Exception as exc:
                        error = exc
                if error:
                    failed += 1
                    self.store.company_error(company.key, type(error).__name__)
                    log.warning("Board failed: %s (%s)", company.key, type(error).__name__)
                else:
                    ok += 1
                    seen += len(jobs)
                self.store.scan_progress(scan_id, ok, failed, seen)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return ok, failed, seen

    async def _send_pending(self, kind: str, scan_id: str | None = None) -> int:
        """Drain pending outbox entries of one kind and return successful sends."""
        limit = self.settings.max_send if kind == "job" else 100
        delivered = 0
        for delivery in self.store.pending(self.settings.mode, kind, limit):
            if kind == "job" and is_stale(Job(**json.loads(delivery["payload"]))):
                self.store.cancel(delivery["id"])
                continue
            delivered += await self.send(delivery, scan_id)
        return delivered

    async def scan(self) -> None:
        """Run one non-overlapping scan while preserving outbox and snapshot invariants."""
        if self.scan_lock.locked():
            log.warning("Scan skipped because another scan is running")
            return
        async with self.scan_lock:
            self.load_registry()
            await self.reconcile()
            for old_scan in self.store.unfinished_scans(self.settings.mode):
                self.store.finish_scan(
                    old_scan, self.settings.mode, self.store.backlog(self.settings.mode)
                )
            # Recover summaries after restarts, even when the next scan finds no new jobs.
            await self._send_pending("summary")
            scan_id = self.store.new_scan(self.settings.mode)
            ok, failed, seen = await self._scan_boards(scan_id)
            self.store.queue_active_jobs(self.settings.mode)
            delivered = await self._send_pending("job", scan_id)
            backlog = self.store.backlog(self.settings.mode)
            self.store.finish_scan(scan_id, self.settings.mode, backlog)
            await self._send_pending("summary")
            log.info(
                "Scan %s: boards_ok=%d boards_failed=%d jobs=%d sent=%d backlog=%d mode=%s",
                scan_id,
                ok,
                failed,
                seen,
                delivered,
                backlog,
                self.settings.mode,
            )

    async def discover(self) -> None:
        self.load_registry()
        if not self.discovery:
            return
        added = await self.discovery.run(
            self.settings.discovery_path, self.store.companies(include_disabled=True)
        )
        self.store.set_metadata("last_discovery", now())
        log.info("Discovery finished: %d boards added", len(added))

    async def run(self, stop: asyncio.Event) -> None:
        async def wait(seconds: float):
            try:
                await asyncio.wait_for(stop.wait(), timeout=seconds)
            except TimeoutError:
                pass

        async def scans():
            while not stop.is_set():
                try:
                    await self.scan()
                except Exception as exc:
                    log.error("Scan interrupted (%s); retrying next interval", type(exc).__name__)
                await wait(self.settings.scan_interval)

        async def discoveries():
            while not stop.is_set():
                last = self.store.metadata("last_discovery")
                age = (
                    (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds()
                    if last
                    else self.settings.discovery_interval
                )
                if age >= self.settings.discovery_interval:
                    try:
                        await self.discover()
                    except Exception as exc:
                        log.error("Discovery interrupted (%s)", type(exc).__name__)
                    await wait(self.settings.discovery_interval)
                else:
                    await wait(self.settings.discovery_interval - age)

        await asyncio.gather(scans(), discoveries())
