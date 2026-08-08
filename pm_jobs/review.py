"""Read each surviving posting and decide whether it's worth showing you.

The model answers questions of fact about the posting — is this a product role,
which languages does it actually demand, what is it about. The *policy* — which
answers mean "drop" — stays in Python below, so a verdict is always consistent
with the fields it came from, and changing the policy doesn't mean re-reading
every job.

Nothing here deletes anything. A drop is a row in `job_reviews`, so a wrong one
is a query away from being found and re-reviewing costs nothing.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .prefilter import DROP_DUTCH, DROP_NOT_PRODUCT, prefilter
from .preferences import Preferences
from .reviewstore import ReviewStore

DROP_LANGUAGE = "language_required"

MAX_TOKENS = 4000
PAUSE_BETWEEN_CALLS_SEC = 0.0   # the API is not the thing that rate-limits us

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_product_role": {
            "type": "boolean",
            "description": "True if this is a product management role (product manager, "
                           "product owner, head/director/VP of product, CPO). False for "
                           "product design, product engineering, program management, "
                           "product marketing, or product compliance roles.",
        },
        "languages_required": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Languages the posting actually requires fluency in. Empty if "
                           "the posting states no language requirement at all.",
        },
        "language_evidence": {
            "type": "string",
            "description": "The phrase from the posting that establishes the language "
                           "requirement, or an empty string if none was stated.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Which of the offered tags apply. Empty array if none do.",
        },
        "summary": {
            "type": "string",
            "description": "What this job is and who it's for.",
        },
    },
    "required": ["is_product_role", "languages_required", "language_evidence", "tags", "summary"],
    "additionalProperties": False,
}


def build_system_prompt(prefs: Preferences) -> str:
    """The cached prefix. Identical for every job in a run — keep it that way.

    Anything job-specific belongs in the user turn; a job id or timestamp in
    here would change the prefix bytes and the cache would never hit.
    """
    spoken = ", ".join(prefs.speak)
    tag_lines = "\n".join(f"- `{name}`: {desc}" for name, desc in prefs.tags.items())

    return f"""\
You are screening job postings for one product manager. For each posting you \
are given, answer five questions about it. Answer only from what the posting \
says — do not infer requirements it doesn't state.

## 1. Is this a product management role?

Yes for: product manager, senior/principal/group/technical product manager, \
product owner, product lead, head of product, director of product, VP of \
product, chief product officer.

No for: product designer, product engineer, product developer, product \
marketing manager, product compliance or specialist roles, program manager, \
project manager, engineering manager, delivery manager. These often have \
"product" in the title without being product management jobs.

Job titles are unreliable — they are translated, abbreviated, and misspelled. \
Read the responsibilities in the description when the title is unclear. A \
posting titled "Senior Product Ownwer" is a product owner role.

## 2. Which languages does it actually require?

The reader speaks {spoken} and nothing else. This determines whether they can \
apply, so the distinction between a requirement and a preference matters.

List a language only when the posting requires fluency or professional \
proficiency in it. Do not list a language when:

- it is described as "a plus", "nice to have", "an advantage", "a pré", or \
"preferred but not required"
- the posting offers to teach it (language courses listed as a benefit)
- the phrase is about the right to work, not language ("Dutch/EU working \
rights", "EU work permit")
- the posting mentions a country's regulations, market, or domain as subject \
matter ("expertise in Dutch pension regulation", "the German market")
- a job description written in that language never states a language \
requirement — the language it is written in is not itself a requirement

If a posting requires one language from a set ("fluent in English or Dutch"), \
list only the one the reader speaks, if any.

These are real phrases from postings in this market, and how each should be \
read. They are illustrative, not a lookup table — the same distinctions show \
up in other wordings.

| Phrase in the posting | languages_required |
|---|---|
| "Vloeiend in Nederlands en Engels, zowel mondeling als schriftelijk" | English, Dutch — fluency in both is demanded |
| "Fluent in English; Dutch or German proficiency is a plus" | English only — the others are optional |
| "You are based in the Netherlands and you have full Dutch/EU working rights" | none — this is about the right to work |
| "Access to the Leaseweb Academy, offering a variety of studies, (Dutch) courses, and trainings" | none — a benefit, not a requirement |
| "Demonstrate strong expertise in Dutch pension regulation" | none — domain knowledge, not language |
| "Je spreekt Nederlands, of wilt dat snel leren" | Dutch — required, though they will accept a learner |
| "Professionele beheersing van de Nederlandse taal maar je hoeft geen native speaker te zijn" | Dutch — professional proficiency is required |

Quote the phrase you based this on in `language_evidence`, so a wrong call can \
be spotted. Empty string if the posting states no language requirement.

## 3–4. Which tags apply?

Use only these, and only when the posting genuinely fits. An empty array is a \
normal answer — most jobs match nothing, and a tag applied loosely is worse \
than no tag because it stops meaning anything.

{tag_lines}

## 5. Summarize it

{prefs.summary_sentences} sentences or fewer, for someone deciding in about \
ten seconds whether to open the posting. Lead with what the product is and who \
uses it. Include company stage or domain when the posting says. Skip the \
benefits, the culture boilerplate, and the application process — those are the \
same everywhere and tell the reader nothing.

Write plainly, in complete sentences, and do not repeat the job title.\
"""


def build_user_message(job: sqlite3.Row, description: str | None) -> str:
    parts = [
        f"Title: {job['title'] or '(none)'}",
        f"Company: {job['company'] or '(unknown)'}",
        f"Location: {job['location'] or '(unstated)'}",
        f"Posted: {job['date_posted'] or '(undated)'}",
        "",
        "Description:",
        (description or "(the board returned no description)").strip(),
    ]
    return "\n".join(parts)


@dataclass
class ReviewResult:
    considered: int = 0
    prefiltered: int = 0
    judged: int = 0
    kept: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    aborted: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def status(self) -> str:
        if self.aborted:
            return "error"
        return "partial" if self.failed else "ok"

    def cost_estimate(self, model: str) -> float:
        """Rough dollars, from the run's own token counts.

        Rates are per million tokens. Cached reads bill at a tenth of input.
        """
        rates = {"claude-sonnet-5": (3.0, 15.0), "claude-opus-5": (5.0, 25.0),
                 "claude-haiku-4-5": (1.0, 5.0)}
        inp, out = rates.get(model, (3.0, 15.0))
        return (self.input_tokens * inp + self.cached_tokens * inp * 0.1
                + self.output_tokens * out) / 1_000_000

    def summary(self) -> str:
        if self.aborted:
            return f"review aborted: {self.aborted}"
        if not self.considered:
            return "review: nothing new to review"
        return (f"review: {self.considered} considered, {self.prefiltered} dropped by prefilter, "
                f"{self.judged} judged, {self.kept} kept, {self.failed} failed")


def is_fatal(exc: BaseException) -> bool:
    """Would every remaining job fail this way too?

    A bad key, a revoked key, or a model id that doesn't exist will fail
    identically on all 48 postings. Grinding through them writes 48 useless
    error rows and delays the one message that actually helps. Rate limits and
    malformed responses are the opposite — per job, and worth continuing past.
    """
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return False

    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                        anthropic.NotFoundError)):
        return True
    # The SDK raises a bare TypeError when it cannot resolve any credential,
    # before a request is ever built.
    return isinstance(exc, TypeError) and "authentication" in str(exc).lower()


def _default_client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "the anthropic package is not installed — run `uv sync`"
        ) from exc
    return anthropic.Anthropic()


def judge_one(client, prefs: Preferences, system: str, user_text: str) -> tuple[dict[str, Any], Any]:
    """One model call. Returns (parsed result, usage)."""
    response = client.messages.create(
        model=prefs.model,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        thinking={"type": "adaptive"},
        output_config={"effort": prefs.effort,
                       "format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
        messages=[{"role": "user", "content": user_text}],
    )

    # A safety decline returns HTTP 200 with an empty or partial content list,
    # so stop_reason has to be checked before indexing into content.
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined to review this posting")
    if response.stop_reason == "max_tokens":
        raise RuntimeError(f"response hit max_tokens ({MAX_TOKENS}) before completing")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise RuntimeError(f"no text block in response (stop_reason={response.stop_reason})")
    return json.loads(text), response.usage


def decide(prefs: Preferences, result: dict[str, Any]) -> tuple[str, str | None]:
    """Turn the model's answers into a verdict. Policy lives here, not in the prompt."""
    if not result.get("is_product_role"):
        return "drop", DROP_NOT_PRODUCT

    spoken = {s.strip().lower() for s in prefs.speak}
    required = [str(x) for x in result.get("languages_required") or []]
    unspoken = [lang for lang in required if lang.strip().lower() not in spoken]
    if unspoken:
        return "drop", f"{DROP_LANGUAGE}: {', '.join(unspoken)}"
    return "keep", None


AGENT_MODEL = "agent"   # recorded on verdicts the invoking agent produced


def prefilter_pass(store: ReviewStore, prefs: Preferences, run_id: int,
                   pending: list[sqlite3.Row]) -> tuple[list[tuple[sqlite3.Row, str | None, bool]], int]:
    """Record the deterministic drops; return what still needs judging."""
    to_judge: list[tuple[sqlite3.Row, str | None, bool]] = []
    dropped = 0
    for job in pending:
        description = json.loads(job["payload"]).get("description")
        pre = prefilter(job["title"], description, prefs)
        if not pre.keep:
            store.record(run_id, job, stage="prefilter", verdict="drop", drop_reason=pre.reason)
            dropped += 1
        else:
            to_judge.append((job, description, pre.role_certain))
    store.conn.commit()
    return to_judge, dropped


def export_batch(store: ReviewStore, prefs: Preferences, limit: int | None = None) -> dict[str, Any]:
    """Everything an agent needs to judge a batch, in one object.

    The instructions and schema travel with the work rather than being restated
    in the skill. There is one definition of how a posting gets judged, and both
    backends read it from here — otherwise the two would drift apart the first
    time either was tuned, and verdicts would stop being comparable.
    """
    pending = store.pending(limit=limit)
    run_id = store.start_run(AGENT_MODEL, prefs.hash)
    to_judge, dropped = prefilter_pass(store, prefs, run_id, pending)

    return {
        "run_id": run_id,
        "prefs_hash": prefs.hash,
        "prefiltered": dropped,
        "instructions": build_system_prompt(prefs),
        "schema": RESULT_SCHEMA,
        "jobs": [
            {
                "raw_job_id": job["raw_job_id"],
                "board": job["board"],
                "board_job_id": job["board_job_id"],
                "title": job["title"],
                "company": job["company"],
                "location": job["location"],
                "date_posted": job["date_posted"],
                # False means the title mentions product but matched no
                # configured role phrase — the model has to settle the role.
                "role_certain": role_certain,
                "description": description or "",
            }
            for job, description, role_certain in to_judge
        ],
    }


class ApplyError(Exception):
    """The verdict file doesn't match the run it claims to belong to."""


def apply_verdicts(store: ReviewStore, prefs: Preferences, payload: dict[str, Any]) -> ReviewResult:
    """Validate agent-produced verdicts and store them.

    This is the seam that keeps the agent backend as trustworthy as the API
    one. An agent writing SQLite directly could corrupt state quietly; here a
    malformed verdict is rejected with a message, and the same `decide()` turns
    answers into a verdict, so both backends can't drift on policy.
    """
    run_id = payload.get("run_id")
    if not isinstance(run_id, int):
        raise ApplyError("missing or non-integer 'run_id'")

    run = store.conn.execute("SELECT * FROM review_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ApplyError(f"no review run {run_id} — did the export happen?")
    if run["prefs_hash"] != prefs.hash:
        raise ApplyError(
            f"preferences changed since run {run_id} was exported "
            f"({run['prefs_hash']} -> {prefs.hash}). Re-export rather than applying stale verdicts."
        )

    verdicts = payload.get("verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        raise ApplyError("'verdicts' must be a non-empty list")

    # Only postings with no verdict yet are applicable. This makes apply
    # idempotent and blocks a stale file from overwriting a later judgment.
    eligible = {row["raw_job_id"]: row for row in store.pending()}
    known_tags = set(prefs.tag_names)
    result = ReviewResult(considered=len(verdicts))

    for entry in verdicts:
        raw_job_id = entry.get("raw_job_id")
        job = eligible.get(raw_job_id)
        if job is None:
            raise ApplyError(
                f"raw_job_id {raw_job_id!r} is not awaiting judgement in this run "
                "(already judged, or never exported)"
            )
        for field_name in ("is_product_role", "languages_required", "tags", "summary"):
            if field_name not in entry:
                raise ApplyError(f"raw_job_id {raw_job_id}: missing {field_name!r}")

        unknown = [t for t in entry["tags"] if t not in known_tags]
        if unknown:
            raise ApplyError(
                f"raw_job_id {raw_job_id}: unknown tag(s) {unknown}; "
                f"expected any of {sorted(known_tags)}"
            )

        verdict, reason = decide(prefs, entry)
        result.judged += 1
        if verdict == "keep":
            result.kept += 1
        store.record(
            run_id, job, stage="judged", verdict=verdict, drop_reason=reason,
            languages=entry.get("languages_required") or [], tags=entry["tags"],
            summary=entry.get("summary"), model=AGENT_MODEL,
        )

    store.conn.commit()
    remaining = len(store.pending())
    store.finish_run(run_id, "ok" if not remaining else "partial",
                     judged=(run["judged"] or 0) + result.judged,
                     kept=(run["kept"] or 0) + result.kept)
    return result


def run_review(
    store: ReviewStore,
    prefs: Preferences,
    limit: int | None = None,
    client: Any = None,
    on_progress: Callable[[str], None] = lambda msg: None,
    pause: float = PAUSE_BETWEEN_CALLS_SEC,
) -> ReviewResult:
    pending = store.pending(limit=limit)
    result = ReviewResult(considered=len(pending))
    if not pending:
        return result

    run_id = store.start_run(prefs.model, prefs.hash)
    system = build_system_prompt(prefs)
    known_tags = set(prefs.tag_names)

    # Deterministic drops first — they cost nothing and remove most of the list
    # before the client is even constructed.
    to_judge, result.prefiltered = prefilter_pass(store, prefs, run_id, pending)
    on_progress(f"prefilter: {result.prefiltered} dropped, {len(to_judge)} to judge")

    if to_judge and client is None:
        try:
            client = _default_client()
        except Exception as exc:
            result.aborted = str(exc)
            store.finish_run(run_id, "error", considered=result.considered,
                             prefiltered=result.prefiltered)
            return result

    for index, (job, description, role_certain) in enumerate(to_judge, start=1):
        label = f"[{index}/{len(to_judge)}] {job['company']} · {job['title']}"
        try:
            parsed, usage = judge_one(client, prefs, system, build_user_message(job, description))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if is_fatal(exc):
                result.aborted = error
                on_progress(f"{label} → aborting: {error}")
                break
            result.failed += 1
            result.errors.append(f"{job['board_job_id']}: {error}")
            store.record(run_id, job, stage="error", verdict="keep",
                         drop_reason=None, model=prefs.model, error=error)
            store.conn.commit()
            # A failed review keeps the job rather than dropping it: an outage
            # should not quietly shrink the list you review.
            on_progress(f"{label} → failed (kept): {error}")
        else:
            result.judged += 1
            result.input_tokens += getattr(usage, "input_tokens", 0) or 0
            result.output_tokens += getattr(usage, "output_tokens", 0) or 0
            result.cached_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

            # The system prompt is only cacheable above the model's minimum
            # prefix length, and falling under it fails silently — no error,
            # just full price on every call. Check the second call, by which
            # point the first should have written the entry.
            if index == 2 and not result.cached_tokens:
                on_progress("  note: prompt cache not hitting — the system prompt is likely "
                            "below this model's minimum cacheable length")

            verdict, reason = decide(prefs, parsed)
            tags = [t for t in parsed.get("tags") or [] if t in known_tags]
            if verdict == "keep":
                result.kept += 1
            store.record(
                run_id, job, stage="judged", verdict=verdict, drop_reason=reason,
                languages=parsed.get("languages_required") or [],
                tags=tags, summary=parsed.get("summary"), model=prefs.model,
            )
            store.conn.commit()
            mark = "keep" if verdict == "keep" else f"drop ({reason})"
            on_progress(f"{label} → {mark}{' [' + ', '.join(tags) + ']' if tags else ''}")

        if pause and index < len(to_judge):
            time.sleep(pause)

    store.finish_run(run_id, result.status, considered=result.considered,
                     prefiltered=result.prefiltered, judged=result.judged, kept=result.kept)
    return result
