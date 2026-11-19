"""Binary search on the BYTES of a time-sorted log file.

The problem: lines have different lengths, so there is no "middle line".
There is only a middle byte. So we search over byte positions:

1. Jump to a byte position `pos` with seek().
2. We usually land in the middle of a line. Skip to the start of the next line.
3. Read that line's timestamp (its first 23 bytes) and compare with the target.
4. Throw away the half that cannot contain the answer. Repeat.

Lines that do not start with a timestamp (for example a stack trace under an
error) are skipped: we always look for the next line that has one.

A 1 GB file needs ~30 jumps; we read a few kilobytes instead of the whole file.
"""

import os
import re
from dataclasses import dataclass, field

TS_LEN = 23  # len("2026-11-19 14:05:12.481")
TS_RE = re.compile(rb"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")
# A target may be any prefix of the timestamp: "2026-11-19 14", "2026-11-19 14:05:30" ...
TARGET_RE = re.compile(r"\d{4}(-\d{2}(-\d{2}( \d{2}(:\d{2}(:\d{2}(\.\d{1,3})?)?)?)?)?)?")


@dataclass
class Probe:
    """One jump of the search, kept so the web page can animate it later."""

    pos: int  # byte we jumped to
    line_start: int  # start of the first timestamped line at or after pos
    timestamp: str | None  # that line's timestamp (None = end of file)
    went: str  # "left" or "right": which half we kept


@dataclass
class SearchResult:
    offset: int  # start of the first line with timestamp >= target (file size if none)
    probes: list[Probe] = field(default_factory=list)
    bytes_read: int = 0


class LogFile:
    """A log file opened for searching. Use it with `with LogFile(path) as log:`."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.size = os.path.getsize(path)
        self._f = open(path, "rb")
        self.bytes_read = 0

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "LogFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- reading ----------------------------------------------------------

    def _line_start_at_or_after(self, pos: int) -> int:
        """Start of the first line that begins at `pos` or later."""
        if pos <= 0:
            return 0
        # Look at the byte just before pos: if it is "\n", a line starts exactly at pos.
        # Otherwise readline() eats the rest of the line we landed in.
        self._f.seek(pos - 1)
        self.bytes_read += len(self._f.readline())
        return self._f.tell()

    def next_entry(self, pos: int) -> tuple[int, bytes | None]:
        """(start, timestamp) of the first TIMESTAMPED line at or after `pos`.

        Returns (file size, None) if there is no such line.
        """
        start = self._line_start_at_or_after(pos)
        self._f.seek(start)
        while start < self.size:
            line = self._f.readline()
            self.bytes_read += len(line)
            if TS_RE.match(line, 0, TS_LEN):
                return start, line[:TS_LEN]
            start += len(line)  # not a timestamped line (e.g. stack trace): skip it
        return self.size, None

    # ---- searching ----------------------------------------------------------

    def find(self, target: str) -> SearchResult:
        """Byte offset of the first line whose timestamp is >= target.

        `target` is a timestamp or any prefix of one, e.g. "2026-11-19 14:05".
        A shorter target sorts before every full timestamp that starts with it,
        so "2026-11-19 14:05" finds the first line at 14:05:00.000 or later.
        """
        if not TARGET_RE.fullmatch(target):
            raise ValueError(f"target must look like '2026-11-19 14:05:30', got {target!r}")
        key = target.encode("ascii")
        self.bytes_read = 0
        probes: list[Probe] = []

        # Smallest byte position p whose next timestamped line has timestamp >= key.
        lo, hi = 0, self.size
        while lo < hi:
            mid = (lo + hi) // 2
            start, ts = self.next_entry(mid)
            if ts is not None and ts < key:
                # This line is too early, and so is everything before it.
                lo = start + 1
                went = "right"
            else:
                # This line is late enough (or we hit the end): the answer is at or before it.
                hi = mid
                went = "left"
            probes.append(Probe(mid, start, ts.decode() if ts else None, went))

        offset, _ = self.next_entry(lo)
        return SearchResult(offset=offset, probes=probes, bytes_read=self.bytes_read)

    def range(self, start: str, end: str) -> tuple[int, int]:
        """Byte range [from, to) holding every line with start <= timestamp < end."""
        first = self.find(start).offset
        last = self.find(end).offset
        return first, max(first, last)

    def read(self, first: int, last: int) -> bytes:
        """Raw bytes between two offsets (for small ranges; stage 3 will stream)."""
        self._f.seek(first)
        return self._f.read(last - first)
