from datetime import date

import pytest
from fastapi.testclient import TestClient

from logseek.generator import generate
from logseek.web import create_app

DAY = date(2026, 11, 19)


@pytest.fixture(scope="module")
def api(tmp_path_factory):
    path = tmp_path_factory.mktemp("web") / "app.log"
    generate(str(path), 3 * 1024**2, DAY, seed=9)
    with TestClient(create_app(str(path))) as c:
        yield c


def test_info(api):
    info = api.get("/api/info").json()
    assert info["file"] == "app.log" and info["day"] == "2026-11-19"
    assert info["first"].startswith("2026-11-19 00:") and info["last"].startswith("2026-11-19 23:")


def test_day_histogram_is_continuous_and_shows_incident(api):
    data = api.get("/api/histogram", params={"buckets": 96}).json()
    rows = data["buckets"]
    assert len(rows) == 96 and rows[0]["first"] == 0
    assert all(a["last"] == b["first"] for a, b in zip(rows, rows[1:]))
    by_start = {r["start"][11:16]: r["errors"] for r in rows}
    normal = sorted(by_start.values())[48]  # median bucket
    assert by_start["14:00"] > normal + 3000  # the 14:05 incident (12 errors/s for 5 min)


def test_histogram_without_errors(api):
    rows = api.get("/api/histogram", params={"buckets": 24, "errors": "false"}).json()["buckets"]
    assert all(r["errors"] is None for r in rows)


def test_zoomed_histogram(api):
    data = api.get("/api/histogram", params={"start": "14:00", "end": "14:15", "buckets": 15}).json()
    assert data["buckets"][0]["start"] == "2026-11-19 14:00:00.000"
    assert data["buckets"][-1]["end"] == "2026-11-19 14:15:00.000"
    minute = {r["start"][11:16]: r["errors"] for r in data["buckets"]}
    assert minute["14:06"] > 10 * minute["14:12"] / 2  # incident minute vs quiet minute


def test_histogram_rejects_bad_input(api):
    assert api.get("/api/histogram", params={"start": "2pm"}).status_code == 422
    assert api.get("/api/histogram", params={"start": "15:00", "end": "14:00"}).status_code == 422
    assert api.get("/api/histogram", params={"buckets": 0}).status_code == 422


def test_search_returns_entries_stats_and_jumps(api):
    r = api.get("/api/search", params={"start": "14:05", "end": "14:10", "level": "ERROR",
                                        "service": "orders", "limit": 50}).json()
    assert len(r["entries"]) == 50 and r["stats"]["limited"]
    assert all(e["level"] == "ERROR" and e["service"] == "orders" for e in r["entries"])
    assert r["entries"][0]["timestamp"] >= "2026-11-19 14:05"
    probes = r["probes"]
    assert probes and probes[0]["lo"] == 0 and all(p["lo"] <= p["pos"] < p["hi"] for p in probes)
    assert r["stats"]["jumps"] >= len(probes)


def test_search_grep_and_no_matches(api):
    r = api.get("/api/search", params={"start": "21:40", "end": "21:43", "grep": "token signature"}).json()
    assert r["stats"]["matched"] == 200  # limit reached: the incident has 1,620 such lines
    empty = api.get("/api/search", params={"start": "03:00", "end": "03:01", "grep": "token signature"}).json()
    assert empty["entries"] == [] and empty["stats"]["matched"] == 0


def test_search_rejects_bad_input(api):
    assert api.get("/api/search", params={"start": "noon"}).status_code == 422
    assert api.get("/api/search", params={"limit": 5000}).status_code == 422
    assert api.get("/api/search", params={"grep": "x" * 101}).status_code == 422


def test_page_is_served(api):
    page = api.get("/")
    assert page.status_code == 200 and "<title>logseek</title>" in page.text
    assert api.get("/app.js").status_code == 200


def test_warm_up_fills_the_day_histogram_cache(tmp_path):
    import threading

    path = tmp_path / "app.log"
    generate(str(path), 1024**2, DAY, seed=3)
    app = create_app(str(path), warm=True)
    for t in threading.enumerate():
        if t.name == "logseek-warm-up":
            t.join(timeout=30)
    with TestClient(app) as c:
        rows = c.get("/api/histogram", params={"buckets": 96}).json()["buckets"]
    assert len(rows) == 96 and all(r["errors"] is not None for r in rows)
