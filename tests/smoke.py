"""Synthetic check of store + runner semantics without touching real boards."""
import sys, tempfile, os
import pandas as pd
import os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from pm_jobs.config import load_config
from pm_jobs.store import connect, RawStore
from pm_jobs.scrape import run_scrape

cfg = load_config(str(ROOT / "searches.yaml"))

ROW = {
    "id": "li-1", "site": "linkedin", "title": "Product Manager", "company": "Acme",
    "location": "Amsterdam, North Holland, Netherlands", "job_url": "https://x/1",
    "min_amount": float("nan"), "date_posted": pd.Timestamp("2026-08-07"),
    "is_remote": False, "description": None,
}

calls = {"n": 0}
def fake(**kw):
    calls["n"] += 1
    if kw["site_name"] == ["indeed"]:
        raise RuntimeError("simulated 429 from indeed")
    return pd.DataFrame([ROW])

db = os.path.join(tempfile.mkdtemp(), "t.db")
conn = connect(db); store = RawStore(conn)

r1 = run_scrape(cfg, store, scraper=fake, pause=0)
print("run1 status:", r1.status, "| returned:", r1.rows_returned, "| new:", r1.rows_new)
assert r1.status == "partial", r1.status
assert r1.rows_new == 1, f"expected 1 new version, got {r1.rows_new}"

r2 = run_scrape(cfg, store, scraper=fake, pause=0)
print("run2 status:", r2.status, "| returned:", r2.rows_returned, "| new:", r2.rows_new)
assert r2.rows_new == 0, f"re-scrape must add no new versions, got {r2.rows_new}"

# content change -> new version, same posting
ROW["min_amount"] = 90000.0
r3 = run_scrape(cfg, store, scraper=fake, pause=0)
print("run3 (salary added) new:", r3.rows_new)
assert r3.rows_new == 1

s = store.stats()
print("stats:", {k: v for k, v in s.items() if k != "by_board"})
assert s["distinct_postings"] == 1, s
assert s["raw_versions"] == 2, s
assert s["sightings"] == 9, s   # 3 linkedin legs x 3 runs

errs = conn.execute("SELECT COUNT(*) FROM scrape_tasks WHERE status='error'").fetchone()[0]
print("recorded error legs:", errs)
assert errs == 9

print("\nALL CHECKS PASSED")
