import re
from datetime import date

import pytest

from logseek.generator import INCIDENTS, daily_factor, generate, parse_size

DAY = date(2026, 11, 19)
LINE_RE = re.compile(
    r"^2026-11-19 \d{2}:\d{2}:\d{2}\.\d{3} (DEBUG|INFO |WARN |ERROR) (auth|payment|orders|search|gateway)\s+\S.*$"
)


@pytest.fixture(scope="module")
def log(tmp_path_factory):
    path = tmp_path_factory.mktemp("logs") / "app.log"
    stats = generate(str(path), 2 * 1024**2, DAY, seed=1)  # 2 MB is enough to check the shape
    return path, path.read_text(encoding="ascii").splitlines(), stats


def test_every_line_has_the_right_format(log):
    _, lines, _ = log
    bad = [ln for ln in lines if not LINE_RE.match(ln)]
    assert bad == []


def test_lines_are_sorted_by_time(log):
    # The whole project depends on this: binary search needs a sorted file.
    _, lines, _ = log
    stamps = [ln[:23] for ln in lines]
    assert stamps == sorted(stamps)


def test_file_covers_the_whole_day(log):
    _, lines, _ = log
    assert lines[0].startswith("2026-11-19 00:")
    assert lines[-1].startswith("2026-11-19 23:")


def test_size_is_close_to_target(log):
    path, _, _ = log
    assert 0.93 < path.stat().st_size / (2 * 1024**2) < 1.07


def test_stats_match_file(log):
    _, lines, stats = log
    assert stats["lines"] == len(lines)
    assert stats["errors"] == sum(" ERROR " in ln for ln in lines)


def test_same_seed_gives_same_file(tmp_path):
    a, b = tmp_path / "a.log", tmp_path / "b.log"
    generate(str(a), 200_000, DAY, seed=7)
    generate(str(b), 200_000, DAY, seed=7)
    assert a.read_bytes() == b.read_bytes()


def test_incidents_create_error_spikes(log):
    _, lines, _ = log
    inc = INCIDENTS[1]  # 14:05, orders, db connection refused
    during = [ln for ln in lines if ln[11:16] in ("14:05", "14:06") and inc.message in ln]
    assert len(during) >= inc.errors_per_second * 60  # at least a full minute of them
    assert not any(inc.message in ln for ln in lines if ln[11:13] == "03")  # nothing at night


def test_afternoon_is_busier_than_night(log):
    _, lines, _ = log
    night = sum(ln[11:13] == "04" for ln in lines)
    afternoon = sum(ln[11:13] == "16" for ln in lines)
    assert afternoon > 2.5 * night
    assert daily_factor(4 * 3600) == pytest.approx(0.25)
    assert daily_factor(16 * 3600) == pytest.approx(1.0)


@pytest.mark.parametrize("text, expected", [("10B", 10), ("1KB", 1024), ("100MB", 100 * 1024**2), ("1.5GB", int(1.5 * 1024**3))])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_parse_size_rejects_garbage():
    with pytest.raises(ValueError):
        parse_size("lots")
