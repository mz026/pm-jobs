---
name: pm-jobs-setup
description: Install and verify everything pm-jobs needs to run — uv, Python 3.12+, project dependencies, and the config files. Use on a fresh machine, after cloning the repo, or when a pm-jobs command fails with a missing module, a missing interpreter, or "command not found".
disable-model-invocation: true
allowed-tools: Bash(uv:*), Bash(uv run pm-jobs:*), Bash(command -v:*), Bash(python3 --version), Read
---

# pm-jobs setup

Get the project runnable, then prove it. Work from the repo root and stop at
the first step that fails — later steps depend on earlier ones.

Report what you actually ran and what it said. If something was already
installed, say so rather than pretending you installed it.

## 1. uv

```bash
command -v uv && uv --version
```

If it's missing, install it and tell the user which method you used:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # macOS/Linux, no admin rights
# or, if they already use Homebrew:  brew install uv
```

The installer puts `uv` in `~/.local/bin`. If `command -v uv` still fails
afterwards, that directory isn't on `PATH` — tell the user the line to add to
their shell profile rather than editing it yourself:

```
export PATH="$HOME/.local/bin:$PATH"
```

## 2. Python

The project needs **3.12 or newer** (`requires-python = ">=3.12"`, and
`.python-version` pins 3.12).

You don't need to install Python separately — `uv sync` fetches a matching
interpreter if the system doesn't have one. Only investigate the Python version
if step 3 fails.

## 3. Dependencies

```bash
uv sync
```

This creates `.venv` and installs `python-jobspy`, `pyyaml`, `anthropic`, and
`flask`, plus the `pm-jobs` command itself.

**`python-jobspy` is pinned to an exact version on purpose.** If you hit a
resolution error, do not relax that pin — `pm_jobs/linkedin_details.py` depends
on a private jobspy API that can change in any release. Report the error
instead.

## 4. Config files

Both must exist at the repo root:

```bash
uv run pm-jobs searches
```

That loads `searches.yaml` and prints what's configured, so it validates the
file rather than just checking it exists. If it's missing or malformed, the
error names the problem.

Then confirm `preferences.yaml` parses — the same command family reads it:

```bash
uv run pm-jobs review --dry-run
```

This runs the deterministic filters over whatever is already stored and makes
no model calls, so it's free and safe on a fresh checkout (on an empty database
it simply reports nothing pending).

## 5. Verify

```bash
uv run pm-jobs stats
```

On a fresh checkout this prints zeroes and creates `pm_jobs.db`. That's a pass
— it means the schema built and the CLI works.

Optionally, confirm the whole suite runs:

```bash
uv run python tests/run_all.py
```

The suites copy the real database, so they need one to exist; on a completely
fresh checkout they may have nothing to work with. That's not a failure of
setup — say so and move on.

## 6. Report

Tell the user, briefly:

- what you installed versus what was already there
- that they're ready, and the two commands they'll use: `/pm-jobs-daily` then
  `uv run pm-jobs web`

## Notes

- **No API key is needed** for the normal flow — `/pm-jobs-daily` does the
  reading in the Claude Code session. Mention `ANTHROPIC_API_KEY` only if the
  user asks about running `uv run pm-jobs daily` unattended.
- Don't create or edit `searches.yaml` or `preferences.yaml` on the user's
  behalf beyond what the repo ships. Those encode their criteria; if one is
  missing, say what it should contain and let them fill it in.
