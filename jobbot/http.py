"""Polite public-web HTTP client with SSRF checks, retries, and host pacing."""

import asyncio
import ipaddress
import random
import socket
import time
from collections import defaultdict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "BeenJobBot/0.1 (+public job listing discovery)"


def retry_delay(value: str | None, attempt: int) -> float:
    if value:
        try:
            return max(0, min(float(value), 60))
        except ValueError:
            try:
                return max(
                    0, min((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds(), 60)
                )
            except (ValueError, TypeError):
                pass
    return min(2**attempt + random.uniform(0, 0.5), 30)


class PublicHTTP:
    """Shared HTTP boundary for providers and discovery."""
    def __init__(
        self, concurrency: int = 8, interval: float = 0.25, client: httpx.AsyncClient | None = None
    ):
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )
        self.semaphore = asyncio.Semaphore(concurrency)
        self.host_locks = defaultdict(asyncio.Lock)
        self.next_request = defaultdict(float)
        self.interval = interval
        self.robots: dict[str, RobotFileParser | None] = {}

    async def close(self):
        await self.client.aclose()

    async def _check_public(self, url: str):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("Only public HTTP(S) URLs are supported")
        if parts.username or parts.password or parts.port not in {None, 80, 443}:
            raise ValueError("Credentialed URLs and nonstandard ports are unsupported")
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            address = None
        if address is not None:
            if not address.is_global:
                raise ValueError("Discovery cannot access private network addresses")
            return
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                parts.hostname,
                parts.port or (443 if parts.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
            timeout=10,
        )
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("Discovery cannot access private network addresses")

    async def get(
        self,
        url: str,
        *,
        public_only: bool = False,
        max_bytes: int = 20_000_000,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Fetch a bounded response, optionally enforcing public-address safety."""
        # Each redirect is checked before fetching arbitrary discovered links.
        for _ in range(6):
            if public_only:
                await self._check_public(url)
            host = urlsplit(url).netloc
            response = None
            for attempt in range(3):
                try:
                    async with self.semaphore:
                        async with self.host_locks[host]:
                            await asyncio.sleep(max(0, self.next_request[host] - time.monotonic()))
                            self.next_request[host] = time.monotonic() + self.interval
                        async with self.client.stream("GET", url, headers=headers) as streamed:
                            chunks, size = [], 0
                            async for chunk in streamed.aiter_bytes():
                                size += len(chunk)
                                if size > max_bytes:
                                    raise ValueError("Response exceeds size limit")
                                chunks.append(chunk)
                            response_headers = dict(streamed.headers)
                            response_headers.pop("content-encoding", None)
                            response_headers.pop("content-length", None)
                            response = httpx.Response(
                                streamed.status_code,
                                headers=response_headers,
                                content=b"".join(chunks),
                                request=streamed.request,
                            )
                    if response.status_code != 429 and response.status_code < 500:
                        break
                    response.raise_for_status()
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    if attempt == 2:
                        raise
                    header = (
                        exc.response.headers.get("Retry-After")
                        if isinstance(exc, httpx.HTTPStatusError)
                        else None
                    )
                    delay = retry_delay(header, attempt)
                    self.next_request[host] = max(self.next_request[host], time.monotonic() + delay)
                    await asyncio.sleep(delay)
            if response is None:
                raise RuntimeError("No HTTP response")
            if response.status_code == 304:
                return response
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Redirect missing location")
                url = str(response.url.join(location))
                continue
            response.raise_for_status()
            return response
        raise ValueError("Too many redirects")

    async def json(self, url: str):
        return (await self.get(url, max_bytes=100_000_000)).json()

    async def page(self, url: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            try:
                response = await self.get(f"{origin}/robots.txt", public_only=True)
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
                self.robots[origin] = parser
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    parser = RobotFileParser()
                    parser.parse([])
                    self.robots[origin] = parser
                else:
                    self.robots[origin] = None
        parser = self.robots[origin]
        if parser is None or not parser.can_fetch("BeenJobBot", url):
            raise ValueError("Crawling disallowed or robots.txt unavailable")
        return await self.get(url, public_only=True, headers=headers)
