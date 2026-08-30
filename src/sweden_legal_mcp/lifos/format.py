"""Rendering results for an agent to read.

Two rules shape everything here.

Say what is not known. Lifos serves a five-item rolling window, so every answer
about change carries the limits of the evidence behind it. A watcher that
reports "nothing has changed" without saying what it could not see is worse
than no watcher, because it converts a gap into false assurance.

Never imply legal judgement. The output says a position has been republished at
a higher version and names the file that cites the older one. Whether that
matters to a matter is a lawyer's call, and the wording never pretends
otherwise.
"""

from __future__ import annotations

from typing import Any

from .models import FeedItem

COVERAGE_NOTE = (
    "Lifos publishes a five-item rolling RSS window, not an archive, and offers no "
    "backfill. This watcher sees a change only if it polls while the item is still in "
    "the window — poll daily — and it can report a document as superseded only once it "
    "has observed the newer version itself. It is a change sensor, not a completeness "
    "guarantee; the firm's own review discipline still governs."
)

JUDGEMENT_NOTE = (
    "A higher version number means the document was republished. It does not say what "
    "changed or whether it matters. Read the new version before acting."
)


def _item_line(item: FeedItem) -> str:
    ident = item.document_id or "—"
    version = f"v{item.version}" if item.version else "no version stated"
    return f"| {item.published or '—'} | `{ident}` | {version} | {item.title} |"


def render_changes(
    events: list[dict[str, Any]],
    polled: dict[str, int],
    errors: list[dict[str, str]],
    persisted: bool,
) -> str:
    lines: list[str] = []

    if errors:
        lines.append("**Some feeds could not be read.**\n")
        for e in errors:
            lines.append(f"- `{e['feed']}` — {e['error']}")
        lines.append("")

    read = ", ".join(f"{k} ({v} items)" for k, v in polled.items()) or "nothing"
    lines.append(f"Read: {read}.")

    if not events:
        lines.append(
            "\nNothing new since the last check. That means nothing new *in the window* — "
            "see the note below."
        )
    else:
        new_docs = [e for e in events if e["event"] == "new_document"]
        new_versions = [e for e in events if e["event"] == "new_version"]

        if new_versions:
            lines.append(f"\n### Republished at a higher version ({len(new_versions)})\n")
            lines.append("| Date | Document | Was → now | Title |")
            lines.append("|---|---|---|---|")
            for e in new_versions:
                item: FeedItem = e["item"]
                lines.append(
                    f"| {item.published or '—'} | `{item.document_id or '—'}` | "
                    f"{e['previous_version'] or '—'} → {item.version or '—'} | {item.title} |"
                )
            lines.append(
                "\nRun `lifos_stale_positions` to see which knowledge-base entries cite "
                "a superseded version."
            )

        if new_docs:
            lines.append(f"\n### Not seen before ({len(new_docs)})\n")
            lines.append("| Date | Document | Version | Title |")
            lines.append("|---|---|---|---|")
            lines.extend(_item_line(e["item"]) for e in new_docs)
            lines.append(
                "\nOn a first run everything is new — that is the ledger establishing a "
                "baseline, not Migrationsverket having published five things at once."
            )

    if not persisted:
        lines.append("\n*Dry run: the ledger was not written, so these will be reported again.*")

    lines.append(f"\n---\n\n{COVERAGE_NOTE}")
    return "\n".join(lines)


def render_items(items: list[FeedItem], errors: list[dict[str, str]]) -> str:
    lines: list[str] = []
    if errors:
        for e in errors:
            lines.append(f"**{e['feed']}** could not be read — {e['error']}\n")
    if not items:
        lines.append("The feed returned no items.")
    else:
        lines.append("| Date | Document | Version | Title |")
        lines.append("|---|---|---|---|")
        lines.extend(_item_line(i) for i in items)
    lines.append(
        "\nThis is what Lifos is publishing right now. It does not say whether any of it "
        "is new to this firm — `lifos_check_updates` answers that."
    )
    lines.append(f"\n{COVERAGE_NOTE}")
    return "\n".join(lines)


def render_scan(
    kb_path: str, rows: list[dict[str, Any]], registered: bool
) -> str:
    if not rows:
        return (
            f"No Lifos references found under `{kb_path}`.\n\n"
            "The scan looks for identifiers in the form `RS/001/2024` or `RK/005/2026` in "
            "markdown, text and YAML files. A knowledge base that refers to a "
            "ställningstagande by name only will not be found — register those with "
            "`lifos_track`."
        )

    unpinned = [r for r in rows if not r["cited_version"]]
    documents = sorted({r["document_id"] for r in rows})

    lines = [
        f"Found **{len(rows)}** citation(s) of **{len(documents)}** Lifos document(s) "
        f"under `{kb_path}`.\n",
        "| Document | Knowledge-base entry | Line | Cites version |",
        "|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['document_id']}` | `{r['kb_file']}` | {r['line']} | "
            f"{r['cited_version'] or '**not pinned**'} |"
        )

    lines.append(
        f"\n{'Registered for watching.' if registered else 'Reported only — nothing was registered.'}"
    )

    if unpinned:
        lines.append(
            f"\n**{len(unpinned)} citation(s) name no version.** They can never be reported "
            "stale, because there is no baseline to compare a new version against. Pin the "
            "version in the knowledge-base entry — writing `(version 2.0)` next to the "
            "identifier is enough — or register it with `lifos_track`."
        )
    return "\n".join(lines)


def render_stale(rows: list[dict[str, Any]], tracked: int, known: int) -> str:
    if not rows:
        if not tracked:
            return (
                "Nothing is being watched yet, so there is nothing to report.\n\n"
                "Run `lifos_scan_kb` to register the Lifos documents the knowledge base "
                "already cites."
            )
        return (
            f"None of the {tracked} watched document(s) has been observed to move.\n\n"
            "That is not the same as the knowledge base being correct — it means nothing "
            f"watched has been republished at a higher version since this ledger started "
            f"observing ({known} document(s) seen so far).\n\n"
            f"{COVERAGE_NOTE}"
        )

    lines = [
        f"**{len(rows)} knowledge-base "
        f"{'entry cites' if len(rows) == 1 else 'entries cite'} a superseded version.**\n",
        "| Document | Knowledge-base entry | Cites | Current | Republished |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['document_id']}` | `{r['kb_file']}` | {r['kb_cites_version'] or '—'} | "
            f"**{r['current_version'] or '—'}** | {r['published'] or '—'} |"
        )

    lines.append("\n**Documents**\n")
    seen: set[str] = set()
    for r in rows:
        if r["document_id"] in seen:
            continue
        seen.add(r["document_id"])
        lines.append(f"- `{r['document_id']}` — {r['title'] or '—'}")
        if r["url"]:
            lines.append(f"  {r['url']}")

    lines.append(f"\n{JUDGEMENT_NOTE}")
    lines.append(f"\n{COVERAGE_NOTE}")
    return "\n".join(lines)


def render_tracked(rows: list[dict[str, Any]], state_file: str, polls: dict[str, Any]) -> str:
    if not rows:
        return (
            "Nothing is being watched.\n\n"
            f"State file: `{state_file}`\n\n"
            "Run `lifos_scan_kb` to register what the knowledge base already cites."
        )

    lines = [
        "| Document | Current version | Seen in feed | Knowledge-base entries |",
        "|---|---|---|---|",
    ]
    for r in rows:
        deps = "; ".join(
            f"`{d['kb_file']}` (cites {d['cited_version'] or 'no version'})"
            for d in r["dependents"]
        )
        lines.append(
            f"| `{r['document_id']}` | {r['current_version'] or '—'} | "
            f"{'yes' if r['seen_in_feed'] else 'not yet'} | {deps} |"
        )

    lines.append(f"\nState file: `{state_file}`")
    if polls:
        last = "; ".join(f"{k} at {v.get('at', '—')}" for k, v in polls.items())
        lines.append(f"\nLast polled: {last}")
    lines.append(
        "\nA document marked *not yet* has not appeared in the feed since this ledger "
        "started, so there is no baseline for it until it is republished."
    )
    return "\n".join(lines)
