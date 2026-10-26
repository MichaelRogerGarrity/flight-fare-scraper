"""Publishing through a machine's Hugging Face login instead of S3 keys."""

import json
import re
import sys
import types

import pytest

from flight_fare_scraper import cli, storage
from flight_fare_scraper.runner import BatchReport, search_record
from flight_fare_scraper.config import build_query
from tests.factories import make_result

QUERY = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false")
PART = re.compile(r"^(snapshots|diagnostics)/snapshot_date=2026-11-01/home-1-\d{6}-\d{6}\.parquet$")


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


def _searching(error=None):
    """A stand-in for the runner that reports each search as it finishes, as the real one does."""
    def fake_run(queries, **options):
        report = BatchReport()
        for query in queries:
            record = search_record(query, None, 1, 40.0, 0, error)
            results = [] if error else [make_result(depart_date=str(query.depart_date),
                                                    return_date=str(query.return_date))]
            report.records.append(record)
            options["on_search"](query, results, record)
        return report
    return fake_run


def test_a_run_publishes_its_fares_and_records_in_the_day_partition(tmp_path, monkeypatch, uploads):
    monkeypatch.setenv(storage.HF_BUCKET_ENV, "someone/bucket")
    monkeypatch.setattr(cli, "run_queries", _searching())
    code = cli.main(["track", "--spec", _spec(tmp_path), "--today", "2026-11-01", "--db", str(tmp_path / "home.duckdb"),
                     "--run-id", "home-1", "--snapshot-date", "2026-11-01", "--publish-hf", "--checkpoint-every", "1000", "--log-file", ""])
    assert code == 0
    (bucket, files), = uploads
    assert bucket == "someone/bucket"
    keys = sorted(key for _, key in files)
    assert len(keys) == 2 and all(PART.match(key) for key in keys)
    assert [key.split("/")[0] for key in keys] == ["diagnostics", "snapshots"]


def test_records_are_published_even_when_no_fares_were_recorded(tmp_path, monkeypatch, uploads):
    """A run where every search failed is exactly when its records matter."""
    monkeypatch.setenv(storage.HF_BUCKET_ENV, "someone/bucket")
    monkeypatch.setattr(cli, "run_queries", _searching(RuntimeError("blocked")))
    cli.main(["track", "--spec", _spec(tmp_path), "--today", "2026-11-01", "--db", str(tmp_path / "home.duckdb"),
              "--run-id", "home-1", "--snapshot-date", "2026-11-01", "--publish-hf", "--checkpoint-every", "1000", "--log-file", ""])
    (_, files), = uploads
    assert [key.split("/")[0] for _, key in files] == ["diagnostics"]


def test_a_missing_bucket_fails_before_any_search(tmp_path, monkeypatch, uploads):
    monkeypatch.delenv(storage.HF_BUCKET_ENV, raising=False)
    searched = []
    monkeypatch.setattr(cli, "run_queries", lambda queries, **_: searched.append(queries) or BatchReport())
    code = cli.main(["track", "--spec", _spec(tmp_path), "--today", "2026-11-01", "--db", str(tmp_path / "home.duckdb"),
                     "--run-id", "home-3", "--publish-hf", "--checkpoint-every", "1000", "--log-file", ""])
    assert code == 2
    assert searched == [] and uploads == []


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
