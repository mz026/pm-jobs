"""Link the same posting across boards so it appears once in the list.

Only the cheap tier, deliberately. Measured on the stored corpus, blocking on
normalized company and requiring an exact normalized title matched all 8 known
LinkedIn/Indeed duplicate pairs with no false positives. The fuzzy-title and
description-similarity tiers exist to resolve one ambiguous posting and are not
worth their failure modes here.

Duplicates are linked, never merged. Both source URLs stay reachable, and the
raw store is untouched — this is a view concern.

Why it matters more than the 7% rate suggests: in a CSV a duplicate is a row
you skip. In a read/unread list it is permanent, because marking one copy read
leaves its twin in `unread` forever.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

# Legal forms and country qualifiers. Without these, 'ING' and 'ING Nederland'
# are different companies, and so are 'GAC Motor Europe' and 'GAC Motor Europe
# B.V.' — stripping them found 2 of the 8 pairs.
COMPANY_NOISE = re.compile(
    r"\b(b\s?v|n\s?v|inc|ltd|llc|gmbh|holding|nederland|netherlands|group|"
    r"international|europe|emea)\b"
)
NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str | None) -> str:
    return NON_ALNUM.sub(" ", (text or "").lower()).strip()


def normalize_company(name: str | None) -> str:
    return NON_ALNUM.sub(" ", COMPANY_NOISE.sub(" ", normalize(name))).strip()


def duplicate_groups(rows: list[dict]) -> list[list[dict]]:
    """Group rows that are the same posting seen on different boards.

    A group only forms across boards — two postings from the same board with an
    identical company and title are a re-list, which is a different thing and
    is not collapsed here.
    """
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        key = (normalize_company(row.get("company")), normalize(row.get("title")))
        if all(key):
            buckets[key].append(row)

    return [group for group in buckets.values()
            if len({r["board"] for r in group}) > 1]


def collapse(rows: list[dict]) -> list[dict]:
    """Fold cross-board duplicates into one row carrying every source link.

    The surviving row keeps the richest description-derived fields available
    (Indeed supplies a direct apply link, LinkedIn does not), and is read or
    favorited if *either* copy is — otherwise marking one read would leave the
    other unread and the pair would never leave the view.
    """
    by_id = {(r["board"], r["board_job_id"]): r for r in rows}
    merged_away: set[tuple[str, str]] = set()

    for group in duplicate_groups(rows):
        group = sorted(group, key=lambda r: (r["first_seen_at"], r["board"]))
        primary, *rest = group
        primary["sources"] = [{"board": r["board"], "url": r["job_url"]} for r in group]
        primary["is_read"] = 1 if any(r["is_read"] for r in group) else 0
        primary["is_favorite"] = 1 if any(r["is_favorite"] for r in group) else 0
        primary["duplicate_ids"] = [(r["board"], r["board_job_id"]) for r in rest]
        # Prefer a summary that exists; boards differ on description quality.
        if not primary.get("summary"):
            primary["summary"] = next((r["summary"] for r in rest if r.get("summary")), None)
        primary["tags"] = sorted({t for r in group for t in r.get("tags") or []})
        for r in rest:
            merged_away.add((r["board"], r["board_job_id"]))

    out = []
    for key, row in by_id.items():
        if key in merged_away:
            continue
        row.setdefault("sources", [{"board": row["board"], "url": row["job_url"]}])
        row.setdefault("duplicate_ids", [])
        out.append(row)
    return out


def twins(conn: sqlite3.Connection, board: str, board_job_id: str) -> list[tuple[str, str]]:
    """Every copy of this posting, including itself.

    Used when marking read or favorited so the action applies to the pair —
    the whole point of linking rather than merging.
    """
    row = conn.execute(
        """SELECT r.title, r.company FROM raw_jobs r
           JOIN (SELECT board, board_job_id, MAX(id) AS n FROM raw_jobs GROUP BY 1, 2) x
             ON x.n = r.id
           WHERE r.board = ? AND r.board_job_id = ?""",
        (board, board_job_id),
    ).fetchone()
    if row is None:
        return [(board, board_job_id)]

    key = (normalize_company(row["company"]), normalize(row["title"]))
    if not all(key):
        return [(board, board_job_id)]

    others = conn.execute(
        """SELECT r.board, r.board_job_id, r.title, r.company FROM raw_jobs r
           JOIN (SELECT board, board_job_id, MAX(id) AS n FROM raw_jobs GROUP BY 1, 2) x
             ON x.n = r.id"""
    ).fetchall()
    return [
        (o["board"], o["board_job_id"]) for o in others
        if (normalize_company(o["company"]), normalize(o["title"])) == key
    ]
