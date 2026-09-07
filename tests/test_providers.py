import json
from pathlib import Path

import httpx
import pytest

from jobbot.http import PublicHTTP
from jobbot.models import Company
from jobbot.providers import Providers, salary_text

FIXTURES = Path(__file__).parent / "fixtures"


def api(handler):
    return Providers(
        PublicHTTP(interval=0, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    )


@pytest.mark.parametrize("provider", ["ashby", "greenhouse", "lever"])
async def test_provider_fixtures(provider):
    data = json.loads((FIXTURES / f"{provider}.json").read_text())
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=data)

    providers = api(handle)
    try:
        jobs = await providers.fetch(Company("Example", provider, "example", region="eu"))
        assert len(jobs) == 1
        assert jobs[0].compensation
        assert "<p>" not in jobs[0].description
        if provider == "ashby":
            assert jobs[0].remote_id == "abc"
            assert jobs[0].location == "Toronto · Vancouver"
            assert "includeCompensation=true" in str(requests[0].url)
        if provider == "greenhouse":
            assert jobs[0].published_at is None
            assert "content=true" in str(requests[0].url)
        if provider == "lever":
            assert requests[0].url.host == "api.eu.lever.co"
            assert "Python" in jobs[0].description
    finally:
        await providers.http.close()


async def test_lever_pagination():
    fixture = json.loads((FIXTURES / "lever.json").read_text())[0]
    requests = []

    def handle(request):
        skip = int(request.url.params["skip"])
        requests.append(skip)
        data = [{**fixture, "id": str(i)} for i in range(skip, min(skip + 100, 103))]
        return httpx.Response(200, json=data)

    providers = api(handle)
    try:
        assert len(await providers.fetch(Company("Example", "lever", "example"))) == 103
        assert requests == [0, 100]
    finally:
        await providers.http.close()


async def test_invalid_snapshot_is_not_empty_board():
    providers = api(lambda r: httpx.Response(200, json={"error": "not found"}))
    try:
        with pytest.raises(ValueError, match="Invalid Ashby"):
            await providers.fetch(Company("Example", "ashby", "example"))
    finally:
        await providers.http.close()


async def test_bad_job_rejects_whole_snapshot():
    providers = api(lambda r: httpx.Response(200, json={"jobs": [{"title": "Missing URL"}]}))
    try:
        with pytest.raises(ValueError):
            await providers.fetch(Company("Example", "greenhouse", "example"))
    finally:
        await providers.http.close()


async def test_jsonld_graph(monkeypatch):
    data = {
        "@graph": [
            {
                "@type": "JobPosting",
                "title": "Software Engineer Intern",
                "url": "/jobs/1",
                "description": "<p>Build software</p>",
                "identifier": {"value": "1"},
                "jobLocationType": "TELECOMMUTE",
                "baseSalary": {
                    "currency": "USD",
                    "value": {"minValue": 40, "maxValue": 60, "unitText": "HOUR"},
                },
            }
        ]
    }
    providers = api(lambda r: httpx.Response(200))

    async def page(url):
        return httpx.Response(
            200,
            text=f'<script type="application/ld+json">{json.dumps(data)}</script>',
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(providers.http, "page", page)
    try:
        jobs = await providers.fetch(
            Company("Example", "jsonld", "example", career_url="https://example.com/careers")
        )
        assert jobs[0].url == "https://example.com/jobs/1"
        assert jobs[0].compensation == "USD 40 – 60 HOUR"
        assert jobs[0].workplace == "Remote"
        data["@graph"] = []
        with pytest.raises(ValueError, match="custom adapter"):
            await providers.fetch(Company("Example", "jsonld", "https://example.com/careers"))
    finally:
        await providers.http.close()


def test_salary_without_estimation():
    assert salary_text("Our funding is $100 million.") is None
    assert salary_text("Compensation: CAD 35 - 45 per hour.") == "CAD 35 - 45 per hour"
    assert salary_text("Competitive salary") is None


@pytest.mark.parametrize("provider", ["ashby", "greenhouse", "lever"])
async def test_conditional_snapshot_200_304_and_missing_headers(provider):
    data = json.loads((FIXTURES / f"{provider}.json").read_text())
    requests = []
    modified = "Sun, 06 Sep 2026 00:00:00 GMT"
    responses = [
        httpx.Response(200, json=data, headers={"ETag": '"v1"', "Last-Modified": modified}),
        httpx.Response(304),
        httpx.Response(200, json=data),
    ]

    def handle(request):
        requests.append(request)
        return responses.pop(0)

    providers = api(handle)
    company = Company("Example", provider, "example")
    try:
        first = await providers.fetch_snapshot(company)
        assert first.etag == '"v1"'
        assert first.last_modified == modified
        assert first.jobs and first.digest
        unchanged = await providers.fetch_snapshot(company, etag=first.etag, last_modified=modified)
        assert unchanged.jobs is None
        assert unchanged.digest is None
        assert requests[-1].headers["If-None-Match"] == '"v1"'
        assert requests[-1].headers["If-Modified-Since"] == modified
        third = await providers.fetch_snapshot(company, etag=first.etag)
        assert third.jobs == first.jobs
        assert third.etag is third.last_modified is None
        assert third.digest == first.digest
    finally:
        await providers.http.close()


@pytest.mark.parametrize("provider", ["ashby", "greenhouse", "lever"])
async def test_provider_rejects_unconditional_304(provider):
    providers = api(lambda r: httpx.Response(304))
    try:
        with pytest.raises(ValueError, match="Unexpected HTTP 304"):
            await providers.fetch_snapshot(Company("Example", provider, "example"))
    finally:
        await providers.http.close()


async def test_paginated_lever_does_not_cache_only_first_page():
    item = json.loads((FIXTURES / "lever.json").read_text())[0]
    requests = []
    total = 101

    def handle(request):
        requests.append(request)
        skip = int(request.url.params["skip"])
        data = [{**item, "id": str(i)} for i in range(skip, min(skip + 100, total))]
        return httpx.Response(200, json=data, headers={"ETag": '"first-page"'})

    providers = api(handle)
    company = Company("Example", "lever", "example")
    try:
        first = await providers.fetch_snapshot(company)
        assert len(first.jobs) == 101
        assert first.etag is first.last_modified is None
        total = 102
        second = await providers.fetch_snapshot(company, etag=first.etag)
        assert len(second.jobs) == 102
        assert second.digest != first.digest
        assert all("If-None-Match" not in request.headers for request in requests)
    finally:
        await providers.http.close()


@pytest.mark.parametrize(
    ("provider", "body"),
    [
        ("ashby", {"jobs": [], "error": "unavailable"}),
        ("greenhouse", {"jobs": [], "meta": {"total": 10}}),
        ("greenhouse", {"jobs": [], "errors": ["unavailable"]}),
        ("lever", {"error": "unavailable"}),
    ],
)
async def test_error_or_partial_response_never_becomes_empty_snapshot(provider, body):
    providers = api(lambda r: httpx.Response(200, json=body, headers={"ETag": '"bad"'}))
    try:
        with pytest.raises(ValueError):
            await providers.fetch_snapshot(Company("Example", provider, "example"), etag='"good"')
    finally:
        await providers.http.close()


async def test_lever_page_failure_rejects_complete_snapshot():
    item = json.loads((FIXTURES / "lever.json").read_text())[0]

    def handle(request):
        if request.url.params["skip"] == "0":
            return httpx.Response(200, json=[{**item, "id": str(i)} for i in range(100)])
        return httpx.Response(200, json={"error": "unavailable"})

    providers = api(handle)
    try:
        with pytest.raises(ValueError, match="Invalid Lever"):
            await providers.fetch_snapshot(Company("Example", "lever", "example"))
    finally:
        await providers.http.close()


async def test_jsonld_conditional_requests_respect_robots(monkeypatch):
    record = {
        "@type": "JobPosting",
        "title": "Software Engineer Intern",
        "url": "https://example.com/jobs/1",
        "description": "Build software",
    }
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/robots.txt":
            assert "If-None-Match" not in request.headers
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if "If-None-Match" in request.headers:
            return httpx.Response(304)
        return httpx.Response(
            200,
            text=f'<script type="application/ld+json">{json.dumps(record)}</script>',
            headers={"ETag": '"page"'},
        )

    providers = api(handle)
    from unittest.mock import AsyncMock

    monkeypatch.setattr(providers.http, "_check_public", AsyncMock())
    company = Company("Example", "jsonld", "https://example.com/careers")
    try:
        first = await providers.fetch_snapshot(company)
        assert len(first.jobs) == 1
        assert first.etag == '"page"'
        second = await providers.fetch_snapshot(company, etag=first.etag)
        assert second.jobs is None
        assert [r.url.path for r in requests] == ["/robots.txt", "/careers", "/careers"]
    finally:
        await providers.http.close()
