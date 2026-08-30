"""Finding Lifos references inside a firm's own knowledge base.

This is what turns a feed reader into a notary. Without it the watcher can only
say "Migrationsverket published something"; with it, the watcher can say "this
file of yours is now out of date, and here is the line."

The scan reads. It never writes to the knowledge base — proposing a change to a
firm's stated legal position is a lawyer's job, and the tool's job is to say
that the question has been reopened.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from .models import DOC_ID_RE, VERSION_RE

KB_SUFFIXES = {".md", ".markdown", ".txt", ".yaml", ".yml"}

# How far either side of a reference to look for the version it pins. Wide
# enough to span "RS/001/2024 (version 2.0)" written either way round and a
# sentence between them; narrow enough not to borrow a version from the next
# paragraph, which would be worse than finding none.
VERSION_WINDOW = 160

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".lifos"}


def _files(root: Path) -> Iterable[Path]:
    if root.is_file():
        return [root]
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in KB_SUFFIXES:
            continue
        if SKIP_DIRS & set(p.parts):
            continue
        out.append(p)
    return sorted(out)


def scan(kb_path: str | os.PathLike[str], window: int = VERSION_WINDOW) -> list[dict[str, Any]]:
    """Return one row per (file, document) citation found.

    Each row carries the version the citation pins, or None when the knowledge
    base refers to the document without naming a version. Those are reported
    rather than inferred: an unpinned citation is a gap for someone to close,
    and treating it as "probably the current one" would make every future
    staleness check quietly meaningless.
    """
    root = Path(kb_path)
    if not root.exists():
        raise FileNotFoundError(
            f"Knowledge base path not found: {root}. Pass kb_path explicitly or set LIFOS_KB_PATH."
        )

    found: dict[tuple[str, str], dict[str, Any]] = {}
    for f in _files(root):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in DOC_ID_RE.finditer(text):
            doc_id = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
            lo, hi = max(0, m.start() - window), min(len(text), m.end() + window)
            vm = VERSION_RE.search(text, lo, hi)
            row = {
                "document_id": doc_id,
                "kb_file": str(f),
                "line": text.count("\n", 0, m.start()) + 1,
                "cited_version": vm.group(1) if vm else None,
            }
            key = (row["kb_file"], doc_id)
            prior = found.get(key)
            # One file may cite the same document more than once. Keep the
            # citation that pins a version over one that does not.
            if prior is None or (prior["cited_version"] is None and row["cited_version"]):
                found[key] = row

    return sorted(found.values(), key=lambda r: (r["document_id"], r["kb_file"]))
