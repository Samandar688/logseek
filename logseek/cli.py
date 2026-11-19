"""Command line tool.

    logseek data/app.log --from 14:05 --to 14:10 --level ERROR --service orders
    logseek data/app.log --from 14:05 --to 14:10 --count
    logseek data/app.log --histogram
    logseek data/app.log --from 09:00 --to 10:00 --grep timeout --limit 20

Matching lines go to stdout (so you can pipe them); the summary goes to stderr.
"""

import argparse
import sys

from logseek.query import Query, QueryStats, first_day, histogram, normalize_time, run
from logseek.search import LogFile

RED, YELLOW, DIM, RESET = "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="logseek", description="Search a huge time-sorted log file fast.")
    p.add_argument("file", help="log file, e.g. data/app.log")
    p.add_argument("--from", dest="start", help="start time, included: 14:05 or 2026-11-19 14:05")
    p.add_argument("--to", dest="end", help="end time, excluded")
    p.add_argument("--level", action="append", default=[], help="ERROR, WARN, INFO, DEBUG (repeatable)")
    p.add_argument("--service", action="append", default=[], help="service name (repeatable)")
    p.add_argument("--grep", help="text the entry must contain (case-insensitive)")
    p.add_argument("--limit", type=int, help="stop after this many matches")
    p.add_argument("--count", action="store_true", help="print only the number of matches")
    p.add_argument("--histogram", action="store_true", help="print log volume per hour (binary search only)")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    return p


def colorize(text: str, level: str, on: bool) -> str:
    if not on:
        return text
    if level == "ERROR":
        return RED + text + RESET
    if level == "WARN":
        return YELLOW + text + RESET
    if level == "DEBUG":
        return DIM + text + RESET
    return text


def print_histogram(log: LogFile, day: str) -> None:
    buckets = histogram(log, day)
    biggest = max(b.bytes for b in buckets) or 1
    for b in buckets:
        bar = "#" * round(40 * b.bytes / biggest)
        print(f"{b.start[11:16]}  {b.bytes / 1024**2:7.1f} MB  {bar}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        log = LogFile(args.file)
    except OSError as e:
        print(f"logseek: {e}", file=sys.stderr)
        return 2

    with log:
        try:
            day = first_day(log)
            if args.histogram:
                print_histogram(log, day)
                return 0
            q = Query(
                start=normalize_time(args.start, day) if args.start else None,
                end=normalize_time(args.end, day) if args.end else None,
                levels={lv.upper() for lv in args.level},
                services=set(args.service),
                contains=args.grep,
                limit=args.limit,
            )
        except ValueError as e:
            print(f"logseek: {e}", file=sys.stderr)
            return 2

        color = args.color == "always" or (args.color == "auto" and sys.stdout.isatty())
        stats = QueryStats()
        try:
            for e in run(log, q, stats):
                if not args.count:
                    print(colorize(e.text(), e.level, color))
        except BrokenPipeError:  # e.g. `logseek ... | head`: the reader went away
            sys.stderr.close()
            return 0
        if args.count:
            print(stats.matched)

    print(
        f"{stats.matched:,} matches / {stats.scanned:,} entries in window "
        f"({stats.window_bytes / 1024**2:.1f} MB of {log.size / 1024**2:,.1f} MB) | "
        f"binary search {stats.jumps} jumps {stats.search_ms:.2f} ms | scan {stats.scan_ms:.0f} ms",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
