"""Rendering case-law results for an agent to read.

One rule governs this file: never let the mirror look bigger than it is.

It holds roughly 17,000 published decisions. JUNO holds over two million, plus
the commentaries. Presented as a research database this loses the moment a
lawyer searches for a hovrätt case and finds nothing; presented as a monitor
over what the firm has asked to watch, it does something no subscription does.
Every empty result therefore says what was *not* searched, not just that
nothing matched.
"""

from __future__ import annotations

from typing import Any

from .models import SIGNIFICANCE_LABEL, Decision

SCOPE_NOTE = (
    "This is Domstolsverket's published case law only — superior-court decisions and "
    "referat. It is not a complete record of Swedish case law: most tingsrätt and "
    "hovrätt decisions are never published, and this holds no legislation, no "
    "förarbeten and no commentary. It complements a legal database; it does not replace one."
)


def _row(d: Decision) -> str:
    subj = "; ".join(d.subjects) or "—"
    return (
        f"| {d.decided or '—'} | {d.court_code} | {d.case_label} | "
        f"{SIGNIFICANCE_LABEL.get(d.significance, '—')} | {subj} |"
    )


def render_results(decisions: list[Decision], described: str) -> str:
    if not decisions:
        return (
            f"No decisions in the mirror for {described}.\n\n"
            "Before concluding there is nothing: check `rattspraxis_coverage` — if the "
            "mirror has not been synced, or was synced only partially, it cannot answer. "
            f"Absence here is not absence in Swedish law.\n\n{SCOPE_NOTE}"
        )

    lines = [
        f"**{len(decisions)}** decision(s) for {described}.\n",
        "| Decided | Court | Case | Weight | Rättsområde |",
        "|---|---|---|---|---|",
    ]
    lines.extend(_row(d) for d in decisions)
    lines.append("\n**Summaries**\n")
    for d in decisions:
        lines.append(f"- **{d.case_label}** ({d.court_code}, {d.decided or '—'}) — {d.summary}")
        if d.lagrum:
            lines.append(f"  - lagrum: {'; '.join(d.lagrum[:4])}")
    lines.append(f"\n{SCOPE_NOTE}")
    return "\n".join(lines)


def render_decision(d: Decision) -> str:
    lines = [
        f"### {d.case_label} — {d.court_name}",
        "",
        f"- **Decided:** {d.decided or '—'}   **Published:** {d.published or '—'}",
        f"- **Weight:** {SIGNIFICANCE_LABEL.get(d.significance, '—')}   **Form:** {d.form or '—'}",
    ]
    if d.subjects:
        lines.append(f"- **Rättsområde:** {'; '.join(d.subjects)}")
    if d.referat:
        lines.append(f"- **Referat:** {'; '.join(d.referat)}")
    if d.lagrum:
        lines.append(f"- **Lagrum:** {'; '.join(d.lagrum)}")
    if d.sfs:
        lines.append(f"- **SFS:** {', '.join(d.sfs)}")
    if d.keywords:
        lines.append(f"- **Nyckelord:** {'; '.join(d.keywords)}")
    lines += ["", d.summary or "_No summary published._"]
    if d.attachments:
        lines.append(
            f"\n_{d.attachments} document(s) attached at Domstolsverket. The full text is the "
            "decision; this summary is Domstolsverket's, not the court's reasoning._"
        )
    return "\n".join(lines)


def render_sync(
    new: int, updated: int, pages: int, corpus_total: int | None, held: int, partial: bool
) -> str:
    lines = [
        f"Synced {pages} page(s): **{new} new**, {updated} refreshed.",
        f"\nThe mirror now holds **{held:,}** decisions.",
    ]
    if corpus_total:
        pct = 100 * held / corpus_total
        lines.append(
            f"Domstolsverket reports **{corpus_total:,}** publications, so the mirror is "
            f"**{pct:.1f}%** complete."
        )
        if pct < 99:
            lines.append(
                "\nRun `rattspraxis_sync` with mode='full' to finish it. A partial mirror "
                "answers searches confidently and wrongly — it cannot distinguish "
                "*nothing matched* from *not fetched yet*."
            )
    if partial:
        lines.append("\n*Stopped early at max_pages — this was a sample, not a sync.*")
    return "\n".join(lines)


def render_coverage(c: dict[str, Any]) -> str:
    if not c.get("total"):
        return (
            "The mirror is empty. Run `rattspraxis_sync` with mode='full' first — "
            "roughly 1,700 requests, a few minutes.\n\n" + SCOPE_NOTE
        )

    lines = [
        f"**{c['total']:,} decisions**, {c['earliest']} → {c['latest']}.",
        f"\nStored at `{c['path']}`.",
    ]
    if c.get("corpus_total_reported"):
        try:
            reported = int(c["corpus_total_reported"])
            lines.append(
                f"Domstolsverket reports {reported:,} publications — "
                f"**{100 * c['total'] / reported:.1f}%** mirrored."
            )
        except (TypeError, ValueError):
            pass
    if c.get("last_sync"):
        lines.append(f"Last synced {c['last_sync']}.")

    lines.append("\n**Courts**\n")
    lines.append("| Court | Decisions |")
    lines.append("|---|---|")
    lines.extend(f"| {n} | {v:,} |" for n, v in c["courts"])

    if c.get("subjects"):
        lines.append("\n**Rättsområden**\n")
        lines.append("| Rättsområde | Decisions |")
        lines.append("|---|---|")
        lines.extend(f"| {n} | {v:,} |" for n, v in c["subjects"])

    total = c["total"]
    lines.append(
        f"\n**Field coverage** — {c['with_subject']:,} ({100 * c['with_subject'] / total:.0f}%) "
        f"carry a rättsområde, {c['with_lagrum']:,} ({100 * c['with_lagrum'] / total:.0f}%) a "
        "statutory reference. Only the summary is populated for every record, which is why "
        "free text is the reliable way to search this and lagrum is a filter, not a key."
    )
    lines.append(f"\n{SCOPE_NOTE}")
    return "\n".join(lines)


def render_watch_check(result: dict[str, Any]) -> str:
    if not result["watch_count"]:
        return (
            "No watches are set.\n\nAdd one with `rattspraxis_add_watch` — a free-text term, "
            "optionally narrowed to a court or rättsområde, and the firm's own matter "
            "reference so an alert can be routed to whoever owns it."
        )
    if not result["matched"]:
        return (
            f"None of the {result['watch_count']} watch(es) matched anything new.\n\n"
            "That covers what has been synced into the mirror — run `rattspraxis_sync` "
            "first if it has not run today."
        )

    lines = [
        f"**{result['total_hits']} new decision(s)** across "
        f"{len(result['matched'])} of {result['watch_count']} watch(es).\n"
    ]
    for entry in result["matched"]:
        w = entry["watch"]
        header = f"### {w['name']}"
        if w.get("matter"):
            header += f"  ·  {w['matter']}"
        lines.append(header)
        terms = [f"{k}: {w[k]}" for k in ("text", "court", "subject", "lagrum") if w.get(k)]
        lines.append(f"_{' · '.join(terms)}_\n")
        for d in entry["hits"]:
            lines.append(
                f"- **{d.case_label}** ({d.court_code}, {d.decided or '—'}, "
                f"{SIGNIFICANCE_LABEL.get(d.significance, '—')})\n  {d.summary}"
            )
        lines.append("")

    if result.get("marked"):
        lines.append("_These are now marked as reported and will not appear again._")
    lines.append(
        "\nA match means the words line up, not that the decision matters. Read it before "
        "telling a client anything."
    )
    return "\n".join(lines)


def render_watches(watches: list[dict[str, Any]]) -> str:
    if not watches:
        return "No watches are set."
    lines = ["| Watch | Terms | Court | Rättsområde | Lagrum | Matter |", "|---|---|---|---|---|---|"]
    for w in watches:
        lines.append(
            f"| {w['name']} | {w.get('text') or '—'} | {w.get('court') or '—'} | "
            f"{w.get('subject') or '—'} | {w.get('lagrum') or '—'} | {w.get('matter') or '—'} |"
        )
    return "\n".join(lines)


def render_courts(courts: list[dict[str, Any]]) -> str:
    lines = ["| Code | Court |", "|---|---|"]
    lines.extend(
        f"| `{c.get('domstolKod')}` | {c.get('domstolNamn')} |"
        for c in sorted(courts, key=lambda c: c.get("domstolNamn") or "")
    )
    lines.append(
        "\nMost of what is published now comes from Mark- och miljööverdomstolen (`MMOD`), "
        "Högsta domstolen (`HD`) and Högsta förvaltningsdomstolen (`HFD`)."
    )
    return "\n".join(lines)
