#!/usr/bin/env python
"""Run every smoke suite. `uv run python tests/run_all.py`

These are scripts rather than pytest cases on purpose: each one drives a real
code path end to end against a copy of the real database, which is what has
actually caught the bugs in this project — enum serialization, redundant
re-fetching, silently-uncacheable prompts. None of those would have shown up
against fixtures.
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SUITES = ["smoke.py", "smoke_backfill.py", "smoke_day2.py", "smoke_review.py", "smoke_web.py"]

failed = []
for name in SUITES:
    proc = subprocess.run([sys.executable, str(ROOT / name)], capture_output=True, text=True)
    last = (proc.stdout.strip().splitlines() or ["(no output)"])[-1]
    ok = proc.returncode == 0
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<22} {last}")
    if not ok:
        failed.append((name, proc.stdout, proc.stderr))

for name, out, err in failed:
    print(f"\n─── {name} ───\n{out}\n{err}")

print(f"\n{len(SUITES) - len(failed)}/{len(SUITES)} suites passed")
sys.exit(1 if failed else 0)
