# pm-jobs

A personal job scanner for one product manager. Not a general-purpose tool — it
deliberately encodes one person's search criteria, experience, and preferences.

## Pipeline

1. **Scrape** job boards on a schedule and keep the raw output forever. ✅ done
2. **Normalize + dedupe** into a clean `jobs` table. _next_
3. **Filter + rank** against a resume and a preferences file. _planned_
4. **Research** the top-ranked jobs automatically. _future_

Work is tracked in [beads](https://github.com/steveyegge/beads): `bd list`.

## The one rule

**Raw scrape output is immutable and separate from everything downstream.**

Search criteria and preferences both get retuned constantly. Because raw output
is preserved, retuning dedup rules or ranking is a re-run over stored data —
seconds, no network. If raw were thrown away, every tweak would mean re-scraping
and eventually getting rate-limited.

## Usage

```bash
uv sync

uv run pm-jobs searches         # what is configured
uv run pm-jobs scrape --dry-run # what would be hit, without hitting it
uv run pm-jobs scrape           # scrape, then backfill descriptions
uv run pm-jobs scrape --search pm-north-holland
uv run pm-jobs scrape --no-backfill
uv run pm-jobs backfill         # descriptions only; --limit N, --retry-failed
uv run pm-jobs runs             # recent runs; --show N for per-leg detail
uv run pm-jobs stats            # what is in the store
```

## Descriptions

LinkedIn's search endpoint returns no job description — 0 of 56 postings on
live data, while Indeed returned one for 58 of 58. Stage 3 cannot rank a job on
its title, so descriptions are fetched from each job's own page.

That fetch runs as its own pass after the scrape, not inside it. Speed is not
the reason: it adds ~0.66s per posting, about 40s for a day's LinkedIn haul.
The reason is blast radius — one request per job is the shape of traffic that
gets rate-limited, and a throttled pass that can be resumed and retried without
touching an already-committed scrape is worth more than the seconds it saves.

`scrape` chains it automatically. A backfill failure never fails the scrape:
the scrape's data is already committed by then. Postings that fail three times
are treated as gone and skipped, so a dead posting is not retried forever;
`backfill --retry-failed` overrides that.

Enriched results are written as a **new content version**, never as an edit,
and deliberately record no sighting — a sighting means a board's search
returned the posting, and a backfill is not that.

Descriptions arrive with `job_level`, `company_industry`, `job_type` and
`job_function`, which stage 3 wants anyway.

## Tuning searches

Everything tunable lives in `searches.yaml`. No Python edits.

Location is stated **once** per search. jobspy accepts location three different
ways (`location`, `google_search_term`, `country_indeed`) and silently searches
the wrong place if they disagree, so all three are derived from that one block.

Scraping does **not** filter by region. Boards return nearby jobs outside the
target area, and those are stored too — widening from "North Holland" to
"Randstad" later then re-filters data already on disk instead of forcing a
re-scrape. Region filtering happens in stage 3.

## Storage

SQLite (`pm_jobs.db`, gitignored). Four tables:

| Table | Answers |
|---|---|
| `scrape_runs` | when did we scrape, did the run finish |
| `scrape_tasks` | how did each (search, term, board) leg go, including failures |
| `raw_jobs` | what did a posting say — one row per distinct *version* |
| `job_sightings` | when did we see a posting — one row per observation |

`raw_jobs` and `job_sightings` are split on purpose. A posting seen on five days
with unchanged text is one `raw_jobs` row and five sightings; if a salary appears
on day three, that becomes a second `raw_jobs` row. Stage 2 gets both "what does
it say now" and "how long has it been up" without guessing.

Re-running an unchanged scrape stores zero new versions but still records
sightings, so the store is safe to run on a schedule.

## Known gaps

- `pm_jobs/linkedin_details.py` depends on a **private** jobspy API
  (`LinkedIn._get_job_details`), so `python-jobspy` is pinned. That module is
  the only place that touches it and fails loudly if it moves — re-check it
  before unpinning.
- Boards disagree on field formats. Two are already reconciled at write time
  (`job_type` spelling and multi-value joining); expect stage 2 to find more.
- No scheduling yet — run it by hand until the search criteria stop moving.
