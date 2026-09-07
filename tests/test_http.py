import gzip
from unittest.mock import AsyncMock

import httpx
import pytest

from jobbot.http import PublicHTTP


async def test_compressed_body_decoded_once():
    data = gzip.compress(b'{"jobs": []}')
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=data, headers={"Content-Encoding": "gzip"})
        )
    )
    http = PublicHTTP(client=client, interval=0)
    try:
        assert await http.json("https://example.com") == {"jobs": []}
    finally:
        await http.close()


async def test_rate_limit_and_server_error_retried(monkeypatch):
    monkeypatch.setattr("jobbot.http.asyncio.sleep", AsyncMock())
    responses = [429, 503, 200]

    def handle(request):
        return httpx.Response(responses.pop(0), json={"ok": True}, headers={"Retry-After": "0"})

    http = PublicHTTP(client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), interval=0)
    try:
        assert await http.json("https://example.com") == {"ok": True}
        assert responses == []
    finally:
        await http.close()


async def test_404_not_retried():
    handler = AsyncMock(return_value=httpx.Response(404))
    http = PublicHTTP(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), interval=0)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await http.get("https://example.com")
        assert handler.call_count == 1
    finally:
        await http.close()


async def test_arbitrary_discovery_rejects_private_ips():
    http = PublicHTTP()
    try:
        with pytest.raises(ValueError, match="private network"):
            await http.get("http://127.0.0.1/secret", public_only=True)
    finally:
        await http.close()


async def test_size_limit():
    http = PublicHTTP(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 20))
        ),
        interval=0,
    )
    try:
        with pytest.raises(ValueError, match="size limit"):
            await http.get("https://example.com", max_bytes=10)
    finally:
        await http.close()


async def test_conditional_headers_and_304_without_location():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(304, headers={"ETag": '"v1"'})

    http = PublicHTTP(client=httpx.AsyncClient(transport=httpx.MockTransport(handle)), interval=0)
    try:
        response = await http.get(
            "https://example.com/jobs",
            headers={"If-None-Match": '"v1"', "If-Modified-Since": "Sun, 06 Sep 2026 00:00:00 GMT"},
        )
        assert response.status_code == 304
        assert len(requests) == 1
        assert requests[0].headers["If-None-Match"] == '"v1"'
        assert "If-Modified-Since" in requests[0].headers
    finally:
        await http.close()
