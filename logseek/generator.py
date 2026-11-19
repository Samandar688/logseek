"""Make a realistic, time-sorted log file of any size (one full day of logs).

Every line looks like this (the timestamp is always the first 23 characters):

    2026-11-19 14:05:12.481 ERROR payment  card declined user=4821 amount=120.50

Why this exact timestamp format? "YYYY-MM-DD HH:MM:SS.mmm" sorts the same way
as text and as time. So later we can compare raw bytes, no date parsing needed.

The day is not flat, like real traffic:
* quiet at night, busy in the afternoon (daily curve),
* a few "incidents": short windows where one service throws many errors.

Run it:  python -m logseek.generator --out data/app.log --size 100MB
"""

import argparse
import math
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

SECONDS_PER_DAY = 86_400
AVG_LINE_BYTES = 70  # measured: 100 MB run gave 83,665,920 bytes / 1,199,653 lines ~ 69.7

SERVICES = ["auth", "payment", "orders", "search", "gateway"]
SERVICE_WEIGHTS = [20, 10, 15, 25, 30]

LEVELS = ["DEBUG", "INFO", "WARN", "ERROR"]
LEVEL_WEIGHTS = [10, 80, 8, 2]


@dataclass(frozen=True)
class Incident:
    """A short window where one service fails a lot."""

    start: str  # "HH:MM"
    minutes: int
    service: str
    message: str
    errors_per_second: int

    def seconds(self) -> range:
        h, m = map(int, self.start.split(":"))
        first = h * 3600 + m * 60
        return range(first, first + self.minutes * 60)


INCIDENTS = [
    Incident("09:12", 8, "payment", "timeout calling bank-api after 3000ms", 6),
    Incident("14:05", 5, "orders", "db connection refused host=db-2", 12),
    Incident("21:40", 3, "auth", "token signature invalid", 9),
]


# ---- message templates ----------------------------------------------------
# Each takes a random generator and returns the text after the service name.

def _info(service: str, r: random.Random) -> str:
    user = r.randrange(1, 50_000)
    if service == "auth":
        return f"login ok user={user} ip=10.0.{r.randrange(256)}.{r.randrange(256)}"
    if service == "payment":
        return f"charge ok user={user} amount={r.randrange(100, 50_000) / 100:.2f}"
    if service == "orders":
        return f"order created id={r.randrange(10**6, 10**7)} items={r.randrange(1, 9)}"
    if service == "search":
        return f"query ok q=item{r.randrange(5000)} hits={r.randrange(200)} took={r.randrange(1, 90)}ms"
    path = r.choice(["/api/cart", "/api/items", "/api/login", "/api/pay", "/api/search"])
    return f"GET {path} 200 {r.randrange(2, 120)}ms"


def _warn(service: str, r: random.Random) -> str:
    return r.choice([
        f"slow response took={r.randrange(500, 2000)}ms",
        f"retry attempt={r.randrange(1, 4)}",
        f"cache miss key=k{r.randrange(10_000)}",
    ])


def _error(service: str, r: random.Random) -> str:
    return r.choice([
        f"unexpected status 500 from {r.choice(SERVICES)}",
        f"request failed user={r.randrange(1, 50_000)} reason=timeout",
        "null value in required field",
    ])


def _debug(service: str, r: random.Random) -> str:
    return f"trace span={r.randrange(16**8):08x} step={r.randrange(1, 20)}"


TEMPLATES = {"DEBUG": _debug, "INFO": _info, "WARN": _warn, "ERROR": _error}


# ---- traffic shape --------------------------------------------------------

def daily_factor(second: int) -> float:
    """How busy this moment of the day is: 0.25 at 04:00, 1.0 at 16:00."""
    phase = 2 * math.pi * (second - 4 * 3600) / SECONDS_PER_DAY
    return 0.25 + 0.75 * (0.5 - 0.5 * math.cos(phase))


def parse_size(text: str) -> int:
    """'100MB' -> 104857600. Accepts B, KB, MB, GB (1024-based)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMG]?B)\s*", text.upper())
    if not m:
        raise ValueError(f"size must look like 100MB or 2GB, got {text!r}")
    number, unit = float(m.group(1)), m.group(2)
    return int(number * {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}[unit])


# ---- the generator --------------------------------------------------------

def generate(path: str, target_bytes: int, day: date, seed: int = 42) -> dict:
    """Write one day of logs to `path`, about `target_bytes` big. Returns stats."""
    r = random.Random(seed)
    # Incidents always add the same number of lines, so take them out of the budget first.
    incident_lines = sum(inc.minutes * 60 * inc.errors_per_second for inc in INCIDENTS)
    total_lines = max(1, target_bytes // AVG_LINE_BYTES - incident_lines)
    # lines per second at full speed, so the whole day adds up to total_lines
    avg_factor = sum(daily_factor(s) for s in range(0, SECONDS_PER_DAY, 60)) / (SECONDS_PER_DAY / 60)
    peak_rate = total_lines / (SECONDS_PER_DAY * avg_factor)

    incident_at = {}
    for inc in INCIDENTS:
        for s in inc.seconds():
            incident_at[s] = inc

    day_start = datetime(day.year, day.month, day.day)
    lines_written = errors = 0
    started = time.perf_counter()

    with open(path, "w", encoding="ascii", newline="\n", buffering=1 << 20) as f:
        for second in range(SECONDS_PER_DAY):
            # How many normal lines this second? Expected value, then round randomly.
            expected = peak_rate * daily_factor(second)
            n = int(expected) + (r.random() < expected - int(expected))
            services = r.choices(SERVICES, SERVICE_WEIGHTS, k=n)
            levels = r.choices(LEVELS, LEVEL_WEIGHTS, k=n)
            events = [(svc, lvl, TEMPLATES[lvl](svc, r)) for svc, lvl in zip(services, levels)]

            inc = incident_at.get(second)
            if inc is not None:
                events += [(inc.service, "ERROR", inc.message)] * inc.errors_per_second
            if not events:
                continue

            # Spread the events over the milliseconds of this second, in order.
            millis = sorted(r.randrange(1000) for _ in events)
            r.shuffle(events)
            prefix = (day_start + timedelta(seconds=second)).strftime("%Y-%m-%d %H:%M:%S")
            chunk = []
            for ms, (svc, lvl, msg) in zip(millis, events):
                chunk.append(f"{prefix}.{ms:03d} {lvl:<5} {svc:<8} {msg}\n")
                errors += lvl == "ERROR"
            f.write("".join(chunk))
            lines_written += len(chunk)

    elapsed = time.perf_counter() - started
    return {"lines": lines_written, "errors": errors, "seconds": round(elapsed, 2)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a realistic, time-sorted log file")
    parser.add_argument("--out", required=True, help="output file, e.g. data/app.log")
    parser.add_argument("--size", default="100MB", help="target size: 10MB, 1GB ...")
    parser.add_argument("--date", default="2026-11-19", help="day of the logs, YYYY-MM-DD")
    parser.add_argument("--seed", type=int, default=42, help="same seed = same file")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    stats = generate(args.out, parse_size(args.size), date.fromisoformat(args.date), args.seed)
    size_mb = os.path.getsize(args.out) / 1024**2
    print(f"{args.out}: {size_mb:.1f} MB, {stats['lines']:,} lines, "
          f"{stats['errors']:,} errors, {stats['seconds']} s")


if __name__ == "__main__":
    main()
