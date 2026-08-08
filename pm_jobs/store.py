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
from enum import Enum
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
    date_posted   TEXT,                  -- board-reported posting date, day resolution
    first_seen_at TEXT NOT NULL,
    UNIQUE (board, board_job_id, payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_raw_board_job ON raw_jobs(board, board_job_id);
-- idx_raw_date_posted is created in _migrate(), after the column is guaranteed
-- to exist: CREATE TABLE IF NOT EXISTS does not add columns to an older table,
-- so indexing here would fail on any database created before date_posted.

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

-- Yours. Permanent. Never regenerated.
--
-- Deliberately keyed on (board, board_job_id) rather than raw_jobs.id: raw_jobs
-- is versioned, so a re-listed posting writes a new row, and state stored
-- against a version would be orphaned the moment a board re-lists the job.
CREATE TABLE IF NOT EXISTS job_state (
    board         TEXT NOT NULL,
    board_job_id  TEXT NOT NULL,
    is_read       INTEGER NOT NULL DEFAULT 0,
    is_favorite   INTEGER NOT NULL DEFAULT 0,
    read_at       TEXT,
    favorited_at  TEXT,
    PRIMARY KEY (board, board_job_id)
);

CREATE TABLE IF NOT EXISTS review_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    model        TEXT NOT NULL,
    prefs_hash   TEXT NOT NULL,          -- which preferences produced these verdicts
    considered   INTEGER NOT NULL DEFAULT 0,
    prefiltered  INTEGER NOT NULL DEFAULT 0,
    judged       INTEGER NOT NULL DEFAULT 0,
    kept         INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'running'
);

-- The model's. Regenerable. One row per (posting version, review run).
--
-- Dropped postings are stored too, with the rule that dropped them. Without
-- that a too-aggressive filter is invisible — you would only ever see what
-- survived, never what it cost you.
CREATE TABLE IF NOT EXISTS job_reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES review_runs(id),
    board         TEXT NOT NULL,
    board_job_id  TEXT NOT NULL,
    raw_job_id    INTEGER NOT NULL REFERENCES raw_jobs(id),  -- exact version reviewed
    stage         TEXT NOT NULL,          -- prefilter | judged | error
    verdict       TEXT NOT NULL,          -- keep | drop
    drop_reason   TEXT,
    languages     TEXT NOT NULL DEFAULT '[]',  -- what the model read as required
    tags          TEXT NOT NULL DEFAULT '[]',
    summary       TEXT,
    model         TEXT,
    error         TEXT,
    reviewed_at   TEXT NOT NULL,
    UNIQUE (raw_job_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_reviews_job ON job_reviews(board, board_job_id);
CREATE INDEX IF NOT EXISTS idx_reviews_raw ON job_reviews(raw_job_id);

CREATE TABLE IF NOT EXISTS backfill_attempts (
    board        TEXT NOT NULL,
    board_job_id TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_try_at  TEXT NOT NULL,
    status       TEXT NOT NULL,          -- ok | error
    error        TEXT,
    PRIMARY KEY (board, board_job_id)
);
"""

# A posting that fails this many times is treated as gone (expired, pulled,
# region-blocked) and skipped, so a dead posting cannot be retried forever.
MAX_BACKFILL_ATTEMPTS = 3


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    """Make one scraped cell safe for json.dumps.

    pandas hands back NaN, NaT, numpy scalars and Timestamps; jobspy's per-job
    API hands back enum members and lists of them. SQLite and JSON want None,
    str, int, float, bool.
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Enum):
        # jobspy enums carry a tuple of localized spellings; the search path
        # stores the first ("fulltime"), so backfilled rows must match.
        inner = value.value
        return _jsonable(inner[0] if isinstance(inner, (list, tuple)) and inner else inner)
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
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


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema.

    The raw store is append-only and expensive to rebuild — it is the one thing
    that cannot be regenerated without re-scraping — so schema changes migrate
    in place rather than asking for a fresh scrape.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(raw_jobs)")}
    if "date_posted" not in columns:
        conn.execute("ALTER TABLE raw_jobs ADD COLUMN date_posted TEXT")
        # Promote the value already sitting in every stored payload.
        conn.execute("UPDATE raw_jobs SET date_posted = json_extract(payload, '$.date_posted')")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_date_posted ON raw_jobs(date_posted)")
    conn.commit()


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
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
               (board, board_job_id, payload_hash, payload, job_url, title, company, location,
                date_posted, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                board,
                board_job_id,
                digest,
                json.dumps(payload, ensure_ascii=False),
                payload.get("job_url"),
                payload.get("title"),
                payload.get("company"),
                payload.get("location"),
                payload.get("date_posted"),
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

    # --- backfill ---------------------------------------------------------

    def jobs_needing_details(
        self, board: str = "linkedin", limit: int | None = None, retry_failed: bool = False
    ) -> list[sqlite3.Row]:
        """Postings we have never obtained a description for.

        The test is "does *any* stored version have a description", not "does
        the newest one". A board re-listing a posting with any field changed
        writes a fresh search version, which never carries a description; asking
        only about the newest version would re-fetch every repeat posting the
        day after it was enriched.

        The row returned is still the newest version, so the fetched description
        merges into the freshest search data.
        """
        attempt_clause = "" if retry_failed else (
            " AND NOT EXISTS (SELECT 1 FROM backfill_attempts a"
            "                 WHERE a.board = r.board AND a.board_job_id = r.board_job_id"
            "                   AND a.status = 'error' AND a.attempts >= ?)"
        )
        params: list = [board]
        if not retry_failed:
            params.append(MAX_BACKFILL_ATTEMPTS)

        sql = f"""
            SELECT r.id, r.board, r.board_job_id, r.payload, r.title, r.company
            FROM raw_jobs r
            JOIN (SELECT board, board_job_id, MAX(id) AS newest
                  FROM raw_jobs GROUP BY board, board_job_id) latest
              ON latest.newest = r.id
            WHERE r.board = ?
              AND NOT EXISTS (
                    SELECT 1 FROM raw_jobs seen
                    WHERE seen.board = r.board
                      AND seen.board_job_id = r.board_job_id
                      AND COALESCE(json_extract(seen.payload, '$.description'), '') <> ''
              )
              {attempt_clause}
            ORDER BY r.id
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def add_version(self, board: str, board_job_id: str, payload: dict) -> bool:
        """Store an enriched version of a posting we already have.

        Deliberately writes no sighting: a sighting means a board's search
        returned this posting, and a backfill is not that.
        """
        digest = payload_hash(payload)
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO raw_jobs
               (board, board_job_id, payload_hash, payload, job_url, title, company, location,
                date_posted, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                board,
                board_job_id,
                digest,
                json.dumps(payload, ensure_ascii=False),
                payload.get("job_url"),
                payload.get("title"),
                payload.get("company"),
                payload.get("location"),
                payload.get("date_posted"),
                utcnow(),
            ),
        )
        return cur.rowcount > 0

    def record_attempt(self, board: str, board_job_id: str, status: str, error: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO backfill_attempts (board, board_job_id, attempts, last_try_at, status, error)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT (board, board_job_id) DO UPDATE SET
                   attempts    = attempts + 1,
                   last_try_at = excluded.last_try_at,
                   status      = excluded.status,
                   error       = excluded.error""",
            (board, board_job_id, utcnow(), status, error),
        )

    # --- reads ------------------------------------------------------------

    def last_successful_run_at(self) -> str | None:
        """When the most recent run that actually stored something finished.

        A failed run is not a watermark: if last night's scrape died, tonight's
        must still cover last night's postings. 'partial' counts — some legs
        succeeded, and the ones that failed are recorded per task.
        """
        row = self.conn.execute(
            """SELECT MAX(COALESCE(finished_at, started_at)) AS at
               FROM scrape_runs WHERE status IN ('ok', 'partial')"""
        ).fetchone()
        return row["at"] if row else None

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

    def freshness(self) -> dict[str, Any]:
        """How well we can tell a posting's age.

        Boards report `date_posted` inconsistently, so stage 3 needs a fallback:
        when it is missing, the earliest sighting is the best available proxy.
        That proxy has a catch worth surfacing — for postings already live when
        the scanner first ran, "first seen" is only a lower bound on age, so a
        three-week-old job can look brand new.
        """
        # Censoring is a question about *which run* first saw a posting, not about
        # clock times: a row's first_seen_at is always a moment after its run began.
        row = self.conn.execute(
            """WITH per_posting AS (
                   SELECT board, board_job_id, MAX(date_posted) AS date_posted
                   FROM raw_jobs GROUP BY board, board_job_id
               ), first_run AS (
                   SELECT board, board_job_id, MIN(run_id) AS run_id
                   FROM job_sightings GROUP BY board, board_job_id
               )
               SELECT COUNT(*) AS postings,
                      SUM(p.date_posted IS NOT NULL) AS with_date,
                      SUM(p.date_posted IS NULL
                          AND f.run_id = (SELECT MIN(id) FROM scrape_runs)) AS undated_and_censored
               FROM per_posting p LEFT JOIN first_run f USING (board, board_job_id)"""
        ).fetchone()
        return {
            "postings": row["postings"],
            "with_date": row["with_date"] or 0,
            "without_date": (row["postings"] or 0) - (row["with_date"] or 0),
            "undated_and_censored": row["undated_and_censored"] or 0,
            "by_date": self.conn.execute(
                """SELECT COALESCE(date_posted, '(none)') AS day, COUNT(*) AS postings
                   FROM (SELECT board, board_job_id, MAX(date_posted) AS date_posted
                         FROM raw_jobs GROUP BY board, board_job_id)
                   GROUP BY day ORDER BY day DESC"""
            ).fetchall(),
        }

    def stats(self) -> dict[str, Any]:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "runs": one("SELECT COUNT(*) FROM scrape_runs"),
            "raw_versions": one("SELECT COUNT(*) FROM raw_jobs"),
            "distinct_postings": one("SELECT COUNT(*) FROM (SELECT DISTINCT board, board_job_id FROM raw_jobs)"),
            "sightings": one("SELECT COUNT(*) FROM job_sightings"),
            # Counted per posting, across all its versions — matching what
            # backfill considers "done" — not per newest version.
            "by_board": self.conn.execute(
                """SELECT board, COUNT(*) AS postings, SUM(has_description) AS with_description
                   FROM (SELECT board, board_job_id,
                                MAX(COALESCE(json_extract(payload, '$.description'), '') <> '')
                                    AS has_description
                         FROM raw_jobs GROUP BY board, board_job_id)
                   GROUP BY board ORDER BY postings DESC"""
            ).fetchall(),
            "reviewed": one(
                "SELECT COUNT(*) FROM (SELECT DISTINCT board, board_job_id FROM job_reviews)"
            ),
            "kept": one(
                """SELECT COUNT(*) FROM (
                       SELECT v.verdict FROM job_reviews v
                       JOIN (SELECT board, board_job_id, MAX(id) mid FROM job_reviews GROUP BY 1, 2) m
                         ON m.mid = v.id
                       WHERE v.verdict = 'keep')"""
            ),
            "backfill_failed": one(
                f"SELECT COUNT(*) FROM backfill_attempts WHERE status = 'error' AND attempts >= {MAX_BACKFILL_ATTEMPTS}"
            ),
        }
