"""HTTP access to Domstolsverket's published case law.

The API is open — no key, no account, CORS wide open — and deliberately plain:

    GET /api/v1/publiceringar?page=N     10 records, newest first
    GET /api/v1/domstolar                the court list

`page` is the only parameter it honours. Court codes, date ranges, subject
filters and a JSON body on /publiceringar/sok are all either ignored or
rejected with 405. Verified 2026-08-30 by trying each one and comparing the
x-total-count and first record against an unfiltered call.

That is why this package mirrors rather than proxies. A search box in front of
this API would have to fetch the whole corpus to answer any question anyway.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, AsyncIterator

import httpx

BASE_URL = "https://rattspraxis.etjanst.domstol.se"
PUBLICERINGAR = "/api/v1/publiceringar"
DOMSTOLAR = "/api/v1/domstolar"
PAGE_SIZE = 10  # fixed by the API; size/limit/antal are all ignored

USER_AGENT = "legal-mcp-sweden/0.1 (+https://github.com/AvoccadoTech/legal-mcp-sweden)"


class RattspraxisError(RuntimeError):
    """Raised with a message an agent can act on, not just a status code."""


class RattspraxisClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 4,
        pause: float = 0.15,
    ) -> None:
        self._max_retries = max_retries
        # A full sync is ~1,700 requests against a public service that
        # publishes no rate limit. Pacing it costs a few minutes once and
        # avoids finding the ceiling on someone else's behalf.
        self._pause = pause
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "RattspraxisClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = await self._client.get(path, params=params)
            except httpx.RequestError as exc:
                last = exc
                await asyncio.sleep(min(2.0**attempt, 16.0) + random.uniform(0, 0.5))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last = RattspraxisError(f"Domstolsverket returned {response.status_code}")
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    await asyncio.sleep(min(float(retry_after), 30.0))
                else:
                    await asyncio.sleep(min(2.0**attempt, 16.0) + random.uniform(0, 0.5))
                continue
            if response.status_code >= 400:
                raise RattspraxisError(
                    f"Domstolsverket rejected the request ({response.status_code}) for {path}"
                )
            return response
        raise RattspraxisError(
            f"Domstolsverket unreachable after {self._max_retries} attempts: {last}"
        )

    async def total_count(self) -> int:
        """Corpus size, from the x-total-count header on any page."""
        response = await self._get(PUBLICERINGAR, {"page": 0})
        try:
            return int(response.headers.get("x-total-count", "0"))
        except ValueError:
            return 0

    async def page(self, n: int) -> list[dict[str, Any]]:
        response = await self._get(PUBLICERINGAR, {"page": n})
        payload = response.json()
        if not isinstance(payload, list):
            raise RattspraxisError(
                f"Expected a list of publications on page {n}, got {type(payload).__name__}. "
                "The API shape has changed."
            )
        return payload

    async def pages(self, start: int = 0, max_pages: int | None = None) -> AsyncIterator[
        tuple[int, list[dict[str, Any]]]
    ]:
        """Yield (page number, records) newest-first until the corpus runs out.

        Ordering is decision-date descending. That is what makes a delta sync
        possible — read from page 0 and stop at the first page holding nothing
        new. It is the API's observed behaviour rather than a documented
        guarantee, so the store also de-duplicates on id.
        """
        n = start
        served = 0
        while max_pages is None or served < max_pages:
            records = await self.page(n)
            if not records:
                return
            yield n, records
            served += 1
            n += 1
            await asyncio.sleep(self._pause)

    async def courts(self) -> list[dict[str, Any]]:
        payload = (await self._get(DOMSTOLAR)).json()
        return payload if isinstance(payload, list) else []
