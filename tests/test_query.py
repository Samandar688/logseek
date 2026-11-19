from datetime import date

import pytest

from logseek.generator import generate
from logseek.query import Query, QueryStats, histogram, iter_entries, normalize_time, parse_line, run
from logseek.search import LogFile

DAY = date(2026, 11, 19)

TEXT = (
    "2026-11-19 10:00:00.000 INFO  auth     login ok user=1\n"
    "2026-11-19 10:00:01.000 ERROR payment  card declined user=2\n"
    "Traceback (most recent call last):\n"
    "  File \"pay.py\", line 9, in charge\n"
    "2026-11-19 10:00:02.000 WARN  orders   slow response took=900ms\n"
    "2026-11-19 10:00:03.000 ERROR orders   db connection refused host=db-2\n"
    "2026-11-19 11:00:00.000 INFO  payment  charge ok user=3\n"
)


@pytest.fixture
def small(tmp_path):
    path = tmp_path / "t.log"
    path.write_bytes(TEXT.encode())
    with LogFile(str(path)) as log:
        yield log


def run_list(log, **kw):
    return list(run(log, Query(**kw)))


# ---- time input ------------------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("14:05", "2026-11-19 14:05"),
    ("14:05:30", "2026-11-19 14:05:30"),
    ("14:05:30.5", "2026-11-19 14:05:30.5"),
    ("2026-11-19 14:05", "2026-11-19 14:05"),
    ("2026-11-19", "2026-11-19"),
])
def test_normalize_time(given, expected):
    assert normalize_time(given, "2026-11-19") == expected


@pytest.mark.parametrize("bad", ["14", "2pm", "14-05", "2026-11-19T14:05"])
def test_normalize_time_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        normalize_time(bad, "2026-11-19")


# ---- parsing and streaming ----------------------------------------------------------

def test_parse_line():
    e = parse_line("2026-11-19 14:05:00.001 ERROR orders   db connection refused host=db-2")
    assert (e.timestamp, e.level, e.service, e.message) == (
        "2026-11-19 14:05:00.001", "ERROR", "orders", "db connection refused host=db-2")


def test_stack_trace_stays_with_its_entry(small):
    entries = list(iter_entries(small, 0, small.size))
    assert len(entries) == 5
    assert entries[1].extra == ["Traceback (most recent call last):", '  File "pay.py", line 9, in charge']
    assert entries[1].text().count("\n") == 2


@pytest.mark.parametrize("chunk", [1, 7, 64, 1 << 20])
def test_chunk_size_does_not_change_the_result(small, chunk):
    # Lines get cut at chunk edges; the reader must glue them back together.
    whole = [e.text() for e in iter_entries(small, 0, small.size, chunk=1 << 20)]
    assert [e.text() for e in iter_entries(small, 0, small.size, chunk=chunk)] == whole


# ---- filters ----------------------------------------------------------------------

def test_time_window(small):
    got = run_list(small, start="2026-11-19 10:00:01", end="2026-11-19 10:00:03")
    assert [e.message.split()[0] for e in got] == ["card", "slow"]


def test_level_filter_keeps_stack_trace(small):
    got = run_list(small, levels={"ERROR"})
    assert [e.service for e in got] == ["payment", "orders"]
    assert got[0].extra  # the trace came along


def test_service_and_level_together(small):
    got = run_list(small, levels={"ERROR"}, services={"orders"})
    assert [e.message for e in got] == ["db connection refused host=db-2"]


def test_grep_is_case_insensitive_and_searches_trace(small):
    assert len(run_list(small, contains="DB CONNECTION")) == 1
    assert len(run_list(small, contains="pay.py")) == 1  # only in the stack trace


def test_limit_stops_early(small):
    stats = QueryStats()
    got = list(run(small, Query(limit=2), stats))
    assert len(got) == 2 and stats.scanned == 2


def test_stats(small):
    stats = QueryStats()
    list(run(small, Query(start="2026-11-19 10:00:01", levels={"ERROR"}), stats))
    assert stats.matched == 2 and stats.scanned == 4
    assert stats.last == small.size and stats.jumps > 0


# ---- big file: compare with a full scan, and the histogram ---------------------------------

@pytest.fixture(scope="module")
def big(tmp_path_factory):
    path = tmp_path_factory.mktemp("big") / "app.log"
    generate(str(path), 3 * 1024**2, DAY, seed=5)
    return str(path)


def test_query_matches_full_scan(big):
    lines = open(big, encoding="ascii").read().splitlines()
    expected = [ln for ln in lines
                if "2026-11-19 14:05" <= ln[:23] < "2026-11-19 14:10" and " ERROR orders " in ln]
    with LogFile(big) as log:
        got = run_list(log, start="2026-11-19 14:05", end="2026-11-19 14:10",
                       levels={"ERROR"}, services={"orders"})
    assert [e.text() for e in got] == expected
    assert len(expected) > 3000  # the 14:05 incident is in there


def test_histogram_covers_the_file_and_shows_the_day_shape(big):
    with LogFile(big) as log:
        buckets = histogram(log, "2026-11-19")
        assert len(buckets) == 24
        assert buckets[0].first == 0 and buckets[-1].last == log.size
        assert all(a.last == b.first for a, b in zip(buckets, buckets[1:]))  # no gaps
        by_hour = {b.start[11:13]: b.bytes for b in buckets}
        assert by_hour["16"] > 2.5 * by_hour["04"]


@pytest.mark.parametrize("chunk", [3, 8, 1 << 20])
def test_count_in_range_matches_simple_count(big, chunk):
    from logseek.query import count_in_range

    data = open(big, "rb").read()
    with LogFile(big) as log:
        assert count_in_range(log, 0, log.size, chunk=chunk) == data.count(b" ERROR ")
        assert count_in_range(log, 1000, 50_000, chunk=chunk) == data[1000:50_000].count(b" ERROR ")


def test_scan_cap_cuts_window_at_whole_entry(big):
    stats = QueryStats()
    with LogFile(big) as log:
        got = list(run(log, Query(max_scan_bytes=10_000), stats))
        assert stats.truncated and 10_000 <= stats.last < 10_200
        assert got[-1].text().endswith(got[-1].message)  # last entry is whole


def test_histogram_needs_even_buckets(big):
    with LogFile(big) as log, pytest.raises(ValueError):
        histogram(log, "2026-11-19", minutes=7)
