"""Day-1 / day-2 scenario: does backfill re-fetch postings it already enriched?"""
import sys, tempfile, os
import os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from pm_jobs.store import connect, RawStore
from pm_jobs.backfill import run_backfill

db = os.path.join(tempfile.mkdtemp(), "t.db")
conn = connect(db); store = RawStore(conn)
fetches = []
def fetcher(jid):
    fetches.append(jid)
    return {"description": "text " * 200}

def scrape_day(ids, churn=False):
    run_id = store.start_run("x.yaml", ["s"])
    task_id = store.start_task(run_id, "s", "pm", "linkedin")
    for i in ids:
        payload = {"id": f"li-{i}", "title": f"PM {i}", "company": "Acme",
                   "job_url": f"https://x/{i}", "description": None}
        if churn:
            payload["vacancy_count"] = 7   # board churns an unrelated field
        store.record_job(run_id, task_id, "s", "pm", "linkedin", payload)
    conn.commit()

# Day 1: 50 postings, all backfilled
scrape_day(range(1, 51))
r1 = run_backfill(store, pause=0, fetcher=fetcher)
print(f"day 1: scraped 50, fetched {r1.filled}")
assert r1.filled == 50

# Day 2: 50 postings, 40 repeats + 10 new, and the board churns a field on repeats
fetches.clear()
scrape_day(range(11, 51), churn=True)      # the 40 repeats, with churn
scrape_day(range(51, 61))                  # the 10 genuinely new
r2 = run_backfill(store, pause=0, fetcher=fetcher)
print(f"day 2: scraped 50 (40 repeat + 10 new), fetched {r2.filled}")
print(f"        re-fetched already-enriched postings: "
      f"{sorted(int(f.split('-')[1]) for f in fetches if int(f.split('-')[1]) <= 50)}")
assert r2.filled == 10, f"expected 10 fetches, got {r2.filled}"

print("\nOK: only the new postings were fetched")
