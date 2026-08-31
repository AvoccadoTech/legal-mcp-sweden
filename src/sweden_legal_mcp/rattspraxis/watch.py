"""Running the firm's watchlist against the mirror.

A watch is a saved query plus the firm's own matter reference. Checking the
watchlist means running each query, discarding what has already been reported,
and returning the rest ranked.

The ranking is deliberately not by significance alone. A routine MÖD decision
on the exact point a client's project turns on matters more than a prejudikat
about something else, so relevance to the watch comes first and significance
only breaks ties.
"""

from __future__ import annotations

from typing import Any

from .models import SIGNIFICANCE_RANK, Decision
from .store import Store


def run_watch(
    store: Store, watch: dict[str, Any], limit: int = 25, mark: bool = True
) -> list[Decision]:
    """New decisions matching one watch, newest first, unreported only."""
    hits = store.search(
        text=watch.get("text"),
        court=watch.get("court"),
        subject=watch.get("subject"),
        lagrum=watch.get("lagrum"),
        limit=limit * 4,  # over-fetch, since reported ones are filtered out below
    )
    fresh = [d for d in hits if not store.already_reported(watch["name"], d.id)]
    fresh.sort(
        key=lambda d: (
            d.decided or "",
            -SIGNIFICANCE_RANK.get(d.significance, 5),
        ),
        reverse=True,
    )
    fresh = fresh[:limit]
    if mark and fresh:
        store.mark_reported(watch["name"], [d.id for d in fresh])
    return fresh


def check_all(store: Store, limit_per_watch: int = 25, mark: bool = True) -> dict[str, Any]:
    watches = store.watches()
    results: list[dict[str, Any]] = []
    for w in watches:
        hits = run_watch(store, w, limit=limit_per_watch, mark=mark)
        if hits:
            results.append({"watch": w, "hits": hits})

    # Watches with the most significant hit first, so the alert opens on what
    # deserves attention rather than on whichever watch was created first.
    results.sort(
        key=lambda r: min(
            SIGNIFICANCE_RANK.get(d.significance, 5) for d in r["hits"]
        )
    )
    return {
        "watch_count": len(watches),
        "matched": results,
        "total_hits": sum(len(r["hits"]) for r in results),
        "marked": mark,
    }
