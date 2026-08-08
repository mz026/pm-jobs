"""The two drops that need no judgment, and therefore get none.

A model call is the right tool for "does this posting require a language I
don't speak" — that question has real ambiguity in it. It is the wrong tool for
"is this description written in Dutch" and "is this a product role": both are
decidable from the text, cheaply and without a model that can be wrong in ways
you won't notice.

Running these first also removes roughly two thirds of each scrape before a
single token is spent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .preferences import Preferences

DROP_DUTCH = "dutch_description"
DROP_NOT_PRODUCT = "not_product_role"

# Function words, not vocabulary: they are frequent, short, and don't survive
# translation, which makes them a far better language signal than topic words.
DUTCH_MARKERS = frozenset("""
    het een van met voor wij jij onze functie werkzaamheden ervaring zoeken je
    en de bij niet ook naar door over uit dat als zijn wordt worden heeft hebben
    binnen waarbij onder tussen jouw ons deze veel meer waar
""".split())

ENGLISH_MARKERS = frozenset("""
    the and you our we for with experience role team to of a in on at is are
    will be have has this that as from about across within their your more
""".split())

# Below this, a "description" is a stub or a scrape artifact and the word
# ratios are noise. Treat it as undecidable rather than guessing.
MIN_WORDS_FOR_LANGUAGE = 30

WORD_RE = re.compile(r"[a-zA-ZàáäéèëïíóöüúçñÀÉËÏ]+")

# A title with "product" in it that matched no configured role phrase is
# ambiguous, not a drop. Checked against the real corpus, a strict word list
# silently discarded 'Senior productmanager', 'Director Technical Product
# Management', 'Chief Product & Technology Officer', and a posting titled
# 'Senior Product Ownwer' — while correctly rejecting Product Designer, Product
# Engineer, and Product Compliance Specialist. Nothing decidable separates
# those two groups by title alone, so the ambiguous ones go to the model.
MAYBE_PRODUCT_RE = re.compile(r"\bproducts?\b|\bpm\b", re.IGNORECASE)


@dataclass(frozen=True)
class PrefilterResult:
    keep: bool
    reason: str | None = None
    language: str | None = None      # english | dutch | unknown
    role_certain: bool = True        # False -> the model must confirm the role


def detect_language(text: str | None) -> str:
    """Crude two-way language guess, deliberately biased toward 'unknown'.

    Returns 'dutch' only when Dutch function words genuinely outnumber English
    ones. A tie, or too little text, returns 'unknown' and the posting is kept —
    a language detector that drops jobs on thin evidence is worse than no
    detector, because the drop is silent.
    """
    words = [w.lower() for w in WORD_RE.findall(text or "")]
    if len(words) < MIN_WORDS_FOR_LANGUAGE:
        return "unknown"

    dutch = sum(1 for w in words if w in DUTCH_MARKERS)
    english = sum(1 for w in words if w in ENGLISH_MARKERS)
    if dutch > english:
        return "dutch"
    if english > dutch:
        return "english"
    return "unknown"


def prefilter(title: str | None, description: str | None, prefs: Preferences) -> PrefilterResult:
    """Apply both deterministic drops. Title first — it is cheaper and cuts more.

    Three outcomes, not two: a clear product role, a clear non-product role, and
    a title that mentions product but doesn't match a configured phrase. The
    third is deferred to the model rather than dropped, because dropping it
    silently loses real jobs (see MAYBE_PRODUCT_RE).
    """
    language = detect_language(description)
    title = title or ""

    if prefs.role_pattern().search(title):
        role_certain = True
    elif MAYBE_PRODUCT_RE.search(title):
        role_certain = False
    else:
        return PrefilterResult(False, DROP_NOT_PRODUCT, language)

    if language == "dutch":
        return PrefilterResult(False, DROP_DUTCH, language, role_certain)
    return PrefilterResult(True, None, language, role_certain)
