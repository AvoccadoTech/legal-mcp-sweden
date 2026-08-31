"""Local mirror of the case-law corpus: SQLite with an FTS5 index.

SQLite because it is in the standard library, needs no server, and produces a
single file the firm can see, back up and delete. FTS5 because the only field
populated on every record is the summary, so free text is the primary key here
rather than a convenience.

The whole corpus is roughly 17,300 records — a few tens of megabytes. This is
a mirror, not a cache: it is meant to be complete, kept current by a daily
delta, and to live wherever the firm's own files live.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Decision, Significance

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            TEXT PRIMARY KEY,
    decided       TEXT,
    published     TEXT,
    court_code    TEXT,
    court_name    TEXT,
    case_numbers  TEXT,
    subjects      TEXT,
    keywords      TEXT,
    lagrum        TEXT,
    sfs           TEXT,
    summary       TEXT,
    significance  TEXT,
    form          TEXT,
    referat       TEXT,
    attachments   INTEGER,
    first_seen    TEXT
);
CREATE INDEX IF NOT EXISTS idx_decided ON decisions(decided);
CREATE INDEX IF NOT EXISTS idx_court   ON decisions(court_code);
CREATE INDEX IF NOT EXISTS idx_seen    ON decisions(first_seen);

-- Contentless-external FTS over the fields worth searching. Kept in step with
-- `decisions` by triggers so a partial sync cannot leave the index skewed.
CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
    summary, subjects, keywords, lagrum, case_numbers,
    content='decisions', content_rowid='rowid',
    tokenize="unicode61 remove_diacritics 0"
);
CREATE TRIGGER IF NOT EXISTS decisions_ai AFTER INSERT ON decisions BEGIN
    INSERT INTO decisions_fts(rowid, summary, subjects, keywords, lagrum, case_numbers)
    VALUES (new.rowid, new.summary, new.subjects, new.keywords, new.lagrum, new.case_numbers);
END;
CREATE TRIGGER IF NOT EXISTS decisions_ad AFTER DELETE ON decisions BEGIN
    INSERT INTO decisions_fts(decisions_fts, rowid, summary, subjects, keywords, lagrum, case_numbers)
    VALUES ('delete', old.rowid, old.summary, old.subjects, old.keywords, old.lagrum, old.case_numbers);
END;
CREATE TRIGGER IF NOT EXISTS decisions_au AFTER UPDATE ON decisions BEGIN
    INSERT INTO decisions_fts(decisions_fts, rowid, summary, subjects, keywords, lagrum, case_numbers)
    VALUES ('delete', old.rowid, old.summary, old.subjects, old.keywords, old.lagrum, old.case_numbers);
    INSERT INTO decisions_fts(rowid, summary, subjects, keywords, lagrum, case_numbers)
    VALUES (new.rowid, new.summary, new.subjects, new.keywords, new.lagrum, new.case_numbers);
END;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS watches (
    name    TEXT PRIMARY KEY,
    text    TEXT,
    court   TEXT,
    subject TEXT,
    lagrum  TEXT,
    matter  TEXT,
    created TEXT
);
-- Which decisions a watch has already reported, so an alert fires once.
CREATE TABLE IF NOT EXISTS watch_hits (
    name        TEXT,
    decision_id TEXT,
    reported    TEXT,
    PRIMARY KEY (name, decision_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- meta ---------------------------------------------------------------- #

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- writing ------------------------------------------------------------- #

    def has(self, decision_id: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM decisions WHERE id = ?", (decision_id,)).fetchone()
            is not None
        )

    def upsert(self, decisions: Iterable[Decision]) -> tuple[int, int]:
        """Insert or refresh. Returns (new, updated).

        Republication happens — a decision can gain a subject tag or a summary
        edit — so an existing row is refreshed rather than skipped. `first_seen`
        is preserved, because that is what "new to this firm" means and it must
        not move when a record is merely touched.
        """
        new = updated = 0
        for d in decisions:
            existing = self.has(d.id)
            payload = (
                d.decided, d.published, d.court_code, d.court_name,
                json.dumps(d.case_numbers, ensure_ascii=False),
                json.dumps(d.subjects, ensure_ascii=False),
                json.dumps(d.keywords, ensure_ascii=False),
                json.dumps(d.lagrum, ensure_ascii=False),
                json.dumps(d.sfs, ensure_ascii=False),
                d.summary, d.significance.value, d.form,
                json.dumps(d.referat, ensure_ascii=False),
                d.attachments,
            )
            if existing:
                self.db.execute(
                    "UPDATE decisions SET decided=?, published=?, court_code=?, court_name=?, "
                    "case_numbers=?, subjects=?, keywords=?, lagrum=?, sfs=?, summary=?, "
                    "significance=?, form=?, referat=?, attachments=? WHERE id=?",
                    (*payload, d.id),
                )
                updated += 1
            else:
                self.db.execute(
                    "INSERT INTO decisions(decided, published, court_code, court_name, "
                    "case_numbers, subjects, keywords, lagrum, sfs, summary, significance, "
                    "form, referat, attachments, id, first_seen) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*payload, d.id, _now()),
                )
                new += 1
        self.db.commit()
        return new, updated

    # -- reading ------------------------------------------------------------- #

    @staticmethod
    def _row_to_decision(row: sqlite3.Row) -> Decision:
        return Decision(
            id=row["id"],
            decided=row["decided"],
            published=row["published"],
            court_code=row["court_code"],
            court_name=row["court_name"],
            case_numbers=json.loads(row["case_numbers"] or "[]"),
            subjects=json.loads(row["subjects"] or "[]"),
            keywords=json.loads(row["keywords"] or "[]"),
            lagrum=json.loads(row["lagrum"] or "[]"),
            sfs=json.loads(row["sfs"] or "[]"),
            summary=row["summary"] or "",
            significance=Significance(row["significance"] or "UNKNOWN"),
            form=row["form"] or "",
            referat=json.loads(row["referat"] or "[]"),
            attachments=row["attachments"] or 0,
        )

    def get(self, decision_id: str) -> Decision | None:
        row = self.db.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        return self._row_to_decision(row) if row else None

    def search(
        self,
        text: str | None = None,
        court: str | None = None,
        subject: str | None = None,
        lagrum: str | None = None,
        decided_from: str | None = None,
        decided_to: str | None = None,
        since_first_seen: str | None = None,
        limit: int = 20,
    ) -> list[Decision]:
        where: list[str] = []
        params: list[Any] = []
        sql = "SELECT d.* FROM decisions d"

        if text:
            sql += " JOIN decisions_fts f ON f.rowid = d.rowid"
            where.append("decisions_fts MATCH ?")
            params.append(text)
        if court:
            where.append("d.court_code = ?")
            params.append(court.upper())
        if subject:
            where.append("d.subjects LIKE ?")
            params.append(f"%{subject}%")
        if lagrum:
            where.append("d.lagrum LIKE ?")
            params.append(f"%{lagrum}%")
        if decided_from:
            where.append("d.decided >= ?")
            params.append(decided_from)
        if decided_to:
            where.append("d.decided <= ?")
            params.append(decided_to)
        if since_first_seen:
            where.append("d.first_seen > ?")
            params.append(since_first_seen)

        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY d.decided DESC LIMIT ?"
        params.append(limit)

        try:
            rows = self.db.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if text:
                raise ValueError(
                    f"The search text is not valid FTS5 syntax ({exc}). Quote phrases as "
                    f'"like this", use * only at the end of a word, and escape stray quotes.'
                ) from exc
            raise
        return [self._row_to_decision(r) for r in rows]

    # -- coverage ------------------------------------------------------------ #

    def coverage(self) -> dict[str, Any]:
        """What the mirror actually holds.

        Exists so nobody mistakes this for a research database. It is ~17k
        records against JUNO's two million, and a lawyer is entitled to see the
        shape of what they are being shown before relying on it.
        """
        cur = self.db.execute
        total = cur("SELECT count(*) c FROM decisions").fetchone()["c"]
        if not total:
            return {"total": 0}
        span = cur("SELECT min(decided) a, max(decided) b FROM decisions").fetchone()
        courts = cur(
            "SELECT court_name n, count(*) c FROM decisions GROUP BY court_name "
            "ORDER BY c DESC LIMIT 10"
        ).fetchall()
        subjects = cur(
            "SELECT subjects s, count(*) c FROM decisions WHERE subjects != '[]' "
            "GROUP BY subjects ORDER BY c DESC LIMIT 10"
        ).fetchall()
        return {
            "total": total,
            "earliest": span["a"],
            "latest": span["b"],
            "courts": [(r["n"], r["c"]) for r in courts],
            "subjects": [(", ".join(json.loads(r["s"])), r["c"]) for r in subjects],
            "with_lagrum": cur(
                "SELECT count(*) c FROM decisions WHERE lagrum != '[]'"
            ).fetchone()["c"],
            "with_subject": cur(
                "SELECT count(*) c FROM decisions WHERE subjects != '[]'"
            ).fetchone()["c"],
            "last_sync": self.get_meta("last_sync"),
            "corpus_total_reported": self.get_meta("corpus_total"),
            "path": str(self.path),
        }

    # -- watches -------------------------------------------------------------- #

    def add_watch(self, **kw: Any) -> None:
        self.db.execute(
            "INSERT INTO watches(name, text, court, subject, lagrum, matter, created) "
            "VALUES(:name,:text,:court,:subject,:lagrum,:matter,:created) "
            "ON CONFLICT(name) DO UPDATE SET text=excluded.text, court=excluded.court, "
            "subject=excluded.subject, lagrum=excluded.lagrum, matter=excluded.matter",
            {**kw, "created": _now()},
        )
        self.db.commit()

    def remove_watch(self, name: str) -> int:
        n = self.db.execute("DELETE FROM watches WHERE name = ?", (name,)).rowcount
        self.db.execute("DELETE FROM watch_hits WHERE name = ?", (name,))
        self.db.commit()
        return n

    def watches(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM watches ORDER BY name")]

    def already_reported(self, name: str, decision_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM watch_hits WHERE name = ? AND decision_id = ?", (name, decision_id)
            ).fetchone()
            is not None
        )

    def mark_reported(self, name: str, decision_ids: Iterable[str]) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO watch_hits(name, decision_id, reported) VALUES(?,?,?)",
            [(name, d, _now()) for d in decision_ids],
        )
        self.db.commit()
