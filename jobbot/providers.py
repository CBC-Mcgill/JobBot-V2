"""Provider adapters that turn complete public job-board responses into jobs."""

import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .http import PublicHTTP
from .models import Company, Job, Snapshot


def plain(value: str | None) -> str:
    if value and "<" not in value and "&" not in value:
        return value.strip()
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def salary_text(description: str) -> str | None:
    # Preserve the actual excerpt, including currency/period; do not guess or annualize.
    match = re.search(
        r"(?:USD|CAD|GBP|EUR|US\$|CA\$|[$£€])\s*\d[\d,.]*(?:\s*[kK])?"
        r"(?:\s*(?:-|–|—|to)\s*(?:USD|CAD|GBP|EUR|US\$|CA\$|[$£€])?"
        r"\s*\d[\d,.]*(?:\s*[kK])?)?"
        r"(?:\s*(?:USD|CAD|GBP|EUR))?"
        r"(?:\s*(?:/|per |a |an )?(?:hour|hr|year|annum|month|week)(?:ly)?)?",
        description,
    )
    if not match:
        return None
    nearby = description[max(0, match.start() - 100) : match.end() + 80].lower()
    if not re.search(r"salary|compensation|pay\b|wage|hour|annum|per year|annual", nearby):
        return None
    return match.group(0).strip()


def valid_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Listing is missing a valid public job URL")
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("Listing is missing a valid public job URL")
    return value


def job_from(company: Company, remote_id, title, description, url, **kwargs) -> Job:
    if not remote_id or not isinstance(title, str) or not title.strip():
        raise ValueError("Listing lacks an ID or title; board snapshot rejected")
    url = valid_url(url)
    apply_url = valid_url(kwargs.pop("apply_url", None) or url)
    description = plain(description)
    compensation = kwargs.pop("compensation", None) or salary_text(description)
    return Job(
        company.provider,
        company.key,
        str(remote_id),
        company.name,
        title.strip(),
        description,
        url,
        apply_url,
        compensation=compensation,
        **kwargs,
    )


class Providers:
    """Fetch complete snapshots from supported board providers."""
    def __init__(self, http: PublicHTTP):
        self.http = http

    async def fetch(self, company: Company) -> list[Job]:
        snapshot = await self.fetch_snapshot(company)
        if snapshot.jobs is None:
            raise ValueError("Unexpected HTTP 304 without a cached snapshot")
        return snapshot.jobs

    async def fetch_snapshot(
        self, company: Company, *, etag: str | None = None, last_modified: str | None = None
    ) -> Snapshot:
        """Return parsed jobs or a 304 marker; never expose a partial snapshot."""
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if company.provider == "lever":
            return await self.lever(company, headers)
        if company.provider == "jsonld":
            url = company.career_url or company.board
            response = (
                await self.http.page(url, headers=headers) if headers else await self.http.page(url)
            )
        else:
            board = quote(company.board, safe="")
            if company.provider == "ashby":
                url = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
            elif company.provider == "greenhouse":
                url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
            else:
                raise ValueError(f"Unsupported provider: {company.provider}")
            response = await self.http.get(url, headers=headers, max_bytes=100_000_000)
        if response.status_code == 304:
            if not headers:
                raise ValueError("Unexpected HTTP 304 without cache validators")
            return Snapshot(
                None, response.headers.get("etag"), response.headers.get("last-modified")
            )
        # Parse the entire response before returning validators that can be persisted.
        if company.provider == "jsonld":
            jobs = self.jsonld(company, response)
        else:
            jobs = getattr(self, company.provider)(company, response.json())
        return Snapshot(jobs, response.headers.get("etag"), response.headers.get("last-modified"))

    def ashby(self, company: Company, data) -> list[Job]:
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("jobs"), list)
            or data.get("errors")
            or data.get("error")
        ):
            raise ValueError("Invalid Ashby board response")
        jobs = []
        for item in data["jobs"]:
            if item.get("isListed") is False:
                continue
            url = item.get("jobUrl", "")
            locations = [item.get("location") or ""] + [
                loc.get("location", "") for loc in item.get("secondaryLocations", []) or []
            ]
            comp = item.get("compensation") or {}
            jobs.append(
                job_from(
                    company,
                    item.get("id") or urlsplit(url).path.rstrip("/").split("/")[-1],
                    item.get("title"),
                    item.get("descriptionHtml") or item.get("descriptionPlain"),
                    url,
                    apply_url=item.get("applyUrl"),
                    location=" · ".join(dict.fromkeys(filter(None, locations))) or "Not specified",
                    workplace=item.get("workplaceType")
                    or ("Remote" if item.get("isRemote") else ""),
                    employment_type=item.get("employmentType") or "",
                    published_at=item.get("publishedAt"),
                    compensation=comp.get("compensationTierSummary")
                    or comp.get("scrapeableCompensationSalarySummary"),
                    department=" ".join(filter(None, [item.get("department"), item.get("team")])),
                )
            )
        return jobs

    def greenhouse(self, company: Company, data) -> list[Job]:
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("jobs"), list)
            or data.get("errors")
            or data.get("error")
            or (
                "meta" in data and data["meta"].get("total", len(data["jobs"])) != len(data["jobs"])
            )
        ):
            raise ValueError("Invalid Greenhouse board response")
        return [
            job_from(
                company,
                item.get("id"),
                item.get("title"),
                item.get("content"),
                item.get("absolute_url"),
                location=(item.get("location") or {}).get("name") or "Not specified",
                department=" ".join(d.get("name", "") for d in item.get("departments", [])),
                # updated_at is not a reliable publication date.
            )
            for item in data["jobs"]
        ]

    async def lever(self, company: Company, headers: dict[str, str]) -> Snapshot:
        host = "api.eu.lever.co" if company.region == "eu" else "api.lever.co"
        jobs, seen = [], set()
        for page in range(100):
            response = await self.http.get(
                f"https://{host}/v0/postings/{quote(company.board, safe='')}"
                f"?mode=json&skip={page * 100}&limit=100",
                headers=headers if page == 0 else None,
                max_bytes=100_000_000,
            )
            if response.status_code == 304:
                if page != 0 or not headers:
                    raise ValueError("Unexpected HTTP 304 during Lever pagination")
                return Snapshot(
                    None, response.headers.get("etag"), response.headers.get("last-modified")
                )
            data = response.json()
            if not isinstance(data, list):
                raise ValueError("Invalid Lever board response")
            for item in data:
                if item.get("id") in seen:
                    raise ValueError("Lever repeated a posting during pagination; retry next scan")
                seen.add(item.get("id"))
                categories = item.get("categories") or {}
                sections = [item.get("descriptionPlain") or item.get("description") or ""]
                sections += [
                    f"{s.get('text', '')}: {s.get('content', '')}" for s in item.get("lists", [])
                ]
                sections.append(item.get("additionalPlain") or item.get("additional") or "")
                compensation = item.get("salaryDescriptionPlain")
                salary = item.get("salaryRange") or {}
                if salary and (salary.get("min") is not None or salary.get("max") is not None):
                    amounts = " – ".join(
                        str(salary[k]) for k in ("min", "max") if salary.get(k) is not None
                    )
                    compensation = " ".join(
                        filter(None, [salary.get("currency"), amounts, salary.get("interval")])
                    )
                created = item.get("createdAt")
                jobs.append(
                    job_from(
                        company,
                        item.get("id"),
                        item.get("text"),
                        " ".join(sections),
                        item.get("hostedUrl"),
                        apply_url=item.get("applyUrl"),
                        location=" · ".join(categories.get("allLocations") or [])
                        or categories.get("location")
                        or "Not specified",
                        workplace=item.get("workplaceType") or "",
                        employment_type=categories.get("commitment") or "",
                        department=" ".join(
                            filter(None, [categories.get("team"), categories.get("department")])
                        ),
                        published_at=datetime.fromtimestamp(created / 1000, UTC).isoformat()
                        if isinstance(created, (int, float))
                        else None,
                        compensation=compensation,
                    )
                )
            if len(data) < 100:
                # A first-page validator describes a whole board only if it fits on one page.
                # Multi-page boards must fetch every page, even when page one is unchanged.
                return Snapshot(
                    jobs,
                    response.headers.get("etag") if page == 0 else None,
                    response.headers.get("last-modified") if page == 0 else None,
                )
        raise ValueError("Lever pagination exceeded limit; incomplete snapshot rejected")

    def jsonld(self, company: Company, response) -> list[Job]:
        soup = BeautifulSoup(response.text, "html.parser")
        records = []

        def visit(value):
            if isinstance(value, list):
                for item in value:
                    visit(item)
            elif isinstance(value, dict):
                types = value.get("@type", [])
                if "JobPosting" in ([types] if isinstance(types, str) else types):
                    records.append(value)
                else:
                    for child in value.values():
                        if isinstance(child, (dict, list)):
                            visit(child)

        for script in soup.select('script[type="application/ld+json"]'):
            visit(json.loads(script.string or script.get_text()))
        if not records:
            raise ValueError("No JobPosting JSON-LD; custom adapter required")
        jobs = []
        for item in records:
            url = urljoin(str(response.url), item.get("url") or str(response.url))
            identifier = item.get("identifier")
            if isinstance(identifier, dict):
                identifier = identifier.get("value")
            salary = item.get("baseSalary") or {}
            value = salary.get("value") or {}
            compensation = None
            if isinstance(value, dict):
                amounts = [
                    str(value[k])
                    for k in ("value", "minValue", "maxValue")
                    if value.get(k) is not None
                ]
                if amounts:
                    compensation = " ".join(
                        filter(
                            None,
                            [salary.get("currency"), " – ".join(amounts), value.get("unitText")],
                        )
                    )
            locations = item.get("jobLocation") or []
            if isinstance(locations, dict):
                locations = [locations]
            location_text = []
            for location in locations:
                address = location.get("address") or {}
                if isinstance(address, str):
                    location_text.append(address)
                else:
                    location_text.append(
                        ", ".join(
                            str(address[k])
                            for k in ("addressLocality", "addressRegion", "addressCountry")
                            if address.get(k)
                        )
                    )
            valid_through = item.get("validThrough")
            if valid_through:
                expiry = datetime.fromisoformat(valid_through.replace("Z", "+00:00"))
                if expiry.replace(tzinfo=expiry.tzinfo or UTC) < datetime.now(UTC):
                    continue
            jobs.append(
                job_from(
                    company,
                    identifier or hashlib.sha256(url.encode()).hexdigest(),
                    item.get("title"),
                    item.get("description"),
                    url,
                    location=" · ".join(filter(None, location_text)) or "Not specified",
                    workplace="Remote" if item.get("jobLocationType") == "TELECOMMUTE" else "",
                    employment_type=str(item.get("employmentType") or ""),
                    published_at=item.get("datePosted"),
                    compensation=compensation,
                )
            )
        return jobs
