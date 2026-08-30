"""HTTP access to the Lifos RSS feeds.

Lifos publishes no JSON API. What it does publish — and what makes this
connector possible at all — is an RSS feed whose item titles carry the document
identifier and its version number:

    Rättsligt ställningstagande. Uppehållstillstånd för besök
    - RS/001/2024 (version 3.0)

For a currency check that is better than a document API would have been: the
version is the whole signal, and it arrives without having to diff any text.

The feed is a five-item rolling window with no archive and no backfill
endpoint. Poll daily.
"""

from __future__ import annotations

import asyncio
import random
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from .models import Feed, FeedItem

USER_AGENT = "legal-mcp-sweden/0.1 (+https://github.com/AvoccadoTech/legal-mcp-sweden)"

# Lifos runs on Sitevision. The landinformation feed has a clean alias; the
# rättsavdelningen feed is reachable only through its portlet URL, and that is
# the fragile part of this connector. If it starts returning 404, re-read the
# feed links on https://lifos.migrationsverket.se/ and update this map.
FEED_URLS: dict[Feed, str] = {
    Feed.LEGAL: (
        "https://lifos.migrationsverket.se/2.339945412c72922c2280000/"
        "12.39a9cd9514a346077211abc.portlet"
        "?state=rss&sv.contenttype=text/xml;charset=UTF-8"
    ),
    Feed.COUNTRY: "https://lifos.migrationsverket.se/rss",
}

FEED_LABELS: dict[Feed, str] = {
    Feed.LEGAL: "Rättsavdelningen — rättsliga ställningstaganden och rättsliga kommentarer",
    Feed.COUNTRY: "Landinformation — country of origin information",
}

DOCUMENT_HOST = "lifos.migrationsverket.se"


class LifosError(RuntimeError):
    """Raised with a message an agent can act on, not just a status code."""


def feeds_for(feed: Feed) -> list[Feed]:
    return [Feed.LEGAL, Feed.COUNTRY] if feed is Feed.ALL else [feed]


def _iso_date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError):
        return raw[:10] or None


def parse_feed(feed: Feed, xml: str) -> list[FeedItem]:
    """Parse one RSS document into items. Kept separate from fetching so the
    title-extraction rules can be tested without touching the network."""
    moved = (
        "The portlet URL may have moved — check the feed links on "
        "https://lifos.migrationsverket.se/ and update FEED_URLS."
    )
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise LifosError(
            f"Lifos returned something that is not valid XML for the {feed.value} feed: {exc}. "
            + moved
        ) from exc

    # A Sitevision error page is valid XML and parses cleanly, so a successful
    # parse proves nothing. Only the root tag does. Without this check a moved
    # feed reads as "no new items" — the failure mode that matters, because it
    # is indistinguishable from a quiet month.
    if root.tag.rsplit("}", 1)[-1].lower() not in {"rss", "rdf", "feed"}:
        raise LifosError(
            f"Lifos served a <{root.tag}> document rather than RSS for the "
            f"{feed.value} feed. " + moved
        )

    items: list[FeedItem] = []
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        if not title:
            continue
        items.append(
            FeedItem.from_title(
                feed=feed,
                title=title,
                url=(node.findtext("link") or node.findtext("guid") or "").strip(),
                published=_iso_date(node.findtext("pubDate")),
            )
        )
    return items


class LifosClient:
    """Reads the feeds. Holds no state about what was read — that is the
    ledger's job, and the ledger lives on the firm's disk."""

    def __init__(self, timeout: float = 30.0, max_retries: int = 3) -> None:
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml, text/xml",
            },
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LifosClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, url: str) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = await self._client.get(url)
            except httpx.RequestError as exc:
                last = exc
                await asyncio.sleep(min(2.0**attempt, 8.0) + random.uniform(0, 0.4))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last = LifosError(f"Lifos returned {response.status_code}")
                await asyncio.sleep(min(2.0**attempt, 8.0) + random.uniform(0, 0.4))
                continue
            if response.status_code == 404:
                raise LifosError(
                    f"Lifos returned 404 for {url}. If this is the legal feed, its "
                    "Sitevision portlet URL has moved — re-read the feed links on "
                    "https://lifos.migrationsverket.se/ and update FEED_URLS."
                )
            if response.status_code >= 400:
                raise LifosError(f"Lifos rejected the request ({response.status_code}) for {url}")
            return response

        raise LifosError(f"Lifos unreachable after {self._max_retries} attempts: {last}")

    async def read_feed(self, feed: Feed) -> list[FeedItem]:
        if feed is Feed.ALL:
            raise ValueError("read_feed takes one feed; use feeds_for() to expand ALL")
        response = await self._get(FEED_URLS[feed])
        return parse_feed(feed, response.text)

    async def get_page(self, url: str) -> str:
        """Fetch a Lifos document page. Only lifos.migrationsverket.se is allowed —
        a feed link is the only thing that should ever be passed here."""
        normalised = url.replace("http://", "https://", 1)
        host = httpx.URL(normalised).host
        if host != DOCUMENT_HOST:
            raise LifosError(
                f"Refusing to fetch {host or url!r}. This tool reads "
                f"{DOCUMENT_HOST} pages only, using a URL from a feed item."
            )
        return (await self._get(normalised)).text
