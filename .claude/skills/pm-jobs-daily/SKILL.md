---
name: pm-jobs-daily
description: Run the daily PM job scan — scrape new postings, read each one, drop the ones that don't fit, and tag and summarize the rest. Use when the user asks to check for new jobs, run the job scan, or refresh the job list.
model: sonnet
context: fork
background: false
allowed-tools: Bash(uv run pm-jobs:*), Read, Write
---

# pm-jobs daily scan

You do the reading yourself. The Python side owns scraping, storage, the two
deterministic filters, dedup, and validation; you answer the questions the
posting can't be filtered on.

Run from the repo root (`/Users/yanghsing/codes/toys/pm-jobs`).

You are running in a forked context, so you have no conversation history —
everything you need is in this file and in the batch files you export. Report
back at the end; that report is all the user sees.

## 1. Scrape

```bash
uv run pm-jobs daily --no-review
```

Scrapes since the last successful run and fills in LinkedIn descriptions. It
prints how many postings are now awaiting review. If that's zero, say so and
stop — there is nothing to read.

## 2. Export a batch

```bash
uv run pm-jobs review --export /tmp/pm-batch.json --limit 10
```

The file contains `instructions` (how to judge), `schema` (the fields to
return), and up to 10 `jobs`. **Read `instructions` from the file and follow
it.** Do not judge from memory of this skill or from a previous run — the
instructions are generated from `preferences.yaml`, so they change when the
user retunes their preferences, and the file is the only current copy.

Ten at a time is deliberate. Reading the whole day's list in one go anchors
later judgments on earlier ones, and a failure loses the batch instead of the
lot.

## 3. Judge the batch

For each job in the file, answer the five questions in `instructions` and
produce one object matching `schema`.

Two things worth slowing down for:

- **The language question decides whether the user can apply at all.** They
  speak English and Mandarin. "Dutch is a plus", a language course offered as a
  benefit, "Dutch/EU working rights", and a mention of a country's regulations
  are **not** requirements. The instructions carry worked examples — use them.
  Search the full description for language wording rather than judging from the
  part you happened to read.
- **`role_certain: false`** means the title mentions product but matched no
  configured role phrase, so the title told us nothing. Decide from the
  responsibilities: a role that owns a product's direction is product
  management; one that owns systems, tooling, design, or engineering for
  products is not.

Write the verdicts to a file:

```json
{
  "run_id": <the run_id from the export file>,
  "verdicts": [
    {
      "raw_job_id": 31,
      "is_product_role": true,
      "languages_required": ["English"],
      "language_evidence": "Fluent in English as a working language",
      "tags": ["consumer"],
      "summary": "..."
    }
  ]
}
```

Every exported job needs a verdict. Tags must come from the set in
`instructions`; an empty array is a normal answer.

## 4. Apply

```bash
uv run pm-jobs review --apply /tmp/pm-verdicts.json
```

It validates against the run and rejects the whole file on any problem —
unknown tag, missing field, a job that isn't awaiting judgement. Fix and
re-apply; nothing is stored until the file is clean.

It prints how many are still to judge. **If that's more than zero, go back to
step 2.** Repeat until it reaches zero.

## 5. Report

Tell the user:

- how many new postings were scraped, and how many are now unread
- what the deterministic filters dropped (`uv run pm-jobs review --drops`)
- anything you dropped for language, with the phrase you based it on — this is
  the call most worth them double-checking
- a one-line pointer: `uv run pm-jobs web`

Keep it short. They're going to read the jobs on the page, not in the terminal.

## Notes

- **Nothing is ever deleted.** A drop is a stored verdict, so a wrong call is
  recoverable: `uv run pm-jobs review --re-review` re-decides everything and
  leaves read/favorite state alone.
- If the user wants this to run unattended later, `uv run pm-jobs review` does
  the same judging through the API instead, using the same instructions. It
  needs `ANTHROPIC_API_KEY`.
- Don't write to `pm_jobs.db` directly. `--apply` is what keeps a malformed
  verdict from becoming corrupt state.
