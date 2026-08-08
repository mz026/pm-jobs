"""Exercise the review pass against the real corpus with a fake model client."""
import sys, json, shutil, tempfile, os
import os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from pm_jobs.store import connect
from pm_jobs.reviewstore import ReviewStore
from pm_jobs.preferences import load_preferences
from pm_jobs.review import run_review, build_system_prompt, decide

# Work on a copy so the real DB isn't touched
db = os.path.join(tempfile.mkdtemp(), "t.db")
shutil.copy(str(ROOT / "pm_jobs.db"), db)
prefs = load_preferences(str(ROOT / "preferences.yaml"))
store = ReviewStore(connect(db))
store.clear_reviews()          # start from a known state whatever the live DB holds
TOTAL = len(store.pending())

sysprompt = build_system_prompt(prefs)
print(f"system prompt: {len(sysprompt)} chars ~ {len(sysprompt)//4} tokens "
      f"({'clears' if len(sysprompt)//4 >= 1024 else 'BELOW'} Sonnet 5's 1024-token cache minimum)")

class Usage:
    input_tokens, output_tokens, cache_read_input_tokens = 1500, 300, 1100

class FakeClient:
    calls = 0
    def __init__(self, fail_on=None): self.fail_on = fail_on or set()
    class messages:
        pass
    def _create(self, **kw):
        FakeClient.calls += 1
        body = kw["messages"][0]["content"]
        title = body.split("\n")[0]
        if FakeClient.calls in self.fail_on:
            raise RuntimeError("simulated 429")
        is_pm = not any(w in title.lower() for w in ("designer", "engineer", "compliance", "program manager"))
        langs = ["English", "German"] if "Drives" in title else ["English"]
        payload = {"is_product_role": is_pm, "languages_required": langs,
                   "language_evidence": "Fluent in English", "tags": ["consumer"],
                   "summary": "A product role at a company."}
        class R:
            stop_reason = "end_turn"
            content = [type("B", (), {"type": "text", "text": json.dumps(payload)})()]
            usage = Usage()
        return R()

fake = FakeClient(fail_on={3})
fake.messages = type("M", (), {"create": staticmethod(lambda **kw: fake._create(**kw))})()

r = run_review(store, prefs, client=fake, pause=0)
print("\n" + r.summary())
print(f"model calls: {FakeClient.calls}")
assert r.considered == TOTAL, (r.considered, TOTAL)
assert r.prefiltered + r.judged + r.failed == TOTAL, r
assert r.failed == 1, r.failed
assert FakeClient.calls == r.judged + r.failed, "one call per job that got past prefilter"

# A failed review must KEEP the job, not silently drop it
kept_after_fail = store.conn.execute(
    "SELECT COUNT(*) FROM job_reviews WHERE stage='error' AND verdict='keep'").fetchone()[0]
assert kept_after_fail == 1, kept_after_fail

print("\ndrop reasons:")
for row in store.drop_reasons():
    print(f"  {row['reason']:<40} {row['n']}")

print("\nviews:", store.counts())

# idempotency: nothing left pending, a second pass is a no-op
r2 = run_review(store, prefs, client=fake, pause=0)
print("second pass:", r2.summary())
assert r2.considered == 0

# policy is decided in Python, not the prompt
assert decide(prefs, {"is_product_role": False, "languages_required": []})[0] == "drop"
assert decide(prefs, {"is_product_role": True, "languages_required": ["English", "German"]})[1].startswith("language_required")
assert decide(prefs, {"is_product_role": True, "languages_required": ["Mandarin"]})[0] == "keep"
assert decide(prefs, {"is_product_role": True, "languages_required": []})[0] == "keep"

# state survives a full re-review — the entire reason the tables are split
first = store.list_jobs("unread")[0]
store.mark_read(first["board"], first["board_job_id"])
before = store.counts()
assert before["read"] == 1, before
store.clear_reviews()
run_review(store, prefs, client=fake, pause=0)
after = store.counts()
print(f"\nafter clear_reviews + re-review: {before} -> {after}")
assert after["read"] == before["read"] == 1, (before, after)

print("\nALL CHECKS PASSED")
