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
uv run pm-jobs scrape           # run everything enabled
uv run pm-jobs scrape --search pm-north-holland
uv run pm-jobs runs             # recent runs; --show N for per-leg detail
uv run pm-jobs stats            # what is in the store
```

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

- **LinkedIn returns no job descriptions** (Indeed returns them for every row).
  Ranking needs descriptions, so stage 2 must add a backfill pass. Fetching them
  inline is ~10x slower and more ban-prone, hence the separate pass.
- No scheduling yet — run it by hand until the search criteria stop moving.
