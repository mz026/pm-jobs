# Daily review flow — implementation plan

One command scrapes since the last run, has a model read each new posting, and
drops the survivors into a web page with unread / favorite / read views.

Supersedes `docs/stage-3-ranking.md` (resume-based ranking, dropped).

---

## Steps

- [ ] **1. Incremental scrape window**
  - [ ] Compute `hours_old` from the last *successful* run (`scrape_runs.status = 'ok'`)
  - [ ] Add overlap margin; floor at 24h, cap at 72h
  - [ ] `--since-hours N` to override, `--full` for the old fixed window
- [ ] **2. Schema: state and reviews**
  - [ ] `job_state` table (read, favorite) + migration
  - [ ] `job_reviews` table (verdict, tags, summary) + migration
  - [ ] `pending_review()` query — newest raw version with no review for that version
- [ ] **3. `preferences.yaml`** — languages, tag definitions, model config, loader
- [ ] **4. Deterministic pre-filters** (no model call)
  - [ ] Description-language detection → drop Dutch-written
  - [ ] Product-role title match → drop non-product roles
  - [ ] Record both as `job_reviews` rows with `stage='prefilter'`
- [ ] **5. AI review pass**
  - [ ] Anthropic client, cached prefix, structured output schema
  - [ ] Per-job call: verdict + tags + summary
  - [ ] Per-job failures recorded, never abort the run
  - [ ] Idempotent — re-running reviews nothing already reviewed
- [ ] **6. `pm-jobs review` CLI** — `--limit`, `--dry-run`, `--re-review`
- [ ] **7. Cross-board dedup** — normalized company + exact title, link duplicates
- [ ] **8. Web UI**
  - [ ] Server + templates, three views (unread / favorite / read)
  - [ ] `/job/<board>/<id>/open` — mark read, redirect to source
  - [ ] Favorite toggle
- [ ] **9. `pm-jobs daily`** — scrape → backfill → review, one command
- [ ] **10. Verify end to end** on real data, then measure a day's actual cost

---

## The flow

```
pm-jobs daily
   │
   ├─ scrape          since last successful run
   ├─ backfill        LinkedIn descriptions (existing)
   └─ review          new postings only
        │
        ├─ prefilter  Dutch-written?  non-product-role?   ← no model call
        └─ judge      language requirements, tags, summary ← claude-sonnet-5

pm-jobs web           → http://localhost:8000
```

---

## What the model decides — and what it can't

**The model never deletes anything.** `raw_jobs` stays append-only. A review is a
verdict written *alongside* the posting, and the web page shows survivors. A
wrong drop is therefore a query away from being found, and re-reviewing with a
better prompt costs nothing because nothing is re-scraped.

This is what makes a cheap model the right choice here. The judgment that
matters — "does this job require a language I don't speak" — is exactly where a
small model errs, and your own data shows why. 29 of 115 postings mention Dutch,
and the word carries five distinct meanings:

| What the posting says | Correct call |
|---|---|
| "Vloeiend in Nederlands en Engels" | **Drop** — real requirement |
| "Fluent in English; Dutch or German proficiency is **a plus**" | **Keep** |
| "You have full **Dutch/EU working rights**" | **Keep** — visa, not language |
| "Access to the Leaseweb Academy… **(Dutch) courses**" | **Keep** — a perk |
| "expertise in **Dutch pension regulation**" | **Keep** — domain knowledge |

A keyword match drops all 29, including roles you could take today. That is the
model's job. Everything below it is deterministic.

---

## Filters, in order

**1. Written in Dutch → drop. Deterministic, no model call.**
23 of 115 descriptions are Dutch-language. Word-frequency detection gets this
right, cannot hallucinate, and removes a fifth of the token spend before it is
incurred.

**2. Not a product role → drop. Deterministic, no model call.**
Only 43 of 115 titles are product roles. Without this the unread list contains
`Medewerker Poedercoating` (powder coating), `Dyson Brand Ambassador`, and
`.Net Developer`.

**33 of 115 survive both** — so roughly a third of each day's scrape reaches the
model.

**3. Requires a language other than English or Mandarin → drop. Model.**
The only language call the model makes.

Everything that survives gets tags and a summary.

---

## Tags

Three, defined in `preferences.yaml` so you can retune the wording without
touching code:

| Tag | Means |
|---|---|
| `consumer` | Consumer-facing, or leverages consumer experience — includes marketplaces where one side is consumer |
| `ed-tech` | Education or ed-tech |
| `gamification` | Gamification, engagement loops, rewards/progression systems |

A job can carry none, some, or all three. Untagged jobs still appear — they
passed the filters, they just don't match a preference. Tags are how you skim,
not a gate.

**No score field.** The list sorts by date, so a score would have no consumer.
If you later want a relevance sort, tags plus a score column can be added then.

---

## Schema

Three new tables. The split matters more than the columns.

```sql
-- Yours. Permanent. Never regenerated.
CREATE TABLE job_state (
    board         TEXT NOT NULL,
    board_job_id  TEXT NOT NULL,
    is_read       INTEGER NOT NULL DEFAULT 0,
    is_favorite   INTEGER NOT NULL DEFAULT 0,
    read_at       TEXT,
    favorited_at  TEXT,
    PRIMARY KEY (board, board_job_id)
);

-- The model's. Regenerable. One row per (posting version, review run).
CREATE TABLE job_reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES review_runs(id),
    board         TEXT NOT NULL,
    board_job_id  TEXT NOT NULL,
    raw_job_id    INTEGER NOT NULL REFERENCES raw_jobs(id),
    stage         TEXT NOT NULL,      -- prefilter | judged | error
    verdict       TEXT NOT NULL,      -- keep | drop
    drop_reason   TEXT,               -- dutch_description | not_product_role | language_required | ...
    tags          TEXT NOT NULL DEFAULT '[]',   -- JSON array
    summary       TEXT,
    model         TEXT,
    error         TEXT,
    reviewed_at   TEXT NOT NULL,
    UNIQUE (raw_job_id, run_id)
);

CREATE TABLE review_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    model        TEXT NOT NULL,
    prefs_hash   TEXT NOT NULL,
    considered   INTEGER NOT NULL DEFAULT 0,
    prefiltered  INTEGER NOT NULL DEFAULT 0,
    judged       INTEGER NOT NULL DEFAULT 0,
    kept         INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL
);
```

**Why `job_state` is separate from `job_reviews`:** so you can re-run the review
pass — new prompt, new model, changed preferences — without wiping which jobs
you have read and favorited. In one table, every re-review either clobbers your
state or needs careful merging.

**Why neither lives on `raw_jobs`:** `raw_jobs` is append-only and versioned. A
re-listed posting writes a *new row*, so a favorite stored there would be
orphaned the moment a board re-lists the job. Both new tables key on
`(board, board_job_id)`, which is stable across versions.

**Why `job_reviews.raw_job_id`:** pins the exact content version reviewed. When
a backfill adds a description or a board re-lists a posting with changes, the
job becomes eligible for re-review — and a stale review is distinguishable from
a current one.

Rejected postings are stored with the rule that rejected them. Without that, a
too-aggressive filter is invisible: you would only ever see what survived.

---

## `preferences.yaml`

The tuning surface. No Python edits.

```yaml
languages:
  # A job requiring any language outside this list is dropped.
  speak: [English, Mandarin]

tags:
  consumer: >
    Consumer-facing product, or a role that leverages consumer product
    experience. Marketplaces count when at least one side is consumers.
  ed-tech: >
    Education, learning, training, or ed-tech.
  gamification: >
    Gamification, engagement loops, rewards, streaks, progression systems.

review:
  model: claude-sonnet-5
  effort: low
  summary_sentences: 3
```

---

## The model call

**`claude-sonnet-5`**, one call per surviving job, structured output so the
result is a validated object rather than prose to parse.

Each call gets a **cached prefix** — the language rule, tag definitions, and
output instructions, byte-identical across every job in the run — plus a
**per-job suffix** with title, company, location, and description. Anything
volatile (job IDs, timestamps) must stay in the suffix or the cache never hits.
Sonnet 5's minimum cacheable prefix is 1024 tokens, so the instruction block
needs to clear that bar to cache at all.

Returned per job:

```jsonc
{
  "keep": true,
  "drop_reason": null,              // set when keep=false, e.g. "requires fluent German"
  "languages_required": ["English"],// what the posting actually demands
  "tags": ["consumer", "ed-tech"],
  "summary": "..."                  // 2-3 sentences
}
```

`languages_required` is not displayed. It exists so that when a drop looks
wrong, you can see what the model thought it read — the difference between
auditing a decision and guessing at it.

### Cost

Measured from your corpus: descriptions average 6,296 characters (~1,575
tokens). Daily volume from posting dates is 34 / 45 / 24 per day, so ~35/day,
of which ~a third reach the model after the deterministic filters.

| | Per day | Per month |
|---|---|---|
| Sonnet 5, thinking off | ~$0.10 | **~$3** |
| Sonnet 5, adaptive thinking at low effort | ~$0.25 | **~$8** |
| Opus 5, adaptive thinking at low effort | ~$0.60 | ~$18 |

Adaptive thinking bills thinking tokens as output, which is most of the gap.
Start with adaptive on at low effort — the language call is the one decision
worth thinking about, and $8/month is not worth optimizing. Turn thinking off
only if a cost review ever says otherwise.

Credentials resolve from `ANTHROPIC_API_KEY` or an `ant auth login` profile; no
key needs to live in the repo.

---

## Cross-board dedup

Now worth doing, having been descoped earlier.

At 7% duplication (8 pairs in 115) a duplicate used to be a minor annoyance in a
CSV. With a read/unread list it is worse: you mark one copy read, its twin stays
`unread` forever, and it never leaves the view.

The cheap tier is enough — block on normalized company (strip `B.V.`, `N.V.`,
country qualifiers: `ING` vs `ING Nederland`, `GAC Motor Europe` vs
`GAC Motor Europe B.V.`), then require exact normalized title. That matched all
8 known pairs with no false positives, in roughly 20 lines. Skip the fuzzy tier
and the description-similarity adjudication from `pm-jobs-0kw.5`.

Duplicates are *linked*, not merged: the list shows one row with both source
links, and read/favorite applies to the pair.

---

## Web UI

Server-rendered HTML, form posts, no JavaScript build. Flask — one dependency,
templates, done. Bound to localhost.

```
GET  /                       unread (default)
GET  /favorites
GET  /read
GET  /job/<board>/<id>/open  mark read → 302 to the source URL
POST /job/<board>/<id>/favorite
```

Marking read happens by **clicking through to the job**, via a redirect route
rather than a direct link — no JavaScript needed, and the state matches what you
actually did. An explicit toggle undoes it.

Each row shows: **title** (the click-through link), **summary**, **posted at**,
**tags**.

### Sorting

`scraped_at` descending, then `posted_at` descending — with one correction:
`scraped_at` must be the posting's **earliest** sighting, not its latest.

Using the latest sighting, every job still live on the boards would jump back to
the top of `unread` every single day, and the ordering would churn constantly.
The earliest sighting is stable, so a posting holds its place until you read it.

Two things to expect in the list: 11 of 115 postings have no `posted_at` and
sort last within their scrape day, and one Indeed posting is dated 39 days
before the 72-hour search that returned it — the boards' own dates are not
always truthful.

---

## Ordering of the build

Steps 1–4 are pure plumbing and cost nothing to run — do them first and check
the prefilter output against the real 115 before spending a cent. Step 5 is the
first step that calls the API. Step 8 is independent of 5–7 and can be built
against prefilter-only data.

Rough sizing: steps 1–4 about two hours, step 5 an afternoon including prompt
iteration, steps 6–7 an hour, step 8 two to three hours, step 9 twenty minutes.
