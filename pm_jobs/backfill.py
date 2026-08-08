"""Fill in job descriptions that the board's search endpoint did not return.

LinkedIn's search results carry no description, and stage 3 cannot rank a job
on its title alone. Descriptions come from each job's own page — one extra HTTP
request per posting.

This runs as its own pass rather than inside the scrape. The cost is not the
reason: measured on live data it adds ~0.66s per job, about 40s for a day's
LinkedIn haul. The reason is blast radius. One request per job is exactly the
shape of traffic that gets rate-limited, and a pass that can be throttled,
resumed, and retried without touching an already-committed scrape is worth more
than the seconds it saves.

Enriched results are written as a new content version of the posting, never as
an edit to the stored one — the raw store stays append-only.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable

from .linkedin_details import DetailFetchUnavailable, LinkedInDetailFetcher
from .store import RawStore, row_to_payload

# Gentler than the scrape's pause: this is one request per posting against a
# site that notices bursts.
PAUSE_BETWEEN_FETCHES_SEC = 1.5


@dataclass
class BackfillResult:
    considered: int = 0
    filled: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    aborted: str | None = None

    @property
    def status(self) -> str:
        if self.aborted:
            return "error"
        if self.failed and not self.filled:
            return "error"
        return "partial" if self.failed else "ok"

    def summary(self) -> str:
        if self.aborted:
            return f"backfill aborted: {self.aborted}"
        if not self.considered:
            return "backfill: nothing missing descriptions"
        return f"backfill: {self.filled} filled, {self.failed} failed, of {self.considered} missing"


def run_backfill(
    store: RawStore,
    limit: int | None = None,
    retry_failed: bool = False,
    pause: float = PAUSE_BETWEEN_FETCHES_SEC,
    fetcher: Callable[[str], dict] | None = None,
    on_progress: Callable[[str], None] = lambda msg: None,
) -> BackfillResult:
    pending = store.jobs_needing_details(board="linkedin", limit=limit, retry_failed=retry_failed)
    result = BackfillResult(considered=len(pending))
    if not pending:
        return result

    if fetcher is None:
        try:
            fetcher = LinkedInDetailFetcher().fetch
        except DetailFetchUnavailable as exc:
            result.aborted = str(exc)
            return result

    on_progress(f"backfill: {len(pending)} LinkedIn postings missing descriptions")

    for index, row in enumerate(pending, start=1):
        board_job_id = row["board_job_id"]
        label = f"[{index}/{len(pending)}] {row['company']} · {row['title']}"

        try:
            # The per-job API returns richer Python types than the search path
            # (enums, lists); normalize so both write identically shaped JSON.
            # Empties are dropped so a thin detail page cannot blank out a
            # field the search already filled in.
            details = {k: v for k, v in row_to_payload(fetcher(board_job_id)).items() if v not in (None, "", [])}
            # The search path joins multi-valued job_type into "parttime, fulltime";
            # the detail API returns a list. Match the search path so stage 2 sees
            # one format per field rather than one format per code path.
            if isinstance(details.get("job_type"), list):
                details["job_type"] = ", ".join(details["job_type"])
        except DetailFetchUnavailable as exc:
            # The private API moved. Every remaining fetch will fail the same
            # way, so stop rather than burning through the whole list.
            result.aborted = str(exc)
            on_progress(f"backfill aborted: {exc}")
            break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            result.failed += 1
            result.errors.append(f"{board_job_id}: {error}")
            store.record_attempt("linkedin", board_job_id, "error", error)
            store.conn.commit()
            on_progress(f"{label} → failed: {error}")
        else:
            if details.get("description"):
                payload = json.loads(row["payload"])
                payload.update(details)
                store.add_version("linkedin", board_job_id, payload)
                store.record_attempt("linkedin", board_job_id, "ok")
                result.filled += 1
                on_progress(f"{label} → {len(details['description'])} chars")
            else:
                result.failed += 1
                store.record_attempt("linkedin", board_job_id, "error", "no description on job page")
                on_progress(f"{label} → no description on page")
            store.conn.commit()

        if pause and index < len(pending):
            time.sleep(pause)

    return result
