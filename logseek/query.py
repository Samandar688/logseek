"""Query engine: find a time range with binary search, then stream and filter it.

Binary search (search.py) gives two byte offsets. Everything between them is
the time window we want. This module reads ONLY that window, in fixed-size
chunks, so even a 1 GB window never sits in memory at once.

It also builds a volume histogram (how many bytes of logs per hour) with
binary search alone: one find() per bucket edge, no full read.
"""

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from logseek.search import TS_LEN, TS_RE, LogFile

CHUNK = 1 << 20  # read the window 1 MB at a time

_TIME_ONLY = re.compile(r"\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?")
_FULL = re.compile(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?)?")


# ---- time input ---------------------------------------------------------------

def normalize_time(text: str, day: str) -> str:
    """Turn user input into a timestamp prefix the search understands.

    "14:05"              -> "2026-11-19 14:05"   (day taken from the file)
    "14:05:30"           -> "2026-11-19 14:05:30"
    "2026-11-19 14:05"   -> unchanged
    """
    text = text.strip()
    if _TIME_ONLY.fullmatch(text):
        return f"{day} {text}"
    if _FULL.fullmatch(text):
        return text
    raise ValueError(f"time must look like 14:05, 14:05:30 or 2026-11-19 14:05, got {text!r}")


def first_day(log: LogFile) -> str:
    """The date of the first timestamped line, e.g. '2026-11-19'."""
    _, ts = log.next_entry(0)
    if ts is None:
        raise ValueError(f"{log.path} has no timestamped lines")
    return ts[:10].decode()


# ---- entries ----------------------------------------------------------------

@dataclass
class Entry:
    """One log event: its first line plus any untimed lines under it (stack trace)."""

    timestamp: str
    level: str
    service: str
    message: str
    extra: list[str] = field(default_factory=list)

    def text(self) -> str:
        first = f"{self.timestamp} {self.level:<5} {self.service:<8} {self.message}"
        return "\n".join([first, *self.extra])


def parse_line(line: str) -> Entry:
    # "2026-11-19 14:05:00.001 ERROR orders   db connection refused host=db-2"
    parts = line[TS_LEN:].split(maxsplit=2)
    level = parts[0] if len(parts) > 0 else ""
    service = parts[1] if len(parts) > 1 else ""
    message = parts[2] if len(parts) > 2 else ""
    return Entry(line[:TS_LEN], level, service, message)


def iter_entries(log: LogFile, first: int, last: int, chunk: int = CHUNK) -> Iterator[Entry]:
    """Yield the entries between two byte offsets, reading `chunk` bytes at a time."""
    current: Entry | None = None
    leftover = b""
    pos = first
    while pos < last:
        data = log.read(pos, min(pos + chunk, last))
        pos += len(data)
        lines = (leftover + data).split(b"\n")
        leftover = lines.pop()  # the last piece may be a half line: keep it for the next chunk
        for raw in lines:
            current = yield from _add_line(raw, current)
    if leftover:
        current = yield from _add_line(leftover, current)
    if current is not None:
        yield current


def _add_line(raw: bytes, current: Entry | None):
    """Start a new entry for a timestamped line, or attach an untimed line to the current one."""
    line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
    if TS_RE.match(raw, 0, TS_LEN):
        if current is not None:
            yield current
        return parse_line(line)
    if current is not None:
        current.extra.append(line)
    return current


# ---- the query ----------------------------------------------------------------

@dataclass
class Query:
    start: str | None = None  # timestamp prefix, None = start of file
    end: str | None = None  # timestamp prefix (excluded), None = end of file
    levels: set[str] = field(default_factory=set)  # empty = any level
    services: set[str] = field(default_factory=set)  # empty = any service
    contains: str | None = None  # substring of the message (case-insensitive)
    limit: int | None = None

    def matches(self, e: Entry) -> bool:
        if self.levels and e.level not in self.levels:
            return False
        if self.services and e.service not in self.services:
            return False
        if self.contains and self.contains.lower() not in e.text().lower():
            return False
        return True


@dataclass
class QueryStats:
    first: int = 0  # byte offsets of the time window
    last: int = 0
    search_ms: float = 0.0  # time spent in binary search
    scan_ms: float = 0.0  # time spent reading + filtering the window
    jumps: int = 0
    scanned: int = 0  # entries in the window
    matched: int = 0

    @property
    def window_bytes(self) -> int:
        return self.last - self.first


def run(log: LogFile, q: Query, stats: QueryStats | None = None) -> Iterator[Entry]:
    """Yield matching entries. Pass a QueryStats to get numbers back."""
    stats = stats if stats is not None else QueryStats()
    t = time.perf_counter()
    first, last, jumps = 0, log.size, 0
    if q.start:
        r = log.find(q.start)
        first, jumps = r.offset, jumps + len(r.probes)
    if q.end:
        r = log.find(q.end)
        last, jumps = max(first, r.offset), jumps + len(r.probes)
    stats.first, stats.last, stats.jumps = first, last, jumps
    stats.search_ms = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    try:
        for e in iter_entries(log, first, last):
            stats.scanned += 1
            if q.matches(e):
                stats.matched += 1
                yield e
                if q.limit is not None and stats.matched >= q.limit:
                    return
    finally:
        stats.scan_ms = (time.perf_counter() - t) * 1000


# ---- histogram ----------------------------------------------------------------

@dataclass
class Bucket:
    start: str  # "2026-11-19 14:00"
    first: int  # byte offsets
    last: int

    @property
    def bytes(self) -> int:
        return self.last - self.first


def histogram(log: LogFile, day: str, minutes: int = 60) -> list[Bucket]:
    """Bytes of logs per time bucket for one day, using only binary search.

    24 hourly buckets = 25 find() calls ~ 750 jumps ~ a few hundred KB read,
    for a file of any size. Bytes are a good stand-in for "number of lines".
    """
    if minutes <= 0 or (24 * 60) % minutes:
        raise ValueError("minutes must divide a day evenly (e.g. 5, 15, 60)")
    begin = datetime.fromisoformat(day)
    edges = [begin + timedelta(minutes=m) for m in range(0, 24 * 60 + 1, minutes)]
    offsets = [log.find(e.strftime("%Y-%m-%d %H:%M")).offset for e in edges]
    return [
        Bucket(edges[i].strftime("%Y-%m-%d %H:%M"), offsets[i], offsets[i + 1])
        for i in range(len(edges) - 1)
    ]
