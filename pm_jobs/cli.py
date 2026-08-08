"""Command line entry point: `pm-jobs <command>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .backfill import run_backfill
from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from .scrape import plan, resolve_window, run_scrape
from .store import DEFAULT_DB_PATH, RawStore, connect


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="path to searches.yaml")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="path to the SQLite store")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pm-jobs", description="Personal PM job scanner")
    sub = parser.add_subparsers(dest="command", required=True)

    scrape = sub.add_parser("scrape", help="run configured searches and store raw results")
    _add_common(scrape)
    scrape.add_argument("--search", action="append", dest="searches", metavar="NAME",
                        help="run only this search (repeatable); default is every enabled search")
    scrape.add_argument("--dry-run", action="store_true", help="show what would be scraped, hit no boards")
    scrape.add_argument("--pause", type=float, default=None, help="seconds to wait between board calls")
    scrape.add_argument("--no-backfill", action="store_true",
                        help="skip the description backfill that normally follows a scrape")
    scrape.add_argument("--since-hours", type=int, metavar="N",
                        help="look back exactly N hours instead of since the last successful run")
    scrape.add_argument("--full", action="store_true",
                        help="use each search's configured window instead of scraping incrementally")

    backfill = sub.add_parser("backfill", help="fetch descriptions LinkedIn's search endpoint omits")
    _add_common(backfill)
    backfill.add_argument("--limit", type=int, default=None, help="fetch at most this many")
    backfill.add_argument("--pause", type=float, default=None, help="seconds to wait between fetches")
    backfill.add_argument("--retry-failed", action="store_true",
                          help="also retry postings that already failed the maximum number of times")

    runs = sub.add_parser("runs", help="show recent scrape runs")
    _add_common(runs)
    runs.add_argument("--limit", type=int, default=10)
    runs.add_argument("--show", type=int, metavar="RUN_ID", help="show every task of one run")

    stats = sub.add_parser("stats", help="show what is currently in the raw store")
    _add_common(stats)

    searches = sub.add_parser("searches", help="list configured searches")
    _add_common(searches)

    return parser


def cmd_searches(args) -> int:
    config = load_config(args.config)
    for search in config.searches:
        mark = " " if search.enabled else "-"
        print(f"{mark} {search.name}")
        print(f"    location : {search.location.query}  ({search.location.region}, {search.location.country})")
        print(f"    terms    : {', '.join(search.terms)}")
        print(f"    boards   : {', '.join(search.boards)}")
        print(f"    window   : last {search.hours_old}h, up to {search.results_wanted} per term/board")
    return 0


def cmd_scrape(args) -> int:
    config = load_config(args.config)
    selected = [config.get(name) for name in args.searches] if args.searches else config.enabled()
    if not selected:
        print("No enabled searches. Set `enabled: true` on at least one search.", file=sys.stderr)
        return 1

    legs = plan(selected)
    conn = connect(args.db)
    store = RawStore(conn)
    try:
        if args.dry_run:
            last_run_at = None if args.full else store.last_successful_run_at()
            print(f"Would run {len(legs)} board calls from {args.config}")
            print(f"Last successful run: {last_run_at or '(none — using configured windows)'}\n")
            for search, term, board in legs:
                window = (args.since_hours if args.since_hours is not None
                          else resolve_window(search, last_run_at))
                kwargs = search.jobspy_kwargs(term, board, hours_old=window)
                extra = kwargs.get("google_search_term") or ""
                print(f"  {search.name} · {board:<14} {term!r} @ {kwargs['location']}"
                      f" · last {window}h"
                      f"{' | google: ' + repr(extra) if extra else ''}")
            return 0

        result = run_scrape(
            config, store, searches=selected,
            on_progress=lambda msg: print(msg, flush=True),
            since_hours=args.since_hours, full=args.full,
            **({"pause": args.pause} if args.pause is not None else {}),
        )
        print(f"\nRun {result.run_id}: {result.status} — "
              f"{result.rows_returned} rows returned, {result.rows_new} new versions stored")
        for task in result.tasks:
            if not task.ok:
                print(f"  failed: {task.search_name} · {task.term!r} · {task.board} — {task.error}",
                      file=sys.stderr)

        # Backfill runs on already-committed scrape data, so a failure here
        # costs nothing that was just scraped and must not fail the scrape.
        if not args.no_backfill:
            print()
            back = run_backfill(store, on_progress=lambda msg: print(msg, flush=True),
                                **({"pause": args.pause} if args.pause is not None else {}))
            print(back.summary())
            if back.aborted:
                print(f"  {back.aborted}", file=sys.stderr)
    finally:
        conn.close()

    return 0 if result.status == "ok" else 1


def cmd_backfill(args) -> int:
    conn = connect(args.db)
    store = RawStore(conn)
    try:
        result = run_backfill(
            store, limit=args.limit, retry_failed=args.retry_failed,
            on_progress=lambda msg: print(msg, flush=True),
            **({"pause": args.pause} if args.pause is not None else {}),
        )
    finally:
        conn.close()

    print(result.summary())
    for error in result.errors[:5]:
        print(f"  {error}", file=sys.stderr)
    return 0 if result.status == "ok" else 1


def cmd_runs(args) -> int:
    conn = connect(args.db)
    store = RawStore(conn)
    try:
        if args.show:
            rows = store.run_tasks(args.show)
            if not rows:
                print(f"No run {args.show}.", file=sys.stderr)
                return 1
            for row in rows:
                line = (f"  {row['status']:<6} {row['search_name']} · {row['term']!r} · {row['board']:<14}"
                        f" {row['rows_returned']} rows / {row['rows_new']} new")
                print(line + (f"\n         {row['error']}" if row["error"] else ""))
            return 0

        runs = store.recent_runs(args.limit)
        if not runs:
            print("No runs yet. Try: pm-jobs scrape")
            return 0
        for row in runs:
            errors = f", {row['errors']} failed legs" if row["errors"] else ""
            print(f"  #{row['id']:<4} {row['started_at']}  {row['status']:<8} "
                  f"{row['sightings']} sightings{errors}")
    finally:
        conn.close()
    return 0


def cmd_stats(args) -> int:
    conn = connect(args.db)
    store = RawStore(conn)
    try:
        s = store.stats()
        print(f"  runs              : {s['runs']}")
        print(f"  distinct postings : {s['distinct_postings']}")
        print(f"  content versions  : {s['raw_versions']}")
        print(f"  sightings         : {s['sightings']}")
        if s["backfill_failed"]:
            print(f"  gave up on     : {s['backfill_failed']} (use `backfill --retry-failed`)")
        if s["by_board"]:
            print("  by board:")
            for row in s["by_board"]:
                print(f"    {row['board']:<14} {row['postings']:>4} postings, "
                      f"{row['with_description']} with description")

        f = store.freshness()
        print(f"\n  posting dates     : {f['with_date']} of {f['postings']} report one")
        if f["without_date"]:
            print(f"    undated         : {f['without_date']}"
                  f" ({f['undated_and_censored']} first seen in the earliest run,"
                  f" so their age is only a lower bound)")
        for row in f["by_date"]:
            print(f"    {row['day']:<16} {row['postings']}")
    finally:
        conn.close()
    return 0


COMMANDS = {
    "scrape": cmd_scrape,
    "backfill": cmd_backfill,
    "runs": cmd_runs,
    "stats": cmd_stats,
    "searches": cmd_searches,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
