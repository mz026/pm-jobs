"""Exercise the export → judge → apply path the daily skill drives."""
import os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import json, shutil, tempfile

from pm_jobs.store import connect
from pm_jobs.reviewstore import ReviewStore
from pm_jobs.preferences import load_preferences
from pm_jobs.review import ApplyError, apply_verdicts, export_batch

db = os.path.join(tempfile.mkdtemp(), "t.db")
shutil.copy(str(ROOT / "pm_jobs.db"), db)
prefs = load_preferences(str(ROOT / "preferences.yaml"))
store = ReviewStore(connect(db))
store.clear_reviews()
TOTAL = len(store.pending())

batch = export_batch(store, prefs, limit=10)
print(f"exported run {batch['run_id']}: {len(batch['jobs'])} to judge, "
      f"{batch['prefiltered']} prefiltered (of {TOTAL} pending)")

# The export must be self-contained: the skill judges from it, not from memory.
assert batch["instructions"] and len(batch["instructions"]) > 3000, "instructions travel with the work"
assert set(batch["schema"]["properties"]) == {
    "is_product_role", "languages_required", "language_evidence", "tags", "summary"}
assert all("description" in j and "role_certain" in j for j in batch["jobs"])
assert "Vloeiend in Nederlands" in batch["instructions"], "worked language examples included"

def verdicts_for(b, **over):
    """Verdicts for every job in batch `b`, carrying that batch's own run_id."""
    return {"run_id": b["run_id"], "verdicts": [
        {"raw_job_id": j["raw_job_id"], "is_product_role": True,
         "languages_required": ["English"], "language_evidence": "Fluent in English",
         "tags": ["consumer"], "summary": "A product role.", **over} for j in b["jobs"]]}


def export_judgeable(limit=4):
    """Export until a batch actually contains something to judge.

    Consecutive pending postings are often all prefilter drops, so a small
    batch can legitimately come back with an empty job list.
    """
    for _ in range(20):
        b = export_batch(store, prefs, limit=limit)
        if b["jobs"]:
            return b
    raise AssertionError("no judgeable postings left")

# --- every rejection path ---------------------------------------------------
def rejects(payload, expect):
    try:
        apply_verdicts(store, prefs, payload)
    except ApplyError as exc:
        assert expect in str(exc), f"expected {expect!r} in {exc!r}"
        print(f"  rejected: {str(exc)[:76]}")
        return
    raise AssertionError(f"should have rejected: {expect}")

rejects({"verdicts": []}, "run_id")
rejects({"run_id": 99999, "verdicts": [{"raw_job_id": 1}]}, "no review run")
rejects({"run_id": batch["run_id"], "verdicts": []}, "non-empty list")
rejects(verdicts_for({"run_id": batch["run_id"], "jobs": batch["jobs"][:1]}, tags=["made-up"]), "unknown tag")
rejects({"run_id": batch["run_id"], "verdicts": [{"raw_job_id": 10**9, "is_product_role": True,
         "languages_required": [], "tags": [], "summary": "x"}]}, "not awaiting judgement")
bad = verdicts_for({"run_id": batch["run_id"], "jobs": batch["jobs"][:1]})
del bad["verdicts"][0]["languages_required"]
rejects(bad, "missing 'languages_required'")

# A rejection must store nothing at all — partial application would be worse
# than none, because the run would look done when it wasn't.
assert len(store.pending()) == TOTAL - batch["prefiltered"], "rejections stored nothing"

# --- policy is shared with the API backend ----------------------------------
r = apply_verdicts(store, prefs, verdicts_for(batch))
print(f"\napplied {r.judged}: {r.kept} kept, {r.judged - r.kept} dropped")
assert r.judged == len(batch["jobs"])

# an unspoken language drops, exactly as the API path would
b2 = export_judgeable()
r2 = apply_verdicts(store, prefs, verdicts_for(b2, languages_required=["English", "German"]))
assert r2.kept == 0, "German is not a language the owner speaks"
reasons = {row["reason"] for row in store.drop_reasons()}
assert any(x.startswith("language_required") for x in reasons), reasons
print("drop reasons now:", sorted(reasons))

# Mandarin is spoken, so it must NOT drop
b3 = export_judgeable()
r3 = apply_verdicts(store, prefs, verdicts_for(b3, languages_required=["Mandarin"]))
assert r3.kept == len(b3["jobs"]), "Mandarin is spoken"

# --- re-applying a stale file is refused ------------------------------------
rejects(verdicts_for(batch), "not awaiting judgement")

# --- a preferences change invalidates an outstanding export -----------------
b4 = export_judgeable()
moved = load_preferences(str(ROOT / "preferences.yaml"))
object.__setattr__(moved, "_hash", "different-hash")
try:
    apply_verdicts(store, moved, verdicts_for(b4))
    raise AssertionError("should refuse verdicts judged under different preferences")
except ApplyError as exc:
    assert "preferences changed" in str(exc), exc
    print(f"  rejected: {str(exc)[:76]}")

print("\nALL CHECKS PASSED")
