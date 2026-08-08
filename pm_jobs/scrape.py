"""Run configured searches against the job boards and persist raw output.

Every (search, term, board) leg is its own task with its own try/except. One
board rate-limiting you must not cost you the other board's results, and the
failure must be visible afterwards rather than scrolling past in a terminal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .config import Config, Search
from .store import RawStore, row_to_payload

# jobspy hits real sites; a short pause between legs keeps a multi-term run
# from looking like a burst.
PAUSE_BETWEEN_TASKS_SEC = 2.0


@dataclass
class TaskResult:
    search_name: str
    term: str
    board: str
    rows_returned: int = 0
    rows_new: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class RunResult:
    run_id: int | None
    tasks: list[TaskResult]

    @property
    def status(self) -> str:
        if not self.tasks:
            return "failed"
        failed = sum(1 for t in self.tasks if not t.ok)
        if failed == 0:
            return "ok"
        return "failed" if failed == len(self.tasks) else "partial"

    @property
    def rows_returned(self) -> int:
        return sum(t.rows_returned for t in self.tasks)

    @property
    def rows_new(self) -> int:
        return sum(t.rows_new for t in self.tasks)


def _default_scraper(**kwargs):
    # Imported lazily: jobspy pulls in pandas and is slow to import, which
    # makes `--dry-run` and `stats` feel sluggish for no reason.
    from jobspy import scrape_jobs

    return scrape_jobs(**kwargs)


def plan(searches: Iterable[Search]) -> list[tuple[Search, str, str]]:
    """Flatten searches into the ordered list of legs a run will execute."""
    return [(search, term, board) for search in searches for term, board in search.jobs()]


def run_scrape(
    config: Config,
    store: RawStore,
    searches: list[Search] | None = None,
    scraper: Callable = _default_scraper,
    on_progress: Callable[[str], None] = lambda msg: None,
    pause: float = PAUSE_BETWEEN_TASKS_SEC,
) -> RunResult:
    searches = searches if searches is not None else config.enabled()
    if not searches:
        raise ValueError("no enabled searches to run")

    legs = plan(searches)
    run_id = store.start_run(str(config.path), [s.name for s in searches])
    results: list[TaskResult] = []

    for index, (search, term, board) in enumerate(legs, start=1):
        label = f"[{index}/{len(legs)}] {search.name} · {term!r} · {board}"
        on_progress(f"{label} …")

        task_id = store.start_task(run_id, search.name, term, board)
        result = TaskResult(search.name, term, board)

        try:
            frame = scraper(**search.jobspy_kwargs(term, board))
            rows = [] if frame is None else frame.to_dict(orient="records")
            result.rows_returned = len(rows)
            for row in rows:
                if store.record_job(run_id, task_id, search.name, term, board, row_to_payload(row)):
                    result.rows_new += 1
            store.conn.commit()
            store.finish_task(task_id, "ok", result.rows_returned, result.rows_new)
            on_progress(f"{label} → {result.rows_returned} rows, {result.rows_new} new")
        except Exception as exc:  # one board's failure must not end the run
            store.conn.rollback()
            result.error = f"{type(exc).__name__}: {exc}"
            store.finish_task(task_id, "error", error=result.error)
            on_progress(f"{label} → FAILED: {result.error}")

        results.append(result)
        if pause and index < len(legs):
            time.sleep(pause)

    run = RunResult(run_id=run_id, tasks=results)
    store.finish_run(run_id, run.status)
    return run
