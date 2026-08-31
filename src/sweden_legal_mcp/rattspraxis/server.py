"""MCP server over Domstolsverket's published case law.

A monitor, not a research database. It mirrors the published corpus locally —
the API offers no search, so there is no other way — and then answers one
question repeatedly: has anything been decided that touches what this firm is
already working on?

The mirror and the watchlist both live on the firm's disk. What a firm watches
is a map of its matters, so it stays where the matters are.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import format as fmt
from . import watch as watchlib
from .client import RattspraxisClient, RattspraxisError
from .models import AddWatch, Decision, Search, Sync, SyncMode
from .store import Store

logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("sweden-rattspraxis")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITES_STATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def _db_path() -> Path:
    return Path(os.environ.get("RATTSPRAXIS_DB", ".rattspraxis")) / "rattspraxis.sqlite3"


def _store() -> Store:
    return Store(_db_path())


@mcp.tool(
    name="rattspraxis_sync",
    annotations=WRITES_STATE,
    description=(
        "Fetch published decisions from Domstolsverket into the local mirror.\n\n"
        "Run mode='full' once to build the mirror — about 1,700 requests over "
        "roughly 17,300 decisions, a few minutes. After that mode='delta' reads "
        "newest-first and stops as soon as it reaches decisions already stored, "
        "which takes seconds and is the right daily job.\n\n"
        "Everything else in this server reads the mirror, so nothing works "
        "properly until this has run at least once."
    ),
)
async def sync(mode: SyncMode = SyncMode.DELTA, max_pages: int | None = None) -> str:
    args = Sync(mode=mode, max_pages=max_pages)
    new = updated = pages = 0
    stop_early = False

    with _store() as store:
        async with RattspraxisClient() as client:
            try:
                corpus_total = await client.total_count()
            except RattspraxisError:
                corpus_total = 0
            if corpus_total:
                store.set_meta("corpus_total", str(corpus_total))

            async for page_no, records in client.pages(max_pages=args.max_pages):
                decisions = [Decision.from_api(r) for r in records]
                page_new, page_updated = store.upsert(decisions)
                new += page_new
                updated += page_updated
                pages += 1

                # A delta stops at the first page holding nothing new. The API
                # serves newest-first, so one fully-known page means everything
                # beyond it is known too.
                if args.mode is SyncMode.DELTA and page_new == 0 and page_no > 0:
                    stop_early = True
                    break

            store.set_meta(
                "last_sync", datetime.now(timezone.utc).isoformat(timespec="seconds")
            )
            held = store.coverage()["total"]

    return fmt.render_sync(
        new, updated, pages, corpus_total or None, held,
        partial=bool(args.max_pages and not stop_early),
    )


@mcp.tool(
    name="rattspraxis_search",
    annotations=READ_ONLY,
    description=(
        "Search the local mirror of published Swedish case law.\n\n"
        "Free text searches the decision summary, which is the only field "
        "present on every record — supports prefix search with * and AND/OR/NOT. "
        "Narrow with court code, rättsområde, statutory reference or decision "
        "date.\n\n"
        "Note what this covers: superior-court decisions and referat published "
        "by Domstolsverket. Most tingsrätt and hovrätt decisions are never "
        "published and are not here, and there is no legislation, no förarbeten "
        "and no commentary. An empty result means nothing matched in the "
        "mirror, not that Swedish law is silent."
    ),
)
async def search(
    text: str | None = None,
    court: str | None = None,
    subject: str | None = None,
    lagrum: str | None = None,
    decided_from: str | None = None,
    decided_to: str | None = None,
    limit: int = 20,
) -> str:
    args = Search(
        text=text, court=court, subject=subject, lagrum=lagrum,
        decided_from=decided_from, decided_to=decided_to, limit=limit,
    )
    with _store() as store:
        hits = store.search(
            text=args.text, court=args.court, subject=args.subject, lagrum=args.lagrum,
            decided_from=args.decided_from, decided_to=args.decided_to, limit=args.limit,
        )
    described = ", ".join(
        f"{k}={v}" for k, v in
        (("text", args.text), ("court", args.court), ("subject", args.subject),
         ("lagrum", args.lagrum), ("from", args.decided_from), ("to", args.decided_to))
        if v
    ) or "the whole mirror"
    return fmt.render_results(hits, described)


@mcp.tool(
    name="rattspraxis_get",
    annotations=READ_ONLY,
    description=(
        "One decision in full from the mirror, by the id returned in a search "
        "result — every field Domstolsverket publishes, including lagrum, "
        "nyckelord and referat number where present."
    ),
)
async def get(decision_id: str) -> str:
    with _store() as store:
        d = store.get(decision_id)
    if d is None:
        return (
            f"No decision with id `{decision_id}` in the mirror. Ids must come from a "
            "search result rather than be constructed, and the mirror may not be fully synced."
        )
    return fmt.render_decision(d)


@mcp.tool(
    name="rattspraxis_add_watch",
    annotations=WRITES_STATE,
    description=(
        "Watch for future decisions matching a query, on behalf of a matter.\n\n"
        "This is what the server is for. A watch is a saved search plus the "
        "firm's own matter reference, and `rattspraxis_check_watchlist` reports "
        "each match once.\n\n"
        "Free text is the term that carries the work — courts reason topically, "
        "so 'strandskydd', 'buller' or 'detaljplanekrav' will catch far more "
        "than a statutory reference will. Only about half of recent decisions "
        "carry a lagrum at all."
    ),
)
async def add_watch(
    name: str,
    text: str | None = None,
    court: str | None = None,
    subject: str | None = None,
    lagrum: str | None = None,
    matter: str | None = None,
) -> str:
    args = AddWatch(name=name, text=text, court=court, subject=subject, lagrum=lagrum, matter=matter)
    if not any([args.text, args.court, args.subject, args.lagrum]):
        raise ValueError(
            "A watch needs at least one of text, court, subject or lagrum — otherwise it "
            "matches the entire corpus and reports everything once."
        )
    with _store() as store:
        store.add_watch(
            name=args.name, text=args.text, court=args.court, subject=args.subject,
            lagrum=args.lagrum, matter=args.matter,
        )
        existing = store.search(
            text=args.text, court=args.court, subject=args.subject, lagrum=args.lagrum, limit=5
        )
    out = [f"Watching **{args.name}**."]
    if existing:
        out.append(
            f"\n{len(existing)} decision(s) already in the mirror match it. They will be "
            "reported on the next `rattspraxis_check_watchlist`, so the first run after "
            "adding a watch reflects history rather than news:\n"
        )
        out.extend(f"- {d.case_label} ({d.court_code}, {d.decided}) — {d.summary[:160]}" for d in existing)
    else:
        out.append("\nNothing in the mirror matches it yet.")
    return "\n".join(out)


@mcp.tool(
    name="rattspraxis_check_watchlist",
    annotations=WRITES_STATE,
    description=(
        "Run every watch against the mirror and report matches not yet seen.\n\n"
        "The daily alert. Each decision is reported once per watch. Results are "
        "ordered so the most significant hit surfaces first, but relevance to "
        "the watch comes before significance — a routine decision on the exact "
        "point a matter turns on beats a prejudikat about something else.\n\n"
        "Run rattspraxis_sync first, or this reports on yesterday's mirror."
    ),
)
async def check_watchlist(mark_reported: bool = True, limit_per_watch: int = 25) -> str:
    with _store() as store:
        return fmt.render_watch_check(
            watchlib.check_all(store, limit_per_watch=limit_per_watch, mark=mark_reported)
        )


@mcp.tool(
    name="rattspraxis_list_watches",
    annotations=READ_ONLY,
    description="Show every watch, its query terms and the matter it belongs to.",
)
async def list_watches() -> str:
    with _store() as store:
        return fmt.render_watches(store.watches())


@mcp.tool(
    name="rattspraxis_remove_watch",
    annotations=WRITES_STATE,
    description=(
        "Stop watching. Also clears the record of what that watch has already "
        "reported, so re-adding it later starts from scratch."
    ),
)
async def remove_watch(name: str) -> str:
    with _store() as store:
        removed = store.remove_watch(name)
    return f"Removed watch **{name}**." if removed else f"No watch named **{name}**."


@mcp.tool(
    name="rattspraxis_coverage",
    annotations=READ_ONLY,
    description=(
        "What the mirror actually holds — how many decisions, over what period, "
        "from which courts and subjects, and how complete it is against the "
        "count Domstolsverket reports.\n\n"
        "Check this before treating an empty search result as meaningful, and "
        "before anyone treats this server as a substitute for a legal database. "
        "It holds roughly 17,000 published decisions and no commentary."
    ),
)
async def coverage() -> str:
    with _store() as store:
        return fmt.render_coverage(store.coverage())


@mcp.tool(
    name="rattspraxis_courts",
    annotations=READ_ONLY,
    description=(
        "List every court publishing to Domstolsverket, with the code used to "
        "narrow a search or a watch."
    ),
)
async def courts() -> str:
    async with RattspraxisClient() as client:
        return fmt.render_courts(await client.courts())


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
