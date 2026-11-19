import json

import pytest

from logseek import bench
from logseek.generator import generate


@pytest.fixture(scope="module")
def log(tmp_path_factory):
    path = tmp_path_factory.mktemp("bench") / "app.log"
    generate(str(path), 2_000_000, bench.DAY)
    return path


def window():
    day = bench.DAY.isoformat()
    return f"{day} {bench.START}", f"{day} {bench.END}"


def test_logseek_and_python_agree(log):
    start, end = window()
    ls_count, ls_read, jumps = bench.by_logseek(log, start, end)
    py_count, py_read, _ = bench.by_python(log, start, end)
    assert ls_count == py_count > 0  # the 14:05 incident is in the window
    assert ls_read < py_read  # binary search reads less than a scan from the top
    assert jumps > 0


@pytest.mark.skipif(bench.find_grep() is None, reason="grep not installed")
def test_grep_agrees(log):
    start, end = window()
    count, read, _ = bench.by_grep(log, start, end, bench.find_grep())
    assert count == bench.by_logseek(log, start, end)[0]
    assert read == log.stat().st_size  # grep always reads everything


def test_bench_file_returns_one_result_per_method(log):
    results = bench.bench_file(log, repeat=1, grep=None)
    assert [r.method for r in results] == ["logseek", "python"]
    assert all(r.seconds > 0 and r.size == log.stat().st_size for r in results)


def test_table_and_main_without_grep(log, tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    monkeypatch.setattr(bench, "find_grep", lambda: None)
    data = tmp_path / "data"
    data.mkdir()
    (data / "app_2MB.log").write_bytes(log.read_bytes())  # reuse instead of generating
    out = tmp_path / "out"
    bench.main(["--sizes", "2MB", "--data", str(data), "--out", str(out), "--repeat", "1"])
    rows = json.loads((out / "results.json").read_text())
    assert {r["method"] for r in rows} == {"logseek", "python"}
    assert (out / "bench_time.png").stat().st_size > 0
    assert (out / "bench_bytes.png").stat().st_size > 0


def test_human_sizes():
    assert bench._human(512) == "512 B"
    assert bench._human(10 * 1024 * 1024) == "10 MB"
    assert bench._human(1.5 * 1024 ** 3) == "1.5 GB"
