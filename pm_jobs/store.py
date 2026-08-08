"""Immutable raw store for scrape output.

Nothing here interprets a job. The contract is: whatever the boards returned,
in full, with enough provenance that stage 2 can re-normalize and stage 3 can
re-rank without ever re-scraping.

Four tables, each answering a different question:

  scrape_runs    when did we scrape, and did the whole run finish
  scrape_tasks   how did each (search, term, board) leg go — including failures
  raw_jobs       what did a posting say (one row per distinct *version* of it)
  job_sightings  when did we see a posting (one row per observation)

raw_jobs and job_sightings are split on purpose. A posting seen on five days
with unchanged text is one raw_jobs row and five sightings; if the salary
appears on day three, that is a second raw_jobs row. Stage 2 gets both "what
does it say now" and "how long has it been up" without guessing.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DB_PATH = Path("pm_jobs.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS scrape_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    config_path  TEXT NOT NULL,
    searches     TEXT NOT NULL,          -- JSON list of search names in this run
    status       TEXT NOT NULL DEFAULT 'running'  -- running | ok | partial | failed
);

CREATE TABLE IF NOT EXISTS scrape_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES scrape_runs(id),
    search_name   TEXT NOT NULL,
    term          TEXT NOT NULL,
    board         TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    rows_returned INTEGER NOT NULL DEFAULT 0,
    rows_new      INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_run ON scrape_tasks(run_id);

CREATE TABLE IF NOT EXISTS raw_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    board         TEXT NOT NULL,
    board_job_id  TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    payload       TEXT NOT NULL,         -- full jobspy row as JSON
    job_url       TEXT,
    title         TEXT,
    company       TEXT,
    location      TEXT,
    first_seen_at TEXT NOT NULL,
    UNIQUE (board, board_job_id, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_raw_board_job ON raw_jobs(board, board_job_id);

CREATE TABLE IF NOT EXISTS job_sightings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_job_id   INTEGER NOT NULL REFERENCES raw_jobs(id),
    task_id      INTEGER NOT NULL REFERENCES scrape_tasks(id),
    run_id       INTEGER NOT NULL REFERENCES scrape_runs(id),
    search_name  TEXT NOT NULL,
    term         TEXT NOT NULL,
    board        TEXT NOT NULL,
    board_job_id TEXT NOT NULL,
    seen_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sightings_job ON job_sightings(board, board_job_id);
CREATE INDEX IF NOT EXISTS idx_sightings_run ON job_sightings(run_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    """Make one pandas cell safe for json.dumps.

    pandas hands back NaN, NaT, numpy scalars and Timestamps; SQLite and JSON
    want None, str, int, float, bool.
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value
    # numpy scalars and pandas NA
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _jsonable(item())
        except (ValueError, TypeError):
            pass
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    text = str(value)
    return None if text in ("nan", "NaT", "None", "<NA>") else text


def row_to_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _jsonable(v) for k, v in row.items()}


def payload_hash(payload: dict[str, Any]) -> str:
    """Stable fingerprint of a posting's content.

    Volatile fields are excluded so a re-scrape of an unchanged posting hashes
    the same and does not create a spurious new version.
    """
    volatile = {"id", "company_logo", "emails"}
    stable = {k: v for k, v in sorted(payload.items()) if k not in volatile}
    blob = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


class RawStore:
    """Thin write/read layer over the raw tables."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- runs -------------------------------------------------------------

    def start_run(self, config_path: str, search_names: Iterable[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO scrape_runs (started_at, config_path, searches) VALUES (?, ?, ?)",
            (utcnow(), config_path, json.dumps(list(search_names))),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE scrape_runs SET finished_at = ?, status = ? WHERE id = ?",
            (utcnow(), status, run_id),
        )
        self.conn.commit()

    # --- tasks ------------------------------------------------------------

    def start_task(self, run_id: int, search_name: str, term: str, board: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO scrape_tasks (run_id, search_name, term, board, started_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, search_name, term, board, utcnow()),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_task(
        self,
        task_id: int,
        status: str,
        rows_returned: int = 0,
        rows_new: int = 0,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE scrape_tasks
               SET finished_at = ?, status = ?, rows_returned = ?, rows_new = ?, error = ?
               WHERE id = ?""",
            (utcnow(), status, rows_returned, rows_new, error, task_id),
        )
        self.conn.commit()

    # --- jobs -------------------------------------------------------------

    def record_job(self, run_id: int, task_id: int, search_name: str, term: str, board: str, payload: dict) -> bool:
        """Store one scraped posting. Returns True if this is a new content version."""
        board_job_id = str(payload.get("id") or payload.get("job_url") or "")
        if not board_job_id:
            return False

        digest = payload_hash(payload)
        seen_at = utcnow()

        cur = self.conn.execute(
            """INSERT OR IGNORE INTO raw_jobs
               (board, board_job_id, payload_hash, payload, job_url, title, company, location, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                board,
                board_job_id,
                digest,
                json.dumps(payload, ensure_ascii=False),
                payload.get("job_url"),
                payload.get("title"),
                payload.get("company"),
                payload.get("location"),
                seen_at,
            ),
        )
        is_new = cur.rowcount > 0

        raw_job_id = self.conn.execute(
            "SELECT id FROM raw_jobs WHERE board = ? AND board_job_id = ? AND payload_hash = ?",
            (board, board_job_id, digest),
        ).fetchone()["id"]

        self.conn.execute(
            """INSERT INTO job_sightings
               (raw_job_id, task_id, run_id, search_name, term, board, board_job_id, seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (raw_job_id, task_id, run_id, search_name, term, board, board_job_id, seen_at),
        )
        return is_new

    # --- reads ------------------------------------------------------------

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT r.*,
                      (SELECT COUNT(*) FROM job_sightings s WHERE s.run_id = r.id) AS sightings,
                      (SELECT COUNT(*) FROM scrape_tasks t WHERE t.run_id = r.id AND t.status = 'error') AS errors
               FROM scrape_runs r ORDER BY r.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def run_tasks(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM scrape_tasks WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()

    def stats(self) -> dict[str, Any]:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "runs": one("SELECT COUNT(*) FROM scrape_runs"),
            "raw_versions": one("SELECT COUNT(*) FROM raw_jobs"),
            "distinct_postings": one("SELECT COUNT(*) FROM (SELECT DISTINCT board, board_job_id FROM raw_jobs)"),
            "sightings": one("SELECT COUNT(*) FROM job_sightings"),
            "by_board": self.conn.execute(
                """SELECT board, COUNT(DISTINCT board_job_id) AS postings
                   FROM raw_jobs GROUP BY board ORDER BY postings DESC"""
            ).fetchall(),
        }
