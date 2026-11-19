import bisect
import math
import random
from datetime import date

import pytest

from logseek.generator import generate
from logseek.search import TS_LEN, LogFile

DAY = date(2026, 11, 19)


def write(tmp_path, text: str, name: str = "t.log"):
    path = tmp_path / name
    path.write_bytes(text.encode("ascii"))
    return str(path)


SMALL = (
    "2026-11-19 10:00:00.000 INFO  a one\n"
    "2026-11-19 10:00:01.000 INFO  a two\n"
    "2026-11-19 10:00:01.000 INFO  a two-again (same time)\n"
    "2026-11-19 10:00:05.500 ERROR b three\n"
    "2026-11-19 11:30:00.000 INFO  c four\n"
)


def offsets_of(text: str) -> list[int]:
    out, pos = [], 0
    for line in text.splitlines(keepends=True):
        out.append(pos)
        pos += len(line)
    return out


@pytest.fixture
def small(tmp_path):
    with LogFile(write(tmp_path, SMALL)) as log:
        yield log


# ---- exact answers on a small file you can read by eye -------------------------

def test_target_before_first_line_gives_zero(small):
    assert small.find("2026-11-19 09").offset == 0


def test_target_after_last_line_gives_file_size(small):
    assert small.find("2026-11-19 12").offset == small.size


def test_exact_time_finds_first_of_duplicates(small):
    starts = offsets_of(SMALL)
    assert small.find("2026-11-19 10:00:01").offset == starts[1]  # not starts[2]


def test_target_between_lines(small):
    starts = offsets_of(SMALL)
    assert small.find("2026-11-19 10:00:02").offset == starts[3]
    assert small.find("2026-11-19 10:00:05.6").offset == starts[4]


def test_range_returns_lines_in_window(small):
    first, last = small.range("2026-11-19 10:00:01", "2026-11-19 10:00:06")
    lines = small.read(first, last).decode().splitlines()
    assert [ln.split()[-1] for ln in lines] == ["two", "time)", "three"]


def test_empty_range_when_end_before_start(small):
    first, last = small.range("2026-11-19 11", "2026-11-19 10")
    assert first == last


# ---- awkward landings ----------------------------------------------------------

def test_every_landing_position_gives_the_right_line(small):
    # Ask "next timestamped line from byte p" for EVERY byte, including the "\n" bytes.
    starts = offsets_of(SMALL)
    for p in range(small.size + 1):
        start, ts = small.next_entry(p)
        expected = next((s for s in starts if s >= p), small.size)
        assert start == expected, f"landed at byte {p}"
        if start < small.size:
            assert ts == SMALL.encode()[start:start + TS_LEN]


def test_lines_without_timestamp_are_skipped(tmp_path):
    text = (
        "2026-11-19 10:00:00.000 ERROR a boom\n"
        "Traceback (most recent call last):\n"
        "  File \"app.py\", line 3, in <module>\n"
        "ValueError: bad\n"
        "2026-11-19 10:00:02.000 INFO  a ok\n"
    )
    with LogFile(write(tmp_path, text)) as log:
        assert log.find("2026-11-19 10:00:01").offset == text.index("2026-11-19 10:00:02")
        # A stack trace stays attached to its error line when we read a range.
        first, last = log.range("2026-11-19 10:00:00", "2026-11-19 10:00:01")
        assert b"ValueError: bad" in log.read(first, last)


def test_file_without_final_newline(tmp_path):
    text = SMALL.rstrip("\n")
    with LogFile(write(tmp_path, text)) as log:
        assert log.find("2026-11-19 11:30").offset == offsets_of(text)[4]
        assert log.find("2026-11-19 12").offset == log.size


def test_empty_file(tmp_path):
    with LogFile(write(tmp_path, "")) as log:
        assert log.find("2026-11-19").offset == 0


def test_bad_target_is_rejected(small):
    for bad in ["14:05", "2026/11/19", "2026-11-19T14:05", "yesterday"]:
        with pytest.raises(ValueError):
            small.find(bad)


# ---- big generated file: compare with a plain linear answer ----------------------

@pytest.fixture(scope="module")
def big(tmp_path_factory):
    path = tmp_path_factory.mktemp("big") / "app.log"
    generate(str(path), 3 * 1024**2, DAY, seed=3)
    data = path.read_bytes()
    starts, stamps, pos = [], [], 0
    for line in data.splitlines(keepends=True):
        starts.append(pos)
        stamps.append(line[:TS_LEN])
        pos += len(line)
    return str(path), starts, stamps, len(data)


def test_binary_search_matches_linear_answer(big):
    path, starts, stamps, size = big
    r = random.Random(0)
    with LogFile(path) as log:
        for _ in range(300):
            h, m, s, ms = r.randrange(24), r.randrange(60), r.randrange(60), r.randrange(1000)
            target = f"2026-11-19 {h:02d}:{m:02d}:{s:02d}.{ms:03d}"
            i = bisect.bisect_left(stamps, target.encode())  # the slow, obvious answer
            expected = starts[i] if i < len(starts) else size
            assert log.find(target).offset == expected, target


def test_jumps_grow_like_log2_of_file_size(big):
    path, _, _, size = big
    with LogFile(path) as log:
        result = log.find("2026-11-19 14:05")
    assert len(result.probes) <= math.log2(size) + 2
    assert result.bytes_read < 20_000  # a few KB, not 3 MB


def test_probes_record_the_path(big):
    path, _, _, _ = big
    with LogFile(path) as log:
        probes = log.find("2026-11-19 14:05").probes
    assert {p.went for p in probes} <= {"left", "right"}
    assert all(p.line_start >= p.pos for p in probes)
    assert probes[0].pos == log.size // 2  # the first jump is to the middle byte
