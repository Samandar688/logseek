# logseek

Find any time range in a **multi-gigabyte log file in milliseconds**, without reading the
whole file and without loading it into memory.

Log lines are written in time order, so the file is already sorted. logseek jumps to the
middle **byte** of the file (`seek()`), reads the timestamp of the next full line, and keeps
halving the range: a 5 GB file needs about 33 jumps instead of reading 5 GB.

## Roadmap

- [x] 1. Log generator (realistic day of logs, any size)
- [x] 2. Core: binary search on bytes (1 GB file: 30 jumps, 0.34 ms, 6.5 KB read vs 1.7 s / 468 MB linear)
- [x] 3. Query engine + CLI (stream the window, filter by level/service/text, hourly histogram)
- [ ] 4. API + web page (jump animation, histogram)
- [ ] 5. Benchmarks vs `grep` / linear scan
- [ ] 6. Docker, CI, deploy, README

## Use it

```bash
logseek data/app.log --from 14:05 --to 14:10 --level ERROR --service orders
logseek data/app.log --from 14:05 --to 14:10 --level ERROR --count
logseek data/app.log --from 09:00 --to 10:00 --grep timeout --limit 20
logseek data/app.log --histogram
```

Matching lines go to stdout, a summary goes to stderr. On a 1 GB file:

```
5,162 matches / 85,034 entries in window (5.7 MB of 1,020.7 MB) | binary search 60 jumps 0.28 ms | scan 111 ms
```

## Run (dev)

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
python -m logseek.generator --out data/app.log --size 100MB
pytest -q
```
