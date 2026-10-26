"""Publishing through a machine's Hugging Face login instead of S3 keys."""

import json
import sys
import types

import pytest

from flight_fare_scraper import cli, storage
from flight_fare_scraper.runner import BatchReport, search_record
from flight_fare_scraper.config import build_query
from tests.factories import make_result

QUERY = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false")


@pytest.fixture
def uploads(monkeypatch):
    """Capture what would be uploaded, without touching any real bucket."""
    calls = []
    fake = types.ModuleType("huggingface_hub")
    fake.batch_bucket_files = lambda bucket_id, add: calls.append((bucket_id, [(str(a), k) for a, k in add]))
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    return calls


def _spec(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"routes": [{"origin": "MIA", "destination": "TYO", "nights": 16, "nonstop": False}]}))
    return str(path)


def test_a_run_publishes_its_snapshot_and_records_under_the_usual_keys(tmp_path, monkeypatch, uploads):
    monkeypatch.setenv(storage.HF_BUCKET_ENV, "someone/bucket")
    monkeypatch.setattr(cli, "run_queries", lambda queries, **_: BatchReport(
        results=[make_result()], succeeded=[queries[0]],
        records=[search_record(queries[0], None, 1, 40.0, 0, None)]))
    code = cli.main(["track", "--spec", _spec(tmp_path), "--today", "2026-11-01", "--db", str(tmp_path / "home.duckdb"),
                     "--run-id", "home-1", "--snapshot-date", "2026-11-01", "--publish-hf", "--log-file", ""])
    assert code == 0
    (bucket, files), = uploads
    assert bucket == "someone/bucket"
    keys = sorted(key for _, key in files)
    assert keys == ["diagnostics/snapshot_date=2026-11-01/home-1.parquet",
                    "snapshots/snapshot_date=2026-11-01/home-1.parquet"]


def test_records_are_published_even_when_no_fares_were_recorded(tmp_path, monkeypatch, uploads):
    """A run where every search failed is exactly when its records matter."""
    monkeypatch.setenv(storage.HF_BUCKET_ENV, "someone/bucket")
    monkeypatch.setattr(cli, "run_queries", lambda queries, **_: BatchReport(
        records=[search_record(q, None, 3, 200.0, 0, RuntimeError("blocked")) for q in queries]))
    cli.main(["track", "--spec", _spec(tmp_path), "--today", "2026-11-01", "--db", str(tmp_path / "home.duckdb"),
              "--run-id", "home-2", "--snapshot-date", "2026-11-01", "--publish-hf", "--log-file", ""])
    (_, files), = uploads
    assert [key for _, key in files] == ["diagnostics/snapshot_date=2026-11-01/home-2.parquet"]


def test_the_bucket_comes_only_from_the_environment(tmp_path, monkeypatch, uploads):
    monkeypatch.delenv(storage.HF_BUCKET_ENV, raising=False)
    monkeypatch.setattr(cli, "run_queries", lambda queries, **_: BatchReport(results=[make_result()], succeeded=[queries[0]]))
    code = cli.main(["track", "--spec", _spec(tmp_path), "--today", "2026-11-01", "--db", str(tmp_path / "home.duckdb"),
                     "--run-id", "home-3", "--publish-hf", "--log-file", ""])
    assert code == 1  # recorded locally, but publishing failed loudly
    assert uploads == []


def test_an_earlier_run_can_be_published_afterwards(tmp_path, monkeypatch, uploads):
    from datetime import datetime, date
    from flight_fare_scraper import db
    monkeypatch.setenv(storage.HF_BUCKET_ENV, "someone/bucket")
    path = str(tmp_path / "home.duckdb")
    con = db.connect(path)
    db.insert_snapshot(con, [make_result()], snapshot_at=datetime(2026, 10, 26, 1, 0), snapshot_date=date(2026, 10, 26))
    con.close()
    assert cli.main(["publish-hf", "--db", path, "--snapshot-date", "2026-10-26", "--run-id", "home-0",
                     "--log-file", ""]) == 0
    (_, files), = uploads
    assert [key for _, key in files] == ["snapshots/snapshot_date=2026-10-26/home-0.parquet"]


@pytest.mark.parametrize("key", ["../x.parquet", "/abs.parquet", "a//b.parquet"])
def test_unsafe_keys_are_refused(key, uploads):
    with pytest.raises(storage.StorageError):
        storage.publish_hf([("local.parquet", key)], "someone/bucket")
    assert uploads == []
