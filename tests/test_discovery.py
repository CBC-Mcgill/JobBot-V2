import json
from unittest.mock import AsyncMock

import httpx
import pytest

from jobbot.discovery import Discovery, board_from_url, source_links
from jobbot.models import Company


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://jobs.ashbyhq.com/openai/abc/application", "ashby:global:openai"),
        ("https://boards.greenhouse.io/embed/job_board?for=example", "greenhouse:global:example"),
        ("https://job-boards.greenhouse.io/example/jobs/123", "greenhouse:global:example"),
        ("https://jobs.eu.lever.co/example/abc", "lever:eu:example"),
        ("https://api.lever.co/v0/postings/example?mode=json", "lever:global:example"),
    ],
)
def test_provider_link_extraction(url, key):
    assert board_from_url(url).key == key
    assert board_from_url("https://jobs.lever.co.evil.example/acme") is None


def test_structured_and_html_sources_preserve_names():
    records = [{"company_name": "Example, Inc.", "url": "https://jobs.lever.co/example/1"}]
    assert source_links(json.dumps(records), "https://example.com") == [
        ("https://jobs.lever.co/example/1", "Example, Inc.")
    ]
    html = (
        '<table><tr><td>Example</td><td><a href="https://jobs.lever.co/example/1">Apply</a></td>'
        "</tr></table>"
    )
    assert source_links(html, "https://example.com")[0][1] == "Example"


async def test_crawl_depth_and_page_count():
    visited = []

    async def page(url):
        visited.append(url)
        html = "".join(f'<a href="/jobs/{n}">Jobs</a>' for n in range(30))
        html += '<iframe src="https://jobs.ashbyhq.com/example"></iframe>'
        return httpx.Response(200, text=html, request=httpx.Request("GET", url))

    http = AsyncMock()
    http.page.side_effect = page
    discovery = Discovery(http, AsyncMock())
    companies = await discovery.crawl_company("https://example.com/careers", "Example")
    assert len(visited) == 10
    assert len(companies) == 1
    assert companies[0].key == "ashby:global:example"


async def test_depth_two_does_not_follow_third_level():
    visited = []

    async def page(url):
        visited.append(url)
        n = int(url.rsplit("/", 1)[-1])
        return httpx.Response(
            200, text=f'<a href="/jobs/{n + 1}">Next</a>', request=httpx.Request("GET", url)
        )

    http = AsyncMock()
    http.page.side_effect = page
    await Discovery(http, AsyncMock()).crawl_company("https://example.com/jobs/0", "Example")
    assert visited == [
        "https://example.com/jobs/0",
        "https://example.com/jobs/1",
        "https://example.com/jobs/2",
    ]


async def test_discovery_validates_before_adding_and_preserves_manual_overrides(store):
    manual = Company("Manual name", "ashby", "example", overrides={"kind": "Quant"})
    store.upsert_company(manual)
    providers = AsyncMock()
    providers.fetch.side_effect = [[], ValueError("Bad response")]
    discovery = Discovery(AsyncMock(), providers, store)
    results = await discovery.validate(
        [
            Company("Discovered name", "ashby", "example"),
            Company("Bad", "lever", "bad"),
        ]
    )
    assert len(results) == 1
    assert store.companies() == [manual]
    assert store.connection.execute("SELECT COUNT(*) FROM discovery_issues").fetchone()[0] == 1


async def test_repeat_discovery_skips_known_boards(tmp_path, store):
    path = tmp_path / "sources.json"
    path.write_text('["https://example.com/list.md"]')
    http = AsyncMock()
    http.get.return_value = httpx.Response(200, text="https://jobs.lever.co/acme/123")
    providers = AsyncMock()
    providers.fetch.return_value = []
    discovery = Discovery(http, providers, store)
    assert len(await discovery.run(path, [])) == 1
    assert await discovery.run(path, store.companies()) == []
    assert providers.fetch.call_count == 1
