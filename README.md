# logseek

Find any time range in a **multi-gigabyte log file in milliseconds**, without reading the
whole file and without loading it into memory.

Log lines are written in time order, so the file is already sorted. logseek jumps to the
middle **byte** of the file (`seek()`), reads the timestamp of the next full line, and keeps
halving the range: a 5 GB file needs about 33 jumps instead of reading 5 GB.

## Roadmap

- [x] 1. Log generator (realistic day of logs, any size)
- [ ] 2. Core: binary search on bytes
- [ ] 3. Query engine + CLI
- [ ] 4. API + web page (jump animation, histogram)
- [ ] 5. Benchmarks vs `grep` / linear scan
- [ ] 6. Docker, CI, deploy, README

## Run (dev)

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
python -m logseek.generator --out data/app.log --size 100MB
pytest -q
```
