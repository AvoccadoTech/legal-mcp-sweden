"""MCP server over Lifos — Migrationsverket's legal-position database.

It answers one question: has a legal position our knowledge base depends on
been superseded?

There is no account, no key and no service in the middle. The server runs on
the firm's machine, reads a public RSS feed, and keeps its ledger in a JSON
file the firm owns. Which ställningstaganden a firm watches is a map of its
matters, and it is not ours to hold.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import format as fmt
from . import kb as kbscan
from .client import FEED_LABELS, LifosClient, LifosError, feeds_for
from .ledger import Ledger
from .models import (
    CheckUpdates,
    Feed,
    ScanKnowledgeBase,
    TrackDocument,
    UntrackDocument,
)

# httpx logs every request at INFO. Those lines carry the feed URLs and, for
# document fetches, which position was being read — the one thing this server
# exists not to record.
logging.getLogger("httpx").setLevel(logging.WARNING)

mcp = FastMCP("sweden-lifos")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITES_STATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)

_client: LifosClient | None = None


def _get_client() -> LifosClient:
    global _client
    if _client is None:
        _client = LifosClient()
    return _client


def _state_file() -> Path:
    """The ledger lives where the operator says, defaulting to ./.lifos.

    Point LIFOS_STATE_DIR at the firm's own storage — beside the knowledge base
    is the natural place, so the two travel together and neither depends on us.
    """
    return Path(os.environ.get("LIFOS_STATE_DIR", ".lifos")) / "lifos-state.json"


def _ledger() -> Ledger:
    return Ledger.load(_state_file())


@mcp.tool(
    name="lifos_check_updates",
    annotations=WRITES_STATE,
    description=(
        "Poll Lifos and report what has changed since the last check — new "
        "rättsliga ställningstaganden and rättsliga kommentarer, and existing "
        "ones republished at a higher version.\n\n"
        "This is the daily job. Lifos serves a five-item rolling window with no "
        "archive, so a slow polling cadence loses changes silently and they "
        "cannot be recovered afterwards.\n\n"
        "Writes what it saw to the local ledger so the same items are not "
        "reported as new next time. Pass mark_seen=false for a dry run."
    ),
)
async def check_updates(feed: Feed = Feed.LEGAL, mark_seen: bool = True) -> str:
    args = CheckUpdates(feed=feed, mark_seen=mark_seen)
    client = _get_client()
    ledger = _ledger()

    events: list[dict] = []
    errors: list[dict[str, str]] = []
    polled: dict[str, int] = {}

    for f in feeds_for(args.feed):
        try:
            items = await client.read_feed(f)
        except LifosError as exc:
            errors.append({"feed": f.value, "error": str(exc)})
            continue
        polled[f.value] = len(items)
        for item in items:
            if event := ledger.observe(item):
                events.append(event)
        ledger.record_poll(f.value, len(items))

    # Persist whenever anything was read. A feed that failed must not cause the
    # feed that succeeded to re-report its items as new on the next run.
    persisted = bool(args.mark_seen and polled)
    if persisted:
        ledger.save()

    return fmt.render_changes(events, polled, errors, persisted)


@mcp.tool(
    name="lifos_recent",
    annotations=READ_ONLY,
    description=(
        "Show what Lifos is publishing right now, without touching the ledger.\n\n"
        "Use this to look at the feed. It does not say whether anything is new "
        "to this firm — lifos_check_updates answers that, and is the tool to use "
        "for monitoring."
    ),
)
async def recent(feed: Feed = Feed.LEGAL) -> str:
    client = _get_client()
    items = []
    errors: list[dict[str, str]] = []
    for f in feeds_for(feed):
        try:
            items.extend(await client.read_feed(f))
        except LifosError as exc:
            errors.append({"feed": f.value, "error": str(exc)})
    items.sort(key=lambda i: (i.published or ""), reverse=True)
    return fmt.render_items(items, errors)


@mcp.tool(
    name="lifos_scan_kb",
    annotations=WRITES_STATE,
    description=(
        "Scan the firm's knowledge base for references to Lifos documents "
        "(identifiers in the form RS/001/2024 or RK/005/2026) and register each "
        "one as a dependency, together with the version that entry cites.\n\n"
        "This is what makes lifos_stale_positions meaningful: without it the "
        "watcher only knows what Migrationsverket published, not which of the "
        "firm's own positions rest on it.\n\n"
        "Reads markdown, text and YAML files. It never writes to the knowledge "
        "base. A citation that names no version is reported as an unpinned gap "
        "rather than guessed at."
    ),
)
async def scan_kb(kb_path: str | None = None, register: bool = True) -> str:
    args = ScanKnowledgeBase(kb_path=kb_path, register=register)
    root = args.kb_path or os.environ.get("LIFOS_KB_PATH")
    if not root:
        raise ValueError(
            "No knowledge-base path given and LIFOS_KB_PATH is not set. Call "
            "lifos_scan_kb with kb_path set to the root of the firm's knowledge base."
        )

    rows = kbscan.scan(root)
    if args.register_findings and rows:
        ledger = _ledger()
        for r in rows:
            ledger.track(r["document_id"], r["kb_file"], r["cited_version"])
        ledger.save()

    return fmt.render_scan(str(root), rows, registered=bool(args.register_findings and rows))


@mcp.tool(
    name="lifos_stale_positions",
    annotations=READ_ONLY,
    description=(
        "List the knowledge-base entries that cite a superseded version of a "
        "Lifos document — the firm says one thing, Migrationsverket has since "
        "moved.\n\n"
        "Each row names the file to revisit, the version it cites and the "
        "version now in force. It does not say what changed or whether the "
        "change matters to any matter; a lawyer decides that."
    ),
)
async def stale_positions() -> str:
    ledger = _ledger()
    return fmt.render_stale(
        ledger.stale(),
        tracked=len(ledger.data["tracked"]),
        known=len(ledger.data["documents"]),
    )


@mcp.tool(
    name="lifos_track",
    annotations=WRITES_STATE,
    description=(
        "Watch one Lifos document on behalf of one knowledge-base entry.\n\n"
        "Use this when the knowledge base depends on a ställningstagande that "
        "lifos_scan_kb cannot find — typically because the entry names it in "
        "prose rather than by identifier. Give the version the entry was "
        "written against, or it can never be reported stale."
    ),
)
async def track(document_id: str, kb_file: str, cited_version: str | None = None) -> str:
    args = TrackDocument(document_id=document_id, kb_file=kb_file, cited_version=cited_version)
    ledger = _ledger()
    ledger.track(args.document_id, args.kb_file, args.cited_version)
    ledger.save()

    doc = ledger.known(args.document_id)
    lines = [
        f"Watching `{args.document_id}` for `{args.kb_file}` "
        f"(cites {args.cited_version or 'no version'})."
    ]
    if doc:
        lines.append(f"\nCurrent known version: **{doc.get('latest_version') or '—'}**.")
    else:
        lines.append(
            "\nThis document has not appeared in the feed yet, so there is no baseline "
            "to compare against until it is republished."
        )
    if not args.cited_version:
        lines.append(
            "\nNo cited version was given, so this entry will never be reported stale. "
            "Add one to make it watchable."
        )
    return "\n".join(lines)


@mcp.tool(
    name="lifos_untrack",
    annotations=WRITES_STATE,
    description=(
        "Stop watching a Lifos document — for one knowledge-base entry, or for "
        "all of them if kb_file is omitted."
    ),
)
async def untrack(document_id: str, kb_file: str | None = None) -> str:
    args = UntrackDocument(document_id=document_id, kb_file=kb_file)
    ledger = _ledger()
    removed = ledger.untrack(args.document_id, args.kb_file)
    ledger.save()
    if not removed:
        return f"`{args.document_id}` was not being watched" + (
            f" for `{args.kb_file}`." if args.kb_file else "."
        )
    return f"Stopped watching `{args.document_id}` — {removed} dependency record(s) removed."


@mcp.tool(
    name="lifos_list_tracked",
    annotations=READ_ONLY,
    description=(
        "Show what is being watched: each Lifos document, the version currently "
        "known, and the knowledge-base entries that depend on it with the "
        "version each cites."
    ),
)
async def list_tracked() -> str:
    ledger = _ledger()
    rows = []
    for doc_id, entries in sorted(ledger.data["tracked"].items()):
        doc = ledger.known(doc_id) or {}
        rows.append(
            {
                "document_id": doc_id,
                "current_version": doc.get("latest_version"),
                "seen_in_feed": bool(doc),
                "dependents": [
                    {"kb_file": e["kb_file"], "cited_version": e.get("cited_version")}
                    for e in entries
                ],
            }
        )
    return fmt.render_tracked(rows, str(_state_file()), ledger.data.get("polls", {}))


@mcp.tool(
    name="lifos_get_document",
    annotations=READ_ONLY,
    description=(
        "Fetch a Lifos document page as text, using a URL taken from a feed "
        "item, so the republished position can actually be read and compared "
        "with what the knowledge base says.\n\n"
        "Only lifos.migrationsverket.se URLs are fetched. Many positions are "
        "published as PDF; the links to those are returned alongside the text."
    ),
)
async def get_document(url: str) -> str:
    import re

    client = _get_client()
    raw = await client.get_page(url)

    body = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", raw)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"&nbsp;?", " ", body)
    body = re.sub(r"[ \t ]+", " ", body)
    body = re.sub(r"\n\s*\n\s*\n+", "\n\n", body).strip()

    pdfs = sorted(set(re.findall(r'href="([^"]+\.pdf)"', raw)))
    limit = 20000
    out = [body[:limit]]
    if len(body) > limit:
        out.append(f"\n\n*(truncated at {limit:,} characters)*")
    if pdfs:
        out.append("\n\n**Attached documents**\n")
        out.extend(f"- {p}" for p in pdfs[:20])
    return "".join(out) if len(out) == 1 else "\n".join(out)


@mcp.tool(
    name="lifos_list_feeds",
    annotations=READ_ONLY,
    description=(
        "List the Lifos feeds this server reads and what each carries. Useful "
        "for deciding whether a question is about legal positions or about "
        "country-of-origin information."
    ),
)
async def list_feeds() -> str:
    lines = ["| Feed | Carries |", "|---|---|"]
    lines.extend(f"| `{f.value}` | {label} |" for f, label in FEED_LABELS.items())
    lines.append(
        "\nUse `legal` for anything a knowledge base would cite. `country` is "
        "landinformation — reporting on conditions in a country, not a legal position."
    )
    return "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
