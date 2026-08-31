"""Models for the case-law monitor.

Two measurements shaped everything here, both taken on 2026-08-30 against the
live API. They are worth stating because they contradict the obvious design.

The corpus is two datasets in one endpoint. Recent publications (raw decisions,
since March 2025) carry a subject tag and a PDF but usually no statutory
reference: `rattsomradeLista` 78%, `lagrumLista` 31%. The older archive
(curated referat, back to 1981) is the reverse: `lagrumLista` 87%,
`nyckelordLista` 88%, `rattsomradeLista` 7%.

So a watch keyed on statute would miss most of what arrives. Worse, the misses
are not random — MÖD reasons topically. Decisions about buller, riktvärden,
täktverksamhet and strandskydd routinely name no section at all. Only
`sammanfattning` is populated for every record, which is why free text over the
summary is the primary key here and `lagrum` is a precision filter.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# "4 kap. 2 § PBL", "7 kap. 15 § miljöbalken", "56 a § lagen (1960:729)".
#
# The statute must stay attached to the section. Matching "4 kap. 2 §" and
# "PBL" as two fragments looks fine in a list and then fails every filter
# anyone would actually type.
_STATUTE = r"(?:[A-ZÅÄÖ]{2,6}\b|[a-zåäöA-ZÅÄÖ-]*(?:balken|lagen|förordningen)\b)"
LAGRUM_RE = re.compile(
    rf"\b\d+\s*kap\.?\s*\d+\s*[a-zåäö]?\s*§(?:\s+{_STATUTE})?"  # chapter + section (+ statute)
    rf"|\b\d+\s*[a-zåäö]?\s*§\s+{_STATUTE}"                     # bare section + statute
    r"|\b(?:PBL|PBF|MB|JB|ABL|LOU|LUF|ExL|RB|BrB|ÄB|UB)\b"      # abbreviation standing alone
)

SFS_RE = re.compile(r"\((\d{4}:\d{1,4})\)")


class Significance(str, Enum):
    """The API's `typ`, ordered by how much attention a decision deserves.

    PROVNINGSTILLSTAND is the one worth explaining: leave to appeal has been
    granted, so the question is on its way to a higher court. It is a forward
    signal rather than a settled answer, and for anyone with a project exposed
    to the point it is the most actionable value in the feed.
    """

    PREJUDIKAT = "PREJUDIKAT"
    FORHANDSAVGORANDE = "FORHANDSAVGORANDE"
    PROVNINGSTILLSTAND = "PROVNINGSTILLSTAND"
    VAGLEDANDE_MEN_EJ_PREJUDICERANDE = "VAGLEDANDE_MEN_EJ_PREJUDICERANDE"
    EJ_VAGLEDANDE = "EJ_VAGLEDANDE"
    UNKNOWN = "UNKNOWN"


# Lower sorts first. Used for ranking, never for hiding: a routine MÖD decision
# on a point a client's project turns on outranks a prejudikat about something
# else, so this only breaks ties between equally relevant hits.
SIGNIFICANCE_RANK = {
    Significance.PREJUDIKAT: 0,
    Significance.FORHANDSAVGORANDE: 1,
    Significance.PROVNINGSTILLSTAND: 2,
    Significance.VAGLEDANDE_MEN_EJ_PREJUDICERANDE: 3,
    Significance.EJ_VAGLEDANDE: 4,
    Significance.UNKNOWN: 5,
}

SIGNIFICANCE_LABEL = {
    Significance.PREJUDIKAT: "prejudikat",
    Significance.FORHANDSAVGORANDE: "förhandsavgörande",
    Significance.PROVNINGSTILLSTAND: "prövningstillstånd — on its way up",
    Significance.VAGLEDANDE_MEN_EJ_PREJUDICERANDE: "vägledande",
    Significance.EJ_VAGLEDANDE: "ej vägledande",
    Significance.UNKNOWN: "—",
}


def extract_lagrum(record: dict[str, Any]) -> list[str]:
    """Statutory references for one decision, structured field first.

    Falls back to scanning the summary, which recovers roughly a quarter of the
    records that carry no `lagrumLista`. It does not recover the rest, and
    nothing here pretends otherwise — see the module docstring.
    """
    out: list[str] = []
    for entry in record.get("lagrumLista") or []:
        ref = (entry.get("referens") or "").strip()
        if ref:
            out.append(re.sub(r"\s+", " ", ref))
    if out:
        return out
    for m in LAGRUM_RE.finditer(record.get("sammanfattning") or ""):
        ref = re.sub(r"\s+", " ", m.group(0)).strip()
        if ref not in out:
            out.append(ref)
    return out


class Decision(BaseModel):
    """One publication, flattened for storage."""

    model_config = ConfigDict(extra="ignore")

    id: str
    decided: str | None = None
    published: str | None = None
    court_code: str = ""
    court_name: str = ""
    case_numbers: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    lagrum: list[str] = Field(default_factory=list)
    sfs: list[str] = Field(default_factory=list)
    summary: str = ""
    significance: Significance = Significance.UNKNOWN
    form: str = ""
    referat: list[str] = Field(default_factory=list)
    attachments: int = 0

    @classmethod
    def from_api(cls, r: dict[str, Any]) -> "Decision":
        try:
            sig = Significance(r.get("typ") or "")
        except ValueError:
            sig = Significance.UNKNOWN
        lagrum = extract_lagrum(r)
        sfs = sorted(
            {e["sfsNummer"] for e in (r.get("lagrumLista") or []) if e.get("sfsNummer")}
            | set(SFS_RE.findall(" ".join(lagrum)))
        )
        return cls(
            id=r["id"],
            decided=r.get("avgorandedatum"),
            published=(r.get("publiceringstid") or "")[:10] or None,
            court_code=(r.get("domstol") or {}).get("domstolKod") or "",
            court_name=(r.get("domstol") or {}).get("domstolNamn") or "",
            case_numbers=r.get("malNummerLista") or [],
            subjects=r.get("rattsomradeLista") or [],
            keywords=r.get("nyckelordLista") or [],
            lagrum=lagrum,
            sfs=sfs,
            summary=re.sub(r"\s+", " ", r.get("sammanfattning") or "").strip(),
            significance=sig,
            form=r.get("publiceringsform") or "",
            referat=r.get("referatNummerLista") or [],
            attachments=len(r.get("bilagaLista") or []),
        )

    @property
    def case_label(self) -> str:
        return ", ".join(self.case_numbers) or "—"


# --------------------------------------------------------------------------- #
# Tool inputs
# --------------------------------------------------------------------------- #


class SyncMode(str, Enum):
    DELTA = "delta"
    FULL = "full"


class Sync(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: SyncMode = Field(
        default=SyncMode.DELTA,
        description=(
            "'delta' reads newest-first and stops once it reaches decisions "
            "already stored — seconds, and the right choice for a daily run. "
            "'full' walks the entire corpus, roughly 1,700 requests, needed "
            "once to build the mirror."
        ),
    )
    max_pages: int | None = Field(
        default=None,
        ge=1,
        description="Stop after this many pages. Useful for a first look; omit for a real sync.",
    )


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(
        default=None,
        description=(
            "Free text over the decision summary — the only field populated for "
            "every record. Supports prefix search with * (e.g. 'strandskydd*') "
            "and AND/OR/NOT."
        ),
    )
    court: str | None = Field(
        default=None, description="Court code, e.g. 'MMOD' for Mark- och miljööverdomstolen."
    )
    subject: str | None = Field(
        default=None,
        description="Rättsområde, e.g. 'Miljömål' or 'Mål enligt plan- och bygglagen'.",
    )
    lagrum: str | None = Field(
        default=None,
        description=(
            "Statutory reference substring, e.g. '4 kap. 2 § PBL' or '1999:1229'. "
            "Present for only about half of recent decisions — narrows precisely "
            "when it matches, and silently excludes much of the corpus when it does not."
        ),
    )
    decided_from: str | None = Field(default=None, description="Earliest decision date, YYYY-MM-DD.")
    decided_to: str | None = Field(default=None, description="Latest decision date, YYYY-MM-DD.")
    limit: int = Field(default=20, ge=1, le=100)


class AddWatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Short label, e.g. 'Solpark Hedemora — jordbruksmark'.")
    text: str | None = Field(
        default=None, description="Free-text terms to match in the summary."
    )
    court: str | None = Field(default=None, description="Restrict to one court code.")
    subject: str | None = Field(default=None, description="Restrict to one rättsområde.")
    lagrum: str | None = Field(default=None, description="Restrict to a statutory reference.")
    matter: str | None = Field(
        default=None, description="The firm's own matter or client reference, for routing the alert."
    )
