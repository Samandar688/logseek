"""Benchmarks: logseek vs a plain Python scan vs grep, on files of growing size.

Every method answers the same real question:

    "How many ERROR lines were logged between 14:05 and 14:10?"

- logseek : two binary searches find the window, then a C-speed count of
            " ERROR " inside the window only.
- python  : the script most people write first. Read line by line from the
            top, skip lines before 14:05, count until 14:10, then stop.
- grep    : `grep -c` with a regex for the time range. Fast C code, but it
            always reads the whole file, and the time range has to be written
            by hand as a regex.

All three must give the same count; the run fails loudly if they do not.

    python -m logseek.bench --sizes 10MB,100MB,1GB,5GB
"""

import argparse
import json
import shutil
import statistics
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from logseek.generator import generate, parse_size
from logseek.query import count_in_range
from logseek.search import TS_LEN, LogFile

DAY = date(2026, 11, 19)
START, END = "14:05", "14:10"
# The same window as a grep regex: minutes 05..09 of hour 14.
GREP_PATTERN = r"^{day} 14:0[5-9]:.* ERROR "

GIT_GREP = Path(r"C:\Program Files\Git\usr\bin\grep.exe")  # Windows + Git for Windows


@dataclass
class Result:
    method: str
    size: int  # file size in bytes
    seconds: float  # median of the runs
    bytes_read: int
    count: int  # the answer: ERROR lines in the window
    jumps: int = 0  # binary-search jumps (logseek only)


# ---- the three methods --------------------------------------------------------
# Each returns (count, bytes_read, jumps).

def by_logseek(path: Path, start: str, end: str) -> tuple[int, int, int]:
    with LogFile(str(path)) as log:
        a, b = log.find(start), log.find(end)
        count = count_in_range(log, a.offset, b.offset)
        read = a.bytes_read + b.bytes_read + (b.offset - a.offset)
        return count, read, len(a.probes) + len(b.probes)


def by_python(path: Path, start: str, end: str) -> tuple[int, int, int]:
    s, e = start.encode(), end.encode()
    count = read = 0
    with open(path, "rb") as f:
        for line in f:
            read += len(line)
            ts = line[:TS_LEN]
            if ts < s:  # before the window (stack-trace lines also land here)
                continue
            if ts >= e:  # past the window: the file is sorted, so stop
                break
            if b" ERROR " in line:
                count += 1
    return count, read, 0


def find_grep() -> str | None:
    found = shutil.which("grep")
    if found:
        return found
    return str(GIT_GREP) if GIT_GREP.exists() else None


def by_grep(path: Path, start: str, end: str, grep: str) -> tuple[int, int, int]:
    pattern = GREP_PATTERN.format(day=start[:10])
    out = subprocess.run([grep, "-c", "-E", pattern, str(path)], capture_output=True, text=True)
    if out.returncode not in (0, 1):  # 1 = no match, still a valid answer
        raise RuntimeError(f"grep failed: {out.stderr.strip()}")
    return int(out.stdout.strip() or 0), path.stat().st_size, 0


# ---- running ------------------------------------------------------------------

def measure(method: str, fn: Callable[[], tuple[int, int, int]], size: int, repeat: int) -> Result:
    """Run fn `repeat` times and keep the median time (one slow run cannot skew it)."""
    times = []
    for _ in range(repeat):
        t = time.perf_counter()
        count, read, jumps = fn()
        times.append(time.perf_counter() - t)
    return Result(method, size, statistics.median(times), read, count, jumps)


def bench_file(path: Path, repeat: int = 3, grep: str | None = None) -> list[Result]:
    day = DAY.isoformat()
    start, end = f"{day} {START}", f"{day} {END}"
    size = path.stat().st_size
    results = [
        measure("logseek", lambda: by_logseek(path, start, end), size, repeat),
        measure("python", lambda: by_python(path, start, end), size, repeat),
    ]
    if grep:
        results.append(measure("grep", lambda: by_grep(path, start, end, grep), size, repeat))
    counts = {r.method: r.count for r in results}
    if len(set(counts.values())) != 1:
        raise RuntimeError(f"methods disagree on {path.name}: {counts}")
    return results


def ensure_file(folder: Path, label: str) -> Path:
    """data/bench/app_1GB.log, generated once and reused."""
    path = folder / f"app_{label}.log"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        print(f"generating {path} ...", flush=True)
        generate(str(path), parse_size(label), DAY)
    return path


# ---- charts -------------------------------------------------------------------

COLORS = {"logseek": "#2a78d6", "python": "#eb6834", "grep": "#1baf7a"}  # categorical slots 1-3
NAMES = {"logseek": "logseek (binary search)", "python": "Python line scan", "grep": "grep -c"}
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3dd"


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":  # 1,020 MB reads better as 1 GB
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return str(n)


def _ms(s: float) -> str:
    return f"{s * 1000:.2f} ms" if s < 1 else f"{s:.1f} s"


def _tick_time(s: float) -> str:
    return f"{s * 1000:g} ms" if s < 1 else f"{s:g} s"


def _nice_ticks(lo: float, hi: float, units: tuple[float, ...]) -> list[float]:
    """Round axis ticks (1, 10, 100 of each unit) that cover [lo, hi]."""
    ticks = sorted({u * m for u in units for m in (1, 10, 100)})
    inside = [t for t in ticks if lo <= t <= hi]
    below = [t for t in ticks if t < lo][-1:]
    return below + inside


def _spread(points: list[tuple[float, float]], min_ratio: float = 1.6) -> list[float]:
    """Vertical text offsets (points) so end labels that sit close on a log axis do not overlap."""
    offsets = [0.0] * len(points)
    order = sorted(range(len(points)), key=lambda i: points[i][1])
    for a, b in zip(order, order[1:]):
        if points[b][1] / points[a][1] < min_ratio:
            offsets[a] -= 7
            offsets[b] += 7
    return offsets


def plot(results: list[Result], out_dir: Path) -> list[Path]:
    """Two log-log line charts: time vs file size, bytes read vs file size."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 11, "axes.edgecolor": GRID, "text.color": INK,
                         "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED})
    sizes = sorted({r.size for r in results})
    charts = [("seconds", "Time to answer (log scale)", "bench_time.png", _ms, _tick_time,
               (1e-3, 1.0)),
              ("bytes_read", "Bytes read from disk (log scale)", "bench_bytes.png", _human, _human,
               (1024, 1024 ** 2, 1024 ** 3))]
    paths = []
    for field, title, name, fmt, tick_fmt, units in charts:
        fig, ax = plt.subplots(figsize=(8, 4.6), dpi=150)
        ends = []
        for method in COLORS:
            rows = sorted((r for r in results if r.method == method), key=lambda r: r.size)
            if not rows:
                continue
            xs, ys = [r.size for r in rows], [getattr(r, field) for r in rows]
            ax.plot(xs, ys, color=COLORS[method], linewidth=2, marker="o", markersize=7,
                    markeredgecolor="white", markeredgewidth=2, label=NAMES[method])
            ends.append((xs[-1], ys[-1]))
        for (x, y), dy in zip(ends, _spread(ends)):
            ax.annotate(fmt(y), (x, y), xytext=(8, dy), textcoords="offset points",
                        va="center", fontsize=10, color=INK)
        values = [getattr(r, field) for r in results]
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xticks(sizes, [_human(s) for s in sizes])
        ax.minorticks_off()
        ax.set_xlabel("Log file size")
        ax.set_title(title, loc="left", fontsize=13, color=INK, pad=12)
        ax.grid(True, axis="y", color=GRID, linewidth=0.8)
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_yticks(_nice_ticks(min(values), max(values), units))
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _, f=tick_fmt: f(v)))
        ax.legend(frameon=False, loc="upper left")
        ax.margins(x=0.12)
        fig.tight_layout()
        path = out_dir / name
        fig.savefig(path)
        plt.close(fig)
        paths.append(path)
    return paths


def table(results: list[Result]) -> str:
    """Markdown table, one row per file size."""
    by_size: dict[int, dict[str, Result]] = {}
    for r in results:
        by_size.setdefault(r.size, {})[r.method] = r
    lines = ["| File | logseek | Python scan | grep | Speed-up vs grep | logseek reads | jumps |",
             "|---|---|---|---|---|---|---|"]
    for size in sorted(by_size):
        row = by_size[size]
        ls, py, gr = row["logseek"], row["python"], row.get("grep")
        lines.append(
            f"| {_human(size)} | {_ms(ls.seconds)} | {_ms(py.seconds)} | "
            f"{_ms(gr.seconds) if gr else '-'} | "
            f"{f'{gr.seconds / ls.seconds:,.0f}x' if gr else '-'} | {_human(ls.bytes_read)} | {ls.jumps} |"
        )
    return "\n".join(lines)


# ---- command line -------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m logseek.bench", description=__doc__.split("\n")[0])
    p.add_argument("--sizes", default="10MB,100MB,1GB,5GB", help="comma-separated file sizes")
    p.add_argument("--data", default="data/bench", help="where test logs are generated")
    p.add_argument("--out", default="bench", help="where results.json and charts go")
    p.add_argument("--repeat", type=int, default=3, help="runs per method (median is kept)")
    args = p.parse_args(argv)

    grep = find_grep()
    if not grep:
        print("grep not found: skipping it")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    results: list[Result] = []
    for label in args.sizes.split(","):
        path = ensure_file(Path(args.data), label.strip())
        print(f"benchmarking {path.name} ({_human(path.stat().st_size)}) ...", flush=True)
        for r in bench_file(path, args.repeat, grep):
            print(f"  {r.method:<8} {_ms(r.seconds):>10}  read {_human(r.bytes_read):>10}  count {r.count:,}")
            results.append(r)

    (out / "results.json").write_text(json.dumps([asdict(r) for r in results], indent=2))
    print()
    print(table(results))
    for path in plot(results, out):
        print(f"chart: {path}")


if __name__ == "__main__":
    main()
