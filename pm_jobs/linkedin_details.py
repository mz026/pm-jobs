"""The single place that touches jobspy's private per-job API.

LinkedIn's search endpoint returns no job descriptions — confirmed on live
data: 0 of 56 postings had one, while Indeed returned them for 58 of 58.
Descriptions are only available by fetching each job's own page, which jobspy
exposes as `LinkedIn._get_job_details(job_id)`.

That leading underscore means jobspy can change or remove it in any release
without it being a breaking change on their side. Everything that depends on
it is confined to this module so an upgrade fails loudly here rather than
silently returning empty descriptions everywhere. `python-jobspy` is pinned in
pyproject.toml for the same reason; re-check this module when unpinning.
"""

from __future__ import annotations

from typing import Any

# Fields _get_job_details returns that are worth keeping. Descriptions are the
# reason for the fetch, but the rest are free and stage 3 wants them.
DETAIL_FIELDS = (
    "description",
    "job_level",
    "company_industry",
    "job_type",
    "job_function",
    "job_url_direct",
)


class DetailFetchUnavailable(RuntimeError):
    """jobspy's private per-job API is not shaped the way this module expects."""


def _build_scraper():
    try:
        from jobspy.linkedin import LinkedIn
        from jobspy.model import DescriptionFormat, ScraperInput
    except ImportError as exc:  # pragma: no cover - depends on jobspy internals
        raise DetailFetchUnavailable(
            f"jobspy's LinkedIn internals moved ({exc}). "
            "See pm_jobs/linkedin_details.py — the private API it relies on has changed."
        ) from exc

    scraper = LinkedIn()
    if not hasattr(scraper, "_get_job_details"):
        raise DetailFetchUnavailable(
            "jobspy's LinkedIn scraper no longer exposes _get_job_details. "
            "See pm_jobs/linkedin_details.py."
        )
    # _get_job_details reads self.scraper_input, which only gets set during a
    # normal search. Standalone use has to supply it.
    scraper.scraper_input = ScraperInput(site_type=[], description_format=DescriptionFormat.MARKDOWN)
    return scraper


def strip_board_prefix(board_job_id: str) -> str:
    """'li-4388353369' -> '4388353369'. jobspy prefixes ids; the fetch API wants the bare one."""
    return board_job_id[3:] if board_job_id.startswith("li-") else board_job_id


class LinkedInDetailFetcher:
    """Fetches one job's full detail page. Holds a scraper so the session is reused."""

    def __init__(self):
        self._scraper = None

    def fetch(self, board_job_id: str) -> dict[str, Any]:
        if self._scraper is None:
            self._scraper = _build_scraper()
        details = self._scraper._get_job_details(strip_board_prefix(board_job_id))
        if not isinstance(details, dict):
            raise DetailFetchUnavailable(
                f"_get_job_details returned {type(details).__name__}, expected dict. "
                "See pm_jobs/linkedin_details.py."
            )
        return {k: v for k, v in details.items() if k in DETAIL_FIELDS and v not in (None, "")}
