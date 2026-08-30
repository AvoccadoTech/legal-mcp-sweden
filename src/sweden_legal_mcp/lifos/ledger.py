"""Local state: what has been seen, and what the knowledge base depends on.

This file is the whole reason the server is stdio and not hosted.

Which ställningstaganden a firm watches, and which of its own files depend on
them, is a map of the firm's matters. It is not ours to hold. So the ledger is
a plain JSON file on the firm's disk, readable by the people it describes, and
nothing in this package sends it anywhere.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import FeedItem, is_newer

STATE_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Ledger:
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Ledger":
        p = Path(path)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"The watcher state at {p} is not readable JSON ({exc}). It holds "
                    "what has already been seen; move it aside rather than deleting it, "
                    "then re-run the scan."
                ) from exc
        else:
            data = {"version": STATE_VERSION}
        data.setdefault("documents", {})
        data.setdefault("tracked", {})
        data.setdefault("polls", {})
        return cls(path=p, data=data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- observing the feed -------------------------------------------------- #

    def observe(self, item: FeedItem) -> dict[str, Any] | None:
        """Record one feed item. Returns an event when something actually changed.

        Two events are worth reporting: a document never seen before, and a
        document republished at a higher version. A republication at the same
        version is not news.
        """
        docs = self.data["documents"]
        known = docs.get(item.key)

        if known is None:
            docs[item.key] = {
                "document_id": item.document_id,
                "kind": item.kind.value,
                "title": item.title,
                "url": item.url,
                "latest_version": item.version,
                "latest_published": item.published,
                "first_seen": _now(),
                "last_seen": _now(),
                "history": [
                    {"version": item.version, "published": item.published, "url": item.url}
                ],
            }
            return {"event": "new_document", "item": item}

        known["last_seen"] = _now()
        if is_newer(item.version, known.get("latest_version")):
            previous = known.get("latest_version")
            known.update(
                {
                    "title": item.title,
                    "url": item.url,
                    "latest_version": item.version,
                    "latest_published": item.published,
                }
            )
            known.setdefault("history", []).append(
                {"version": item.version, "published": item.published, "url": item.url}
            )
            return {"event": "new_version", "previous_version": previous, "item": item}

        if (
            item.published
            and item.version == known.get("latest_version")
            and item.published != known.get("latest_published")
        ):
            known["latest_published"] = item.published
        return None

    def record_poll(self, feed: str, count: int) -> None:
        self.data["polls"][feed] = {"at": _now(), "items": count}

    # -- tracking dependencies ----------------------------------------------- #

    def track(self, document_id: str, kb_file: str, cited_version: str | None) -> dict[str, Any]:
        entries = self.data["tracked"].setdefault(document_id, [])
        for e in entries:
            if e["kb_file"] == kb_file:
                e["cited_version"] = cited_version
                e["updated"] = _now()
                return e
        entry = {"kb_file": kb_file, "cited_version": cited_version, "registered": _now()}
        entries.append(entry)
        return entry

    def untrack(self, document_id: str, kb_file: str | None = None) -> int:
        entries = self.data["tracked"].get(document_id)
        if not entries:
            return 0
        if kb_file is None:
            del self.data["tracked"][document_id]
            return len(entries)
        keep = [e for e in entries if e["kb_file"] != kb_file]
        removed = len(entries) - len(keep)
        if keep:
            self.data["tracked"][document_id] = keep
        else:
            del self.data["tracked"][document_id]
        return removed

    def known(self, document_id: str) -> dict[str, Any] | None:
        return self.data["documents"].get(document_id)

    def stale(self) -> list[dict[str, Any]]:
        """Knowledge-base entries citing a version we have observed to be superseded.

        Only entries that pin a version can appear here. One that cites a
        document without naming a version is reported as a gap by the scan, not
        guessed at as stale.
        """
        out: list[dict[str, Any]] = []
        for doc_id, entries in self.data["tracked"].items():
            doc = self.known(doc_id) or {}
            latest = doc.get("latest_version")
            for e in entries:
                if is_newer(latest, e.get("cited_version")):
                    out.append(
                        {
                            "document_id": doc_id,
                            "title": doc.get("title"),
                            "kb_file": e["kb_file"],
                            "kb_cites_version": e.get("cited_version"),
                            "current_version": latest,
                            "published": doc.get("latest_published"),
                            "url": doc.get("url"),
                        }
                    )
        out.sort(key=lambda r: (r.get("published") or "", r["document_id"]), reverse=True)
        return out
