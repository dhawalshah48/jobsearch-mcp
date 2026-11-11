"""
Pipeline state. SQLite because it is one file, needs no daemon, and survives
restarts of a free-tier host that sleeps.

Two jobs: (1) dedup so a scheduled run only surfaces genuinely new postings,
(2) track where each application actually stands.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

STAGES = ("new", "shortlisted", "tailored", "applied", "outreach_sent",
          "responded", "interviewing", "rejected", "closed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    source      TEXT, company TEXT, title TEXT, location TEXT,
    remote      INTEGER, department TEXT, employment_type TEXT, salary TEXT,
    posted_at   TEXT, apply_url TEXT, description TEXT,
    first_seen  TEXT NOT NULL, last_seen TEXT NOT NULL,
    score       REAL, score_detail TEXT,
    stage       TEXT NOT NULL DEFAULT 'new',
    notes       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_stage ON jobs(stage);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

CREATE TABLE IF NOT EXISTS outreach (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT, company TEXT,
    contact_name TEXT, contact_role TEXT, contact_email TEXT,
    email_status TEXT,          -- verified | unverified | company_alias
    channel     TEXT,           -- email | application_note | referral
    drafted_at  TEXT, sent_at TEXT, replied_at TEXT,
    subject     TEXT, body TEXT,
    UNIQUE(job_id, contact_email)
);

CREATE TABLE IF NOT EXISTS boards (
    target      TEXT PRIMARY KEY,   -- 'greenhouse:stripe'
    label       TEXT DEFAULT '',
    added_at    TEXT,
    last_run    TEXT,
    last_error  TEXT
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------------------------------------------------------------- jobs --
    def upsert_jobs(self, jobs: Iterable[dict[str, Any]]) -> dict[str, int]:
        now = datetime.now(timezone.utc).isoformat()
        new = updated = 0
        with self._conn() as c:
            for j in jobs:
                exists = c.execute(
                    "SELECT 1 FROM jobs WHERE job_id=?", (j["job_id"],)
                ).fetchone()
                if exists:
                    c.execute("UPDATE jobs SET last_seen=? WHERE job_id=?",
                              (now, j["job_id"]))
                    updated += 1
                    continue
                c.execute("""
                    INSERT INTO jobs (job_id, source, company, title, location,
                        remote, department, employment_type, salary, posted_at,
                        apply_url, description, first_seen, last_seen)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    j["job_id"], j.get("source"), j.get("company"), j.get("title"),
                    j.get("location"), int(bool(j.get("remote"))), j.get("department"),
                    j.get("employment_type"), j.get("salary"), j.get("posted_at"),
                    j.get("apply_url"), j.get("description"), now, now,
                ))
                new += 1
        return {"new": new, "already_known": updated}

    def set_score(self, job_id: str, score: float, detail: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute("UPDATE jobs SET score=?, score_detail=? WHERE job_id=?",
                      (score, json.dumps(detail), job_id))

    def set_stage(self, job_id: str, stage: str, note: str = "") -> bool:
        if stage not in STAGES:
            raise ValueError(f"stage must be one of {STAGES}")
        with self._conn() as c:
            cur = c.execute(
                "UPDATE jobs SET stage=?, notes=COALESCE(notes,'')||? WHERE job_id=?",
                (stage, f"\n[{datetime.now(timezone.utc):%Y-%m-%d}] {note}" if note else "",
                 job_id))
            return cur.rowcount > 0

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def query_jobs(self, *, min_score: float | None = None, stage: str | None = None,
                   company: str | None = None, unscored: bool = False,
                   new_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        args: list[Any] = []
        if min_score is not None:
            sql += " AND score >= ?"; args.append(min_score)
        if stage:
            sql += " AND stage = ?"; args.append(stage)
        if company:
            sql += " AND company LIKE ?"; args.append(f"%{company}%")
        if unscored:
            sql += " AND score IS NULL"
        if new_only:
            sql += " AND first_seen = last_seen"
        sql += " ORDER BY score DESC NULLS LAST, first_seen DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    # ------------------------------------------------------------ outreach --
    def record_draft(self, **kw) -> int:
        with self._conn() as c:
            cur = c.execute("""
                INSERT OR REPLACE INTO outreach
                    (job_id, company, contact_name, contact_role, contact_email,
                     email_status, channel, drafted_at, subject, body)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                kw.get("job_id"), kw.get("company"), kw.get("contact_name"),
                kw.get("contact_role"), kw.get("contact_email"),
                kw.get("email_status"), kw.get("channel", "email"),
                datetime.now(timezone.utc).isoformat(),
                kw.get("subject"), kw.get("body"),
            ))
            return cur.lastrowid

    def mark_outreach(self, outreach_id: int, *, sent: bool = False,
                      replied: bool = False) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        field = "replied_at" if replied else "sent_at" if sent else None
        if not field:
            return False
        with self._conn() as c:
            cur = c.execute(f"UPDATE outreach SET {field}=? WHERE id=?", (now, outreach_id))
            return cur.rowcount > 0

    def outreach_log(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM outreach ORDER BY drafted_at DESC LIMIT ?", (limit,))]

    # -------------------------------------------------------------- boards --
    def add_boards(self, targets: Iterable[str], label: str = "") -> int:
        now = datetime.now(timezone.utc).isoformat()
        n = 0
        with self._conn() as c:
            for t in targets:
                cur = c.execute(
                    "INSERT OR IGNORE INTO boards (target, label, added_at) VALUES (?,?,?)",
                    (t.strip(), label, now))
                n += cur.rowcount
        return n

    def list_boards(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM boards ORDER BY target")]

    def mark_board_run(self, target: str, error: str | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE boards SET last_run=?, last_error=? WHERE target=?",
                      (datetime.now(timezone.utc).isoformat(), error, target))

    def stats(self) -> dict[str, Any]:
        with self._conn() as c:
            by_stage = {r["stage"]: r["n"] for r in c.execute(
                "SELECT stage, COUNT(*) n FROM jobs GROUP BY stage")}
            total = c.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
            scored = c.execute(
                "SELECT COUNT(*) n FROM jobs WHERE score IS NOT NULL").fetchone()["n"]
            sent = c.execute(
                "SELECT COUNT(*) n FROM outreach WHERE sent_at IS NOT NULL").fetchone()["n"]
            replied = c.execute(
                "SELECT COUNT(*) n FROM outreach WHERE replied_at IS NOT NULL").fetchone()["n"]
        return {"total_jobs": total, "scored": scored, "by_stage": by_stage,
                "outreach_sent": sent, "outreach_replied": replied,
                "reply_rate": round(replied / sent, 3) if sent else None}
