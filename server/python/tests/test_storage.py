"""Storage tests: migrations, spool mode and loader, concurrent writers."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3

import pytest

from conftest import BASE_TOML, envelope, query_db
from tidesight.storage import (EVENTS_MIGRATIONS, REPLAY_MIGRATIONS, ChunkRecord, HitRecord, RequestRecord,
                               SpoolStore, connect, load_spool, migrate)


def test_migrations_are_idempotent(tmp_path):
    path = str(tmp_path / "events.db")
    conn = connect(path)
    assert migrate(conn, EVENTS_MIGRATIONS) == EVENTS_MIGRATIONS[-1][0]
    assert migrate(conn, EVENTS_MIGRATIONS) == EVENTS_MIGRATIONS[-1][0]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"raw_requests", "raw_hits", "rate_limits", "counters", "spool_progress", "meta"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    conn = connect(str(tmp_path / "replay.db"), journal_mode="delete")
    migrate(conn, REPLAY_MIGRATIONS)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    conn.close()


def test_migrations_survive_a_lost_user_version(tmp_path):
    # A database exported from Cloudflare D1 has the tables but not the user_version.
    conn = connect(str(tmp_path / "events.db"))
    migrate(conn, EVENTS_MIGRATIONS)
    conn.execute("PRAGMA user_version=0")
    assert migrate(conn, EVENTS_MIGRATIONS) == EVENTS_MIGRATIONS[-1][0]
    assert [r[1] for r in conn.execute("PRAGMA table_info(raw_requests)")].count("geo") == 1
    conn.close()


def test_readonly_connection(tmp_path):
    path = str(tmp_path / "events.db")
    conn = connect(path)
    migrate(conn, EVENTS_MIGRATIONS)
    conn.close()
    ro = connect(path, readonly=True)
    assert ro.execute("SELECT COUNT(*) FROM raw_hits").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO meta VALUES ('a', 'b')")
    ro.close()


def _request(n_hits: int = 2, ms: int = 1728380000000) -> RequestRecord:
    return RequestRecord(received_at=ms, endpoint="collect", method="POST", site_id="example", site_status="ok",
                         ip="203.0.113.1", headers={"user-agent": "UA"}, cookies={"kept": {}, "others": 0},
                         envelope={"v": 1}, body=b"\x00raw", proto_v=1,
                         hits=[HitRecord(seq=i, data=json.dumps({"t": "event", "i": i}), type="event",
                                         event_id="e%d" % i) for i in range(n_hits)])


def test_record_round_trip():
    rec = _request()
    again = RequestRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert again.to_dict() == rec.to_dict() and again.body == b"\x00raw"
    chunk = ChunkRecord(received_at=1, kind="replay", site_id="s", site_status="ok", payload=b"\x1f\x8bxx",
                        encoding="gzip", meta={"a": 1}, seq=3)
    assert ChunkRecord.from_dict(json.loads(json.dumps(chunk.to_dict()))).to_dict() == chunk.to_dict()


def test_spool_store_and_loader_idempotent(tmp_path):
    data_dir = str(tmp_path)
    spool = SpoolStore(data_dir)
    day1 = 1728380000000  # 2024-10-08
    for i in range(3):
        spool.write_request(_request(ms=day1 + i))
    spool.write_chunk(ChunkRecord(received_at=day1, kind="replay", site_id="example", site_status="ok",
                                  payload=b"\x1f\x8bdata", encoding="gzip", meta={}, seq=0))
    spool.record_counter("2024-10-08", "example", "dropped_dnt")
    spool.close()
    now = day1 + 3600 * 1000
    stats = load_spool(data_dir, now_ms=now)
    assert stats["requests"] == 3 and stats["chunks"] == 1 and stats["counters"] == 1 and stats["corrupt"] == 0
    stats = load_spool(data_dir, now_ms=now)  # nothing new
    assert stats["requests"] == 0 and stats["chunks"] == 0
    ev = os.path.join(data_dir, "events.db")
    assert query_db(ev, "SELECT COUNT(*) AS n FROM raw_requests")[0]["n"] == 3
    assert query_db(ev, "SELECT COUNT(*) AS n FROM raw_hits")[0]["n"] == 6
    assert query_db(ev, "SELECT body FROM raw_requests LIMIT 1")[0]["body"] == b"\x00raw"
    # Lines appended later are picked up from the saved offset.
    spool = SpoolStore(data_dir)
    spool.write_request(_request(ms=day1 + 10))
    spool.close()
    assert load_spool(data_dir, now_ms=now)["requests"] == 1
    # Days later the files are complete, marked done and deleted.
    later = day1 + 3 * 86400000
    stats = load_spool(data_dir, now_ms=later)
    assert stats["files_done"] == 2 and stats["files_deleted"] == 2
    assert query_db(ev, "SELECT COUNT(*) AS n FROM raw_requests")[0]["n"] == 4


def test_spool_loader_partial_and_corrupt_lines(tmp_path):
    data_dir = str(tmp_path)
    day_dir = tmp_path / "spool" / "events" / "2024-10-08"
    day_dir.mkdir(parents=True)
    good = json.dumps({"k": "request", "rec": _request().to_dict()})
    with open(day_dir / "host-1.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(good + "\n{broken json\n" + good[:40])
    stats = load_spool(data_dir, now_ms=1728380000000)
    assert stats["requests"] == 1 and stats["corrupt"] == 1
    ev = os.path.join(data_dir, "events.db")
    rows = query_db(ev, "SELECT error, body FROM raw_requests ORDER BY id")
    assert rows[1]["error"].startswith("spool_corrupt") and rows[1]["body"] == b"{broken json"
    # The partial last line is completed later and then loaded.
    with open(day_dir / "host-1.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(good[40:] + "\n")
    assert load_spool(data_dir, now_ms=1728380000000)["requests"] == 1


def test_collector_in_spool_mode(make_harness):
    h = make_harness(storage_mode="spool")
    assert h.post(envelope()).code == 204
    assert h.call("GET", "/p.gif", query="site=example").code == 200
    assert not os.path.exists(os.path.join(h.data_dir, "events.db"))
    h.close()
    stats = load_spool(h.data_dir)
    assert stats["requests"] == 2
    hits = h.events("SELECT type, site_status FROM raw_hits h JOIN raw_requests r ON r.id = h.request_id")
    assert sorted(x["type"] for x in hits) == ["pageview", "pageview"]


def test_spool_rate_limit_in_memory(make_harness):
    h = make_harness(storage_mode="spool", rate=2)
    assert [h.post(envelope()).code for _ in range(3)] == [204, 204, 429]


@pytest.mark.parametrize("journal_mode", ["wal", "delete"])
def test_concurrent_writers(tmp_path, journal_mode):
    from mp_helpers import collector_worker
    data_dir = str(tmp_path / "data").replace("\\", "/")
    text = BASE_TOML.format(data_dir=data_dir, rate=0, storage_mode="sqlite", journal_mode=journal_mode)
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    procs, per = 4, 60
    ps = [ctx.Process(target=collector_worker, args=(text, str(tmp_path / "t.toml"), w, per, q))
          for w in range(procs)]
    for p in ps:
        p.start()
    results = [q.get(timeout=120) for _ in ps]
    for p in ps:
        p.join(30)
    assert all(code == 204 for _w, codes in results for code in codes)
    ev = os.path.join(data_dir, "events.db")
    assert query_db(ev, "PRAGMA integrity_check")[0]["integrity_check"] == "ok"
    assert query_db(ev, "SELECT COUNT(*) AS n FROM raw_requests")[0]["n"] == procs * per
    assert query_db(ev, "SELECT COUNT(DISTINCT event_id) AS n FROM raw_hits")[0]["n"] == procs * per * 3
    assert not os.path.exists(os.path.join(data_dir, "spool"))  # no fallback was needed
