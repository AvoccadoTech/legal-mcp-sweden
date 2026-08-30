"""Models for the Lifos watcher.

Tool inputs are pydantic models because FastMCP derives each tool's JSON
schema from the signature — these models *are* the published contract.

The parsed feed item is a plain model rather than a mirror of the RSS: Lifos
publishes a title, a link and a date, and everything that makes this connector
useful — the document id and its version — is extracted from the title text.
That extraction is the fragile part, so it lives here with its own tests.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# "RS/001/2024" — rättsligt ställningstagande. "RK/005/2026" — rättslig
# kommentar. Migrationsverket has used two- and three-digit sequences over the
# years, so the pattern stays loose on that field and strict on the year.
DOC_ID_RE = re.compile(r"\b(RS|RK)/(\d{2,4})/(\d{4})\b")

# "(version 3.0)" in a feed title, "version 2.0" in a knowledge-base sentence.
VERSION_RE = re.compile(r"version\s+(\d+(?:\.\d+)*)", re.IGNORECASE)


class Feed(str, Enum):
    """Which Lifos feed to read.

    LEGAL carries the rättsliga ställningstaganden and rättsliga kommentarer —
    the ones a knowledge base cites. COUNTRY carries landinformation, which is
    country-of-origin reporting rather than legal position.
    """

    LEGAL = "legal"
    COUNTRY = "country"
    ALL = "all"


class DocumentKind(str, Enum):
    STANDPOINT = "rättsligt ställningstagande"
    COMMENT = "rättslig kommentar"
    COUNTRY_INFO = "landinformation"
    OTHER = "other"


class FeedItem(BaseModel):
    """One <item> from a Lifos feed, after identity extraction."""

    model_config = ConfigDict(extra="ignore")

    feed: Feed
    title: str
    url: str = ""
    published: str | None = None
    document_id: str | None = None
    kind: DocumentKind = DocumentKind.OTHER
    version: str | None = None

    @classmethod
    def from_title(cls, feed: Feed, title: str, url: str, published: str | None) -> "FeedItem":
        doc_id = None
        if m := DOC_ID_RE.search(title):
            doc_id = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"

        low = title.lower()
        if low.startswith("rättsligt ställningstagande") or (doc_id or "").startswith("RS/"):
            kind = DocumentKind.STANDPOINT
        elif low.startswith("rättslig kommentar") or (doc_id or "").startswith("RK/"):
            kind = DocumentKind.COMMENT
        elif low.startswith("landinformation"):
            kind = DocumentKind.COUNTRY_INFO
        else:
            kind = DocumentKind.OTHER

        vm = VERSION_RE.search(title)
        return cls(
            feed=feed,
            title=title,
            url=url,
            published=published,
            document_id=doc_id,
            kind=kind,
            version=vm.group(1) if vm else None,
        )

    @property
    def key(self) -> str:
        """Ledger key. Items with no document id are still worth remembering,
        so they are keyed by URL rather than dropped."""
        return self.document_id or f"untitled:{self.url or self.title}"


class CheckUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feed: Feed = Field(
        default=Feed.LEGAL,
        description=(
            "'legal' for rättsliga ställningstaganden and kommentarer, "
            "'country' for landinformation, 'all' for both."
        ),
    )
    mark_seen: bool = Field(
        default=True,
        description=(
            "Persist what was read, so the same items are not reported as new "
            "next time. Set false for a dry run that leaves the ledger untouched."
        ),
    )


class ScanKnowledgeBase(BaseModel):
    # `register` is the tool's parameter name, but pydantic's metaclass inherits
    # ABCMeta.register, so a field of that name shadows it. Aliased rather than
    # renamed: the published tool signature should read the way it reads.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kb_path: str | None = Field(
        default=None,
        description=(
            "Root of the knowledge base to scan. Defaults to $LIFOS_KB_PATH. "
            "Markdown, text and YAML files are read; nothing is written to them."
        ),
    )
    register_findings: bool = Field(
        default=True,
        alias="register",
        description="Record what was found as tracked dependencies. False reports only.",
    )


class TrackDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(description="Document identifier, e.g. 'RS/001/2024'.")
    kb_file: str = Field(description="Path of the knowledge-base entry that depends on it.")
    cited_version: str | None = Field(
        default=None,
        description=(
            "The version that entry was written against, e.g. '2.0'. Without it "
            "the entry can never be reported stale, because there is nothing to "
            "compare a new version to."
        ),
    )


class UntrackDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(description="Document identifier, e.g. 'RS/001/2024'.")
    kb_file: str | None = Field(
        default=None,
        description="Stop watching for this one entry. Omit to stop watching the document entirely.",
    )


def version_tuple(v: str | None) -> tuple[int, ...]:
    if not v:
        return ()
    parts: list[int] = []
    for chunk in v.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            break
    return tuple(parts)


def is_newer(candidate: str | None, baseline: str | None) -> bool:
    """True when `candidate` is a strictly higher version than `baseline`.

    False whenever either side is missing or unparseable. An unknown version is
    never reported as a change: a false "your knowledge base is stale" costs a
    lawyer more than a missed one costs us, and the missed one is visible at the
    next review while the false one erodes trust in every future alert.
    """
    a, b = version_tuple(candidate), version_tuple(baseline)
    return bool(a and b and a > b)
