# pm-jobs

A personal job scanner for one product manager. Not a general-purpose tool — it
deliberately encodes one person's criteria.

Once a day: scrape what's new since the last run, have a model read each
posting and drop the ones you can't take, then read the survivors on a local
page with unread / favorite / read views.

## Daily use

Two steps. In Claude Code:

```
/pm-jobs-daily
```

Scrapes what's new, reads each posting, drops what doesn't fit, tags and
summarizes the rest. No API key — it runs on the session you invoke it from.

Then read them:

```bash
uv run pm-jobs web        # http://localhost:8000
```

That's the whole loop. Everything below is for when something needs
investigating or retuning.

First time only: `uv sync`, then set your criteria in the two files under
[Tuning](#tuning).

## The one rule

**Nothing is ever deleted.** The raw store is append-only, and a "drop" is a
verdict written alongside a posting, never a removal. So a filter that's too
aggressive is a query away from being found (`pm-jobs review --drops`), and
re-deciding everything with a better prompt costs nothing because nothing is
re-scraped.

That's also what makes a cheap model safe to use here. The judgment that
matters — *does this job require a language I don't speak* — is exactly where a
small model errs, and its mistakes have to be recoverable.

## When something needs looking at

```bash
uv run pm-jobs review --drops      # what got dropped, and why
uv run pm-jobs review --re-review  # re-decide everything (keeps read/favorite)
uv run pm-jobs stats               # what's in the store
```

`--drops` is the one worth knowing. It's how you check whether a filter is
costing you jobs — most usefully the language calls, which are the only
judgment in the whole pipeline that can quietly lose you something real.

<details>
<summary>Every other command</summary>

Rarely needed — `/pm-jobs-daily` runs the ones that matter.

```bash
uv run pm-jobs daily               # scrape → backfill → review, all via the API
uv run pm-jobs daily --no-review   # …leaving the judging to the skill

uv run pm-jobs scrape              # --full, --since-hours N, --dry-run
uv run pm-jobs backfill            # LinkedIn descriptions only
uv run pm-jobs review              # judge via the API; --dry-run costs nothing
uv run pm-jobs review --export F   # emit a batch for an agent to judge
uv run pm-jobs review --apply F    # store an agent's verdicts (validated)

uv run pm-jobs searches            # what's configured
uv run pm-jobs runs                # scrape history, including failures
uv run python tests/run_all.py     # the smoke suites
```

</details>

### Running it without Claude Code

`uv run pm-jobs daily` does the same job through the API instead of the skill —
same instructions, same policy, same storage. It needs `ANTHROPIC_API_KEY` and
costs roughly $8/month, and it's the path to use if you ever want this on a
cron or in launchd, which the skill can't do because it needs a session.

## Tuning

Two files, no Python edits.

**`searches.yaml`** — terms, boards, location, window. Location is stated once
per search; jobspy accepts it three different ways and silently searches the
wrong place if they disagree, so all three are derived from one block.

**`preferences.yaml`** — the languages you speak, the titles that count as
product roles, and what each tag means.

## What gets dropped

Three filters. Two are deterministic and cost nothing; only the third needs a
model.

| Filter | How | Effect on the current corpus |
|---|---|---|
| Description written in Dutch | Word-frequency detection | 13 of 149 |
| Not a product role | Title match against `preferences.yaml` | 79 of 149 |
| Requires a language you don't speak | Model | on the remainder |

A title that mentions "product" but matches no configured phrase is **not**
dropped — it goes to the model. That safety net exists because a strict word
list silently discarded four real roles on first contact with the data:
`Senior productmanager` (Dutch compound), `Director Technical Product
Management`, `Chief Product & Technology Officer`, and one titled `Senior
Product Ownwer` that no word list will ever catch.

### Why the language call needs a model

29 postings mention Dutch, and the word means five different things:

| What the posting says | Correct call |
|---|---|
| "Vloeiend in Nederlands en Engels" | Drop — real requirement |
| "Fluent in English; Dutch **is a plus**" | Keep |
| "full **Dutch/EU working rights**" | Keep — visa, not language |
| "(Dutch) **courses**" in the benefits | Keep — a perk |
| "expertise in **Dutch pension regulation**" | Keep — domain knowledge |

A keyword match drops all 29, including roles you could take today.

## Storage

SQLite (`pm_jobs.db`, gitignored).

| Table | Holds | Regenerable? |
|---|---|---|
| `raw_jobs` | One row per distinct content version of a posting | No — the only thing that needs re-scraping |
| `job_sightings` | One row per observation | No |
| `job_reviews` | The model's verdicts, tags, summaries | Yes — thrown away on `--re-review` |
| `job_state` | **Yours**: read, favorite | No — never regenerated |
| `scrape_runs` / `scrape_tasks` / `review_runs` | Run history, including failures | — |

`job_state` is separate from `job_reviews` so you can re-review with a better
prompt without wiping which jobs you've read. Both key on
`(board, board_job_id)` rather than `raw_jobs.id`, which is versioned — state
stored against a version would be orphaned the moment a board re-lists a job.

## Duplicates

The same posting appears on both boards about 7% of the time. They're **linked,
not merged**: one row carrying both source links, and read/favorite applied to
every copy. Without that, marking one copy read strands its twin in `unread`
permanently.

Matching is normalized company + exact title, which caught all 8 known pairs
with no false positives. No fuzzy matching.

## Freshness

Each run asks the boards for the window since the last *successful* run — a
failed run is not a watermark. The window is floored at 24h and given 6 hours of
overlap, because jobspy filters on a board's posting date, not on when a posting
became visible in search. The overlap is free: an unchanged posting hashes
identically and stores nothing.

Sorting uses each posting's **earliest** sighting. Using the latest would send
every job still live on the boards back to the top of `unread` every day.

## Known gaps

- `pm_jobs/linkedin_details.py` uses a **private** jobspy API
  (`LinkedIn._get_job_details`), so `python-jobspy` is pinned. That module is
  the only place that touches it and fails loudly if it moves.
- No salary anywhere — all postings returned empty salary fields.
- LinkedIn omits `date_posted` for about a fifth of postings; those fall back to
  first sighting, which for the first run is only a lower bound on age.
- No scheduling yet — run `pm-jobs daily` by hand, or wrap it in launchd.
