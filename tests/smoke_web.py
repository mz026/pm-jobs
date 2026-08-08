"""Drive the web UI end to end against a synthetically-reviewed copy of the DB."""
import sys, json, shutil, tempfile, os, re
import os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from pm_jobs.store import connect
from pm_jobs.reviewstore import ReviewStore
from pm_jobs.preferences import load_preferences
from pm_jobs.review import run_review
from pm_jobs.web import create_app

db = os.path.join(tempfile.mkdtemp(), "t.db")
shutil.copy(str(ROOT / "pm_jobs.db"), db)
prefs = load_preferences(str(ROOT / "preferences.yaml"))
store = ReviewStore(connect(db))
store.clear_reviews()

TAGS = [["consumer"], ["ed-tech", "gamification"], [], ["consumer", "gamification"]]
n = [0]
class R:
    stop_reason = "end_turn"
    class U: input_tokens = output_tokens = cache_read_input_tokens = 0
    usage = U()
    @property
    def content(self):
        n[0] += 1
        return [type("B", (), {"type": "text", "text": json.dumps({
            "is_product_role": True, "is_software_product": True,
            "product_managed": "a web platform", "languages_required": ["English"],
            "language_evidence": "Fluent in English",
            "tags": TAGS[n[0] % len(TAGS)],
            "summary": f"Synthetic summary number {n[0]} describing the product and its users.",
        })})()]
fake = type("C", (), {"messages": type("M", (), {"create": staticmethod(lambda **k: R())})()})()
run_review(store, prefs, client=fake, pause=0)
print("counts after review:", store.counts())

app = create_app(db)
app.config.update(TESTING=True)
c = app.test_client()

# / redirects to unread
r = c.get("/")
assert r.status_code == 302 and r.headers["Location"].endswith("/unread"), r.headers

# all three views render
for view in ("unread", "favorite", "read"):
    r = c.get(f"/{view}")
    assert r.status_code == 200, (view, r.status_code)
    print(f"  GET /{view:<9} {r.status_code}  {len(r.data)} bytes")

assert c.get("/nonsense").status_code == 404

html = c.get("/unread").get_data(as_text=True)
rows = html.count('<article class="job">')
print(f"\nunread page renders {rows} job cards")
assert rows == len(store.list_jobs("unread")), rows
assert "consumer" in html and "ed-tech" in html, "tags render"
assert "also on" in html, "folded duplicates show their second source"

# --- click through marks read and redirects to the real board URL ---
job = store.list_jobs("unread")[0]
before = store.counts()
r = c.get(f"/job/{job['board']}/{job['board_job_id']}/open")
assert r.status_code == 302, r.status_code
assert r.headers["Location"].startswith("http"), r.headers["Location"]
after = store.counts()
print(f"\nopened one job: {before} -> {after}")
assert after["read"] == before["read"] + 1
assert after["unread"] == before["unread"] - 1

# a folded duplicate must not reappear in unread
assert not any(j["board_job_id"] == job["board_job_id"] for j in store.list_jobs("unread"))

# --- favorite toggles, and survives being read ---
fav = store.list_jobs("unread")[0]
r = c.post(f"/job/{fav['board']}/{fav['board_job_id']}/favorite", data={"from": "unread"})
assert r.status_code == 302
assert store.counts()["favorite"] == 1, store.counts()
r = c.post(f"/job/{fav['board']}/{fav['board_job_id']}/favorite", data={"from": "unread"})
assert store.counts()["favorite"] == 0, "toggles back off"

# --- mark unread undoes a read ---
read_job = store.list_jobs("read")[0]
c.post(f"/job/{read_job['board']}/{read_job['board_job_id']}/unread", data={"from": "read"})
assert store.counts()["read"] == 0, store.counts()

# --- a redirect target that doesn't exist 404s rather than crashing ---
assert c.get("/job/linkedin/does-not-exist/open").status_code == 404

print("\nfinal counts:", store.counts())
print("ALL CHECKS PASSED")
