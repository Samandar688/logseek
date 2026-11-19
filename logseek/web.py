"""Web API + page for one log file.

    logseek-web data/app.log --port 8000      then open http://127.0.0.1:8000

Endpoints (all read-only; the file path is fixed when the server starts):

    GET /api/info        file size, day, first/last timestamp
    GET /api/histogram   bytes + error count per time bucket
    GET /api/search      matching entries + stats + the binary-search jumps
"""

import argparse
import os
import pathlib
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Annotated

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi import Query as Param
from fastapi.staticfiles import StaticFiles

from logseek.query import (
    Query,
    QueryStats,
    first_day,
    histogram_between,
    last_timestamp,
    normalize_time,
    run,
)
from logseek.search import LogFile

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
MAX_SCAN_BYTES = 128 * 1024**2  # a search reads at most 128 MB of its window
MAX_COUNT_BYTES = 4 * 1024**3  # error counts are skipped for windows bigger than 4 GB


def create_app(path: str) -> FastAPI:
    with LogFile(path) as log:
        day = first_day(log)
        info = {
            "file": os.path.basename(path),
            "size": log.size,
            "day": day,
            "first": log.next_entry(0)[1].decode(),
            "last": last_timestamp(log),
        }

    app = FastAPI(title="logseek")

    def to_stamp(text: str | None, default: str) -> str:
        try:
            return normalize_time(text, day) if text else default
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    def to_datetime(stamp: str) -> datetime:
        # "2026-11-19 14:05" / "...:30" / "...:30.250" -> datetime
        try:
            return datetime.fromisoformat(stamp.ljust(23, "0") if len(stamp) > 19 else stamp)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"bad time {stamp!r}")

    @lru_cache(maxsize=128)
    def cached_histogram(start: str, end: str, buckets: int, errors: bool) -> list[dict]:
        a, b = to_datetime(start), to_datetime(end)
        with LogFile(path) as log:
            window = log.find(end).offset - log.find(start).offset
            count = errors and window <= MAX_COUNT_BYTES
            rows = histogram_between(log, a, b, buckets, count_errors=count)
        return [
            {"start": r.start, "end": r.end, "first": r.first, "last": r.last,
             "bytes": r.bytes, "errors": r.errors}
            for r in rows
        ]

    @app.get("/api/info")
    def get_info() -> dict:
        return info

    @app.get("/api/histogram")
    def get_histogram(
        start: str | None = None,
        end: str | None = None,
        buckets: Annotated[int, Param(ge=1, le=400)] = 96,
        errors: bool = True,
    ) -> dict:
        next_day = (datetime.fromisoformat(day) + timedelta(days=1)).strftime("%Y-%m-%d")
        s = to_stamp(start, f"{day} 00:00")
        e = to_stamp(end, f"{next_day} 00:00")
        if to_datetime(e) <= to_datetime(s):
            raise HTTPException(status_code=422, detail="end must be after start")
        return {"start": s, "end": e, "buckets": cached_histogram(s, e, buckets, errors)}

    @app.get("/api/search")
    def get_search(
        start: str | None = None,
        end: str | None = None,
        level: Annotated[list[str], Param()] = [],
        service: Annotated[list[str], Param()] = [],
        grep: Annotated[str | None, Param(max_length=100)] = None,
        limit: Annotated[int, Param(ge=1, le=1000)] = 200,
    ) -> dict:
        q = Query(
            start=to_stamp(start, "") or None,
            end=to_stamp(end, "") or None,
            levels={lv.upper() for lv in level if lv},
            services={s for s in service if s},
            contains=grep or None,
            limit=limit,
            max_scan_bytes=MAX_SCAN_BYTES,
        )
        stats = QueryStats()
        with LogFile(path) as log:
            entries = [
                {"timestamp": e.timestamp, "level": e.level, "service": e.service,
                 "message": e.message, "extra": e.extra}
                for e in run(log, q, stats)
            ]
        return {
            "entries": entries,
            "stats": {
                "first": stats.first, "last": stats.last, "window_bytes": stats.window_bytes,
                "jumps": stats.jumps, "search_ms": round(stats.search_ms, 3),
                "scan_ms": round(stats.scan_ms, 1), "scanned": stats.scanned,
                "matched": stats.matched, "truncated": stats.truncated,
                "limited": stats.matched >= limit,
            },
            "probes": [
                {"pos": p.pos, "line_start": p.line_start, "timestamp": p.timestamp,
                 "went": p.went, "lo": p.lo, "hi": p.hi}
                for p in stats.probes
            ],
        }

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the logseek web page for one log file")
    parser.add_argument("file", help="log file, e.g. data/app.log")
    parser.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 inside Docker")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args()
    uvicorn.run(create_app(args.file), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
