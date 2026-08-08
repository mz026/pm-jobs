# Stage 3 — Filter and rank against experience and preferences

Bead: `pm-jobs-0kw.3`. Turns the 115 stored postings into a short ranked
shortlist with a written reason per job, instead of a table you have to read.

Stage 2 (normalization, dedup) was descoped, so this stage reads `raw_jobs`
directly — the newest version of each `(board, board_job_id)` — and does what
little normalization it needs inline, in memory. Nothing normalized is
persisted. That keeps the pipeline two stages long instead of three, at the
cost of re-deriving location and seniority on every run. At 115 postings that
cost is milliseconds.

---

## What you provide

Two files, both versioned in the repo, both yours to edit:

```
profile/
  resume.md           # your CV, plain markdown
  preferences.yaml    # what you want and what you refuse
```

`preferences.yaml` is deliberately split in two halves, because the two tiers
below need different things from it:

```yaml
# Machine-checkable. Runs without an LLM, on every posting.
hard_filters:
  regions: [North Holland]
  max_age_days: 30
  seniority_min: mid          # junior | mid | senior | lead | director
  exclude_companies: []
  require_product_role: true  # title must look like a product role

# Free text. Handed to the model as-is, no schema.
context: |
  I don't speak Dutch. A role that requires fluent Dutch is out; a role where
  Dutch is "a plus" is fine.
  I want to stay hands-on with discovery and customer conversations...
  I'd rather join a company where product reports to the CEO...
```

The structured half exists so cheap filters stay cheap. The free-text half
exists because most of what actually matters about a job — culture, scope,
reporting line, how a company talks about its users — does not survive being
squeezed into an enum.

---

## Architecture: cheap filter, then judgment

**Tier 1 — deterministic filters. No model call.**
Runs on title, company, location, and date. Rejects the obvious.

**Tier 2 — LLM judge.** One call per surviving job, reading the full
description against your resume and preferences. Produces a score and a written
explanation.

The split exists because **62% of what the boards return is not a product
role.** Measured on the current data: only 43 of 115 titles contain a
product-role phrase. The rest is `.Net Developer`, `Senior Category Buyer`,
`Dyson Brand Ambassador`, and `Medewerker Poedercoating` (a powder-coating job).
There is no reason to pay a model to read a powder-coating job description.

Tier 1 is also the part you will tune most often, and it is the part you can
tune without spending anything.

---

## Why Tier 2 has to be a model, not a regex

Your stated criterion — "no Dutch required" — is the clearest case, so it is
worth walking through what's actually in the data.

**29 of 115 descriptions mention Dutch or Nederlands.** The word means at least
five different things across those 29:

| What the posting says | What it means for you |
|---|---|
| "Vloeiend in Nederlands en Engels, zowel mondeling als schriftelijk" | Hard requirement. **Reject.** |
| "Fluent in English; Dutch or German proficiency is **a plus**" | Not a blocker. **Keep.** |
| "You have full **Dutch/EU working rights**" | Not about language at all — visa. **Keep.** |
| "Access to the Leaseweb Academy… **(Dutch) courses**, and trainings" | A perk in the benefits list. **Keep.** |
| "Demonstrate strong expertise in **Dutch pension regulation**" | Domain knowledge, not language. Judgment call. |
| "Je spreekt Nederlands, **of wilt dat snel leren**" | Soft. Judgment call. |

A regex on `\b(dutch|nederlands)\b` rejects all 29 — including the Booking.com
and FareHarbor roles where Dutch is explicitly optional. That is the argument
for judgment over pattern matching, in one criterion. Every other preference
you have will be at least this ambiguous.

One cheap signal is still worth computing before the call: **23 of the 115
descriptions are written in Dutch.** That is a strong prior for a
Dutch-speaking workplace, though not proof — the Leaseweb posting is in English
and still offers Dutch courses. Pass it to the model as a hint; don't filter on
it.

---

## Tier 1 filters in detail

All five run against the raw payload with no model call.

1. **Product role** — title matched against a product-role pattern.
   Currently cuts 115 → 43.
2. **Region** — the raw location string, matched at region level. Both boards'
   spellings must pass: Indeed writes `Amsterdam-Zuidoost, NH, NL`, LinkedIn
   writes `Amsterdam, North Holland, Netherlands`. Match on region and country
   tokens (`NH`/`North Holland`, `NL`/`Netherlands`), never on city equality —
   city equality never matched on any known duplicate pair.
3. **Seniority floor** — asymmetric by board. LinkedIn supplies `job_level` for
   57 of 57 postings; Indeed supplies it for 0 of 58, so Indeed's seniority is
   read out of the title (`Junior`, `Medior`, `Senior`, `Staff`, `Principal`,
   `Lead`, `Director`, `Head`, `VP`). Unknown seniority passes rather than
   fails — a missing field should not silently drop a job.
4. **Age** — `date_posted` when present, else the earliest sighting. 11 of 115
   postings have no date; those fall back, and postings first seen in the
   earliest run carry a censored age (lower bound only), so the age filter must
   be lenient with them.
5. **Company blocklist** — exact match after normalizing legal suffixes
   (`ING` vs `ING Nederland`, `GAC Motor Europe` vs `GAC Motor Europe B.V.`).

**No salary filter is possible.** All 115 postings have empty
`min_amount`/`max_amount`/`interval`/`currency` — neither board returned salary
for anything. If salary matters it has to be read out of the description text
by Tier 2, and it will often simply be absent.

---

## Tier 2: the judge

**Model: `claude-opus-5`.** One call per surviving job. Structured output via
`output_config.format` so the result is a validated object rather than prose to
parse.

Each call gets:

- **Cached prefix** (stable across every job in a run): your resume, your
  preferences `context`, and the scoring instructions.
- **Per-job suffix**: title, company, location, date, board, the description,
  and the cheap signals Tier 1 already computed (description language,
  derived seniority).

Returned per job:

```jsonc
{
  "score": 0-100,
  "verdict": "strong" | "worth_a_look" | "no",
  "blockers": ["Requires fluent Dutch"],       // dealbreakers actually found
  "why": "2-3 sentences, specific to this job and this resume",
  "unknowns": ["Salary not stated"]            // what the posting didn't say
}
```

`blockers` and `unknowns` matter as much as `score`. A score alone is not
reviewable — you cannot tell a job that scored 40 because it's junior from one
that scored 40 because the description was uninformative. `unknowns` is also
what tells you whether a stage-4 research pass would actually add anything.

### Cost

Measured on the real corpus: 115 descriptions totalling 724,106 characters,
roughly 181,000 tokens. At Opus 5 rates ($5/M input, $25/M output), ranking
**all 115 costs about $2.25** — and Tier 1 cuts most of them before the model
ever sees them, so a real run is well under a dollar.

**Cost is not a design constraint at this scale.** Don't compromise the
ranking to save cents; if daily volume ever grows enough to matter, the lever
is a cheaper model for a first pass, not a worse prompt.

Prompt caching on the resume + preferences prefix cuts the repeated cost by
~90% within a run. The prefix must be byte-identical across calls, so anything
volatile (timestamps, job IDs) goes in the suffix.

---

## Storage

Two new tables. Rankings are persisted so a re-rank after a preference change
can be diffed against the previous one — the point is to see *what your edit
changed*, not just to get a new list.

```sql
CREATE TABLE ranking_runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    model         TEXT NOT NULL,
    resume_hash   TEXT NOT NULL,   -- so a run is attributable to a profile version
    prefs_hash    TEXT NOT NULL,
    considered    INTEGER,          -- postings that entered tier 1
    judged        INTEGER,          -- postings that reached tier 2
    status        TEXT NOT NULL
);

CREATE TABLE rankings (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES ranking_runs(id),
    board         TEXT NOT NULL,
    board_job_id  TEXT NOT NULL,
    raw_job_id    INTEGER NOT NULL REFERENCES raw_jobs(id),  -- exact version judged
    stage         TEXT NOT NULL,   -- 'filtered' | 'judged'
    filter_reason TEXT,            -- which tier-1 rule rejected it
    score         INTEGER,
    verdict       TEXT,
    blockers      TEXT,            -- JSON array
    why           TEXT,
    unknowns      TEXT,            -- JSON array
    UNIQUE (run_id, board, board_job_id)
);
```

`raw_job_id` pins the exact content version that was judged, so a score is
always traceable to the text it was based on — important once backfill or a
re-list changes a posting after it was ranked.

Rejected postings are stored too, with the rule that rejected them. Without
that, a filter that is too aggressive is invisible: you would only ever see
what survived.

---

## Commands

```
pm-jobs rank                    # tier 1 + tier 2, store a ranking run
pm-jobs rank --dry-run          # tier 1 only; show what survives and what each rule cut
pm-jobs rank --limit 20         # cap the model calls while tuning
pm-jobs shortlist               # newest run, verdict >= worth_a_look, ranked
pm-jobs shortlist --run N
pm-jobs diff-runs A B           # what changed after a preference edit
```

`--dry-run` is the tuning surface. Tier 1 is where you will spend most of your
iterations, and it costs nothing to run.

---

## Build order

1. **Profile loading + hashing** — `resume.md`, `preferences.yaml`, schema
   validation, stable hashes for run attribution.
2. **Tier 1 filters** + `rank --dry-run`. Tune against the real 115 until the
   survivor set looks right. No model calls yet.
3. **Storage** — `ranking_runs`, `rankings`.
4. **Tier 2 judge** — Anthropic SDK, cached prefix, structured output, one
   call per survivor, failures recorded per job rather than aborting the run.
5. **`shortlist` and `diff-runs`** — the part you actually read.

Steps 1–3 are the half-day. Step 4 is an afternoon including prompt iteration.
Step 5 is an hour.

---

## Decisions I need from you

1. **Feedback loop — in or out of v1?** Marking jobs interesting/not, and
   feeding that back into scoring, is a genuinely useful signal and also the
   thing most likely to balloon this stage. My recommendation: ship without it,
   add `pm-jobs mark <job> good|bad` once you've read a few real shortlists and
   know what the model is getting wrong.

2. **Shortlist output — terminal, or a file?** A CLI table is fastest to build.
   A generated markdown file is easier to read on a phone and easier to keep as
   a record of what you've already reviewed.

3. **Seniority floor — what is it?** Your resume will imply it, but the hard
   filter needs an explicit value, and getting it wrong silently drops jobs.

4. **How much noise do you want to see?** Tier 1 currently cuts 115 → ~43 on
   the product-role rule alone. If you'd rather eyeball borderline titles than
   risk a mis-cut, that rule becomes a score penalty in Tier 2 instead of a
   hard filter — at roughly 2.5× the model cost, which is still under $3.
