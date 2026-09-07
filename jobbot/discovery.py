"""Discover and validate supported public job boards without posting listings."""

import asyncio
import json
import logging
import re
from collections import deque
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .http import PublicHTTP
from .models import Company, canonical_url, now
from .providers import Providers, plain

log = logging.getLogger(__name__)
URL_PATTERN = re.compile(r"https?://[^\s<>\"'\])]+")
CAREER_PATTERN = re.compile(r"career|/jobs?(?:/|$|\?)|/opportunities", re.I)


def board_from_url(url: str, name: str = "", provenance: str = "discovered") -> Company | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = [unquote(p) for p in parts.path.split("/") if p]
    provider, region, board = "", "global", ""
    if host == "jobs.ashbyhq.com" and path:
        provider, board = "ashby", path[0]
    elif host == "api.ashbyhq.com" and path[:2] == ["posting-api", "job-board"] and len(path) > 2:
        provider, board = "ashby", path[2]
    elif host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and path:
        provider = "greenhouse"
        board = parse_qs(parts.query).get("for", [""])[0] if path[0] == "embed" else path[0]
    elif host == "boards-api.greenhouse.io" and path[:2] == ["v1", "boards"] and len(path) > 2:
        provider, board = "greenhouse", path[2]
    elif host in {"jobs.lever.co", "jobs.eu.lever.co"} and path:
        provider, board = "lever", path[0]
        region = "eu" if host == "jobs.eu.lever.co" else "global"
    elif (
        host in {"api.lever.co", "api.eu.lever.co"}
        and path[:2] == ["v0", "postings"]
        and len(path) > 2
    ):
        provider, board = "lever", path[2]
        region = "eu" if host == "api.eu.lever.co" else "global"
    if not provider or not re.fullmatch(r"[A-Za-z0-9_.-]+", board):
        return None
    return Company(
        # A blank-but-truthy scraped name would otherwise strip to nothing.
        name=name.strip() or board,
        provider=provider,
        board=board,
        region=region,
        career_url=canonical_url(url),
        provenance=provenance,
    )


def source_links(text: str, source: str) -> list[tuple[str, str]]:
    """Extract URLs from Markdown/HTML without copying the source's prose."""
    links = []
    if text.lstrip().startswith(("[", "{")):
        try:
            records = json.loads(text)
            if isinstance(records, dict):
                records = records.get("jobs", records.get("listings", []))
            if isinstance(records, list):
                for item in records:
                    if isinstance(item, dict):
                        name = item.get("company_name") or item.get("company") or ""
                        if isinstance(name, dict):
                            name = name.get("name", "")
                        for key in ("url", "application_url", "company_url"):
                            if isinstance(item.get(key), str):
                                links.append((item[key], name))
                if links:
                    return list(dict.fromkeys(links))
        except ValueError:
            pass
    previous_name = ""
    for line in text.splitlines():
        name = ""
        if line.startswith("|"):
            first_cell = line.split("|")[1].strip()
            first_cell = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", first_cell)
            name = plain(first_cell).strip(" *")
            if name in {"↳", "↪", "", "↳️"}:
                name = previous_name
            if name and name not in {"Company", "---"}:
                previous_name = name
        for match in URL_PATTERN.finditer(line):
            links.append((match.group(0).replace("&amp;", "&"), name))
    # HTML career sites may use relative links and embedded provider iframes.
    if "<" not in text:
        return list(dict.fromkeys(links))
    soup = BeautifulSoup(text, "html.parser")
    last_name = ""
    for row in soup.select("tr"):
        cells = row.select("td")
        if not cells:
            continue
        name = cells[0].get_text(" ", strip=True)
        if name in {"↳", "↪", "", "↳️"}:
            name = last_name
        else:
            last_name = name
        for node in row.select("a[href]"):
            links.insert(0, (urljoin(source, node["href"]), name))
    for node in soup.select("a[href], iframe[src]"):
        link = node.get("href") or node.get("src")
        links.append((urljoin(source, link), ""))
    return list(dict.fromkeys(links))


class Discovery:
    """Find candidate boards, then persist only boards that pass provider validation."""
    def __init__(self, http: PublicHTTP, providers: Providers, store=None):
        self.http = http
        self.providers = providers
        self.store = store

    async def crawl_company(self, start: str, name: str) -> list[Company]:
        queue = deque([(start, 0)])
        seen, found = set(), {}
        origin_host = urlsplit(start).hostname
        while queue and len(seen) < 10:
            url, depth = queue.popleft()
            url = canonical_url(url)
            if url in seen:
                continue
            seen.add(url)
            try:
                response = await self.http.page(url)
                links = [(str(response.url), name)] + source_links(response.text, str(response.url))
                for link, _ in links:
                    company = board_from_url(link, name, start)
                    if company:
                        found[company.key] = company
                    elif (
                        depth < 2
                        and urlsplit(link).hostname == origin_host
                        and CAREER_PATTERN.search(link)
                        and canonical_url(link) not in seen
                    ):
                        queue.append((link, depth + 1))
            except Exception as exc:
                log.debug("Career page unavailable: %s (%s)", url, type(exc).__name__)
        if not found and self.store:
            self.store.discovery_issue(
                start, "No supported board found; JSON-LD/custom adapter needed"
            )
        return list(found.values())

    async def candidates(self, sources: list[str], companies: list[Company]) -> list[Company]:
        candidates = {}
        careers = {
            c.career_url: c.name
            for c in companies
            if c.career_url and not board_from_url(c.career_url)
        }
        for source in sources:
            try:
                response = await self.http.get(source, public_only=True)
                for url, name in source_links(response.text, source):
                    company = board_from_url(url, name, source)
                    if company:
                        candidates.setdefault(company.key, company)
                    elif CAREER_PATTERN.search(url) and urlsplit(url).hostname not in {
                        "github.com",
                        "raw.githubusercontent.com",
                        "simplify.jobs",
                    }:
                        careers.setdefault(url, name)
            except Exception as exc:
                log.warning("Discovery source failed: %s (%s)", source, type(exc).__name__)
                if self.store:
                    self.store.discovery_issue(source, type(exc).__name__)
        # Crawl at most one starting URL per host per discovery pass.
        by_host = {}
        for url, name in careers.items():
            by_host.setdefault(urlsplit(url).hostname, (url, name))
        semaphore = asyncio.Semaphore(4)

        async def crawl(url, name):
            async with semaphore:
                return await self.crawl_company(url, name)

        results = await asyncio.gather(*(crawl(url, name) for url, name in by_host.values()))
        for result in results:
            for company in result:
                candidates.setdefault(company.key, company)
        return list(candidates.values())

    async def validate(self, candidates: list[Company]) -> list[Company]:
        semaphore = asyncio.Semaphore(8)

        async def check(company):
            async with semaphore:
                try:
                    jobs = await self.providers.fetch(company)
                    verified = replace(company, verified_at=now())
                    if self.store:
                        self.store.upsert_company(verified, manual=False)
                    log.info("Verified %s (%d open jobs)", company.key, len(jobs))
                    return verified
                except Exception as exc:
                    log.info("Board verification failed: %s (%s)", company.key, type(exc).__name__)
                    if self.store:
                        self.store.discovery_issue(company.key, type(exc).__name__)
                    return None

        results = await asyncio.gather(*(check(c) for c in candidates))
        return [c for c in results if c is not None]

    async def run(self, path: Path, companies: list[Company]) -> list[Company]:
        """Load source URLs, skip known boards, and return validated additions."""
        sources = json.loads(path.read_text())
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
            raise ValueError("Discovery sources must be a JSON list of URLs")
        known = {c.key for c in companies}
        if self.store:
            known.update(c.key for c in self.store.companies(include_disabled=True))
        candidates = await self.candidates(sources, companies)
        return await self.validate([c for c in candidates if c.key not in known])
