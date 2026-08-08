"""Synthetic check of backfill semantics without hitting LinkedIn."""
import sys, tempfile, os, json
import os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from pm_jobs.store import connect, RawStore, MAX_BACKFILL_ATTEMPTS
from pm_jobs.backfill import run_backfill
from pm_jobs.linkedin_details import DetailFetchUnavailable, strip_board_prefix

db = os.path.join(tempfile.mkdtemp(), "t.db")
conn = connect(db); store = RawStore(conn)

run_id = store.start_run("x.yaml", ["s"])
task_id = store.start_task(run_id, "s", "pm", "linkedin")
for i in (1, 2):
    store.record_job(run_id, task_id, "s", "pm", "linkedin",
                     {"id": f"li-{i}", "title": f"PM {i}", "company": "Acme",
                      "job_url": f"https://x/{i}", "description": None})
conn.commit()

assert len(store.jobs_needing_details()) == 2

# job 1 fetches fine, job 2 always errors
from jobspy.model import JobType
def fetcher(jid):
    if jid == "li-2":
        raise RuntimeError("simulated 429")
    # real _get_job_details returns enum members, not strings
    return {"description": "Full text " * 50, "job_level": "mid", "job_type": [JobType.FULL_TIME]}

r = run_backfill(store, pause=0, fetcher=fetcher)
print("run1:", r.summary(), "| status:", r.status)
assert (r.considered, r.filled, r.failed) == (2, 1, 1), r

pending = store.jobs_needing_details()
print("still pending after run1:", [p["board_job_id"] for p in pending])
assert [p["board_job_id"] for p in pending] == ["li-2"], "filled job must drop out"

# enrichment stored as a NEW version, original preserved, no sighting added
vers = conn.execute("SELECT COUNT(*) FROM raw_jobs WHERE board_job_id='li-1'").fetchone()[0]
sights = conn.execute("SELECT COUNT(*) FROM job_sightings WHERE board_job_id='li-1'").fetchone()[0]
print("li-1 versions:", vers, "| sightings:", sights)
assert vers == 2, "backfill must append a version, not edit"
assert sights == 1, "backfill must not fabricate a sighting"

latest = conn.execute("SELECT payload FROM raw_jobs WHERE board_job_id='li-1' ORDER BY id DESC LIMIT 1").fetchone()[0]
p = json.loads(latest)
assert p["description"] and p["job_level"] == "mid" and p["title"] == "PM 1", p
# enum must serialize the same way the search path stores it: joined string
assert p["job_type"] == "fulltime", p["job_type"]

# repeated failures eventually give up
for _ in range(MAX_BACKFILL_ATTEMPTS):
    run_backfill(store, pause=0, fetcher=fetcher)
print("pending after repeated failures:", len(store.jobs_needing_details()))
assert store.jobs_needing_details() == [], "must stop retrying a dead posting"
assert len(store.jobs_needing_details(retry_failed=True)) == 1, "--retry-failed must resurface it"

# private-API break aborts instead of grinding through every job
def broken(jid):
    raise DetailFetchUnavailable("jobspy moved")
r = run_backfill(store, pause=0, retry_failed=True, fetcher=broken)
print("aborted:", r.aborted, "| status:", r.status)
assert r.aborted and r.failed == 0

assert strip_board_prefix("li-4388353369") == "4388353369"
assert store.stats()["backfill_failed"] == 1

print("\nALL CHECKS PASSED")
