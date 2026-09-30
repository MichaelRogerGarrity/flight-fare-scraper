from datetime import date

import pytest

from flight_fare_scraper import storage


def test_snapshot_key_is_hive_partitioned():
    key = storage.snapshot_key(date(2026, 9, 19), "12345-shard0")
    assert key == "snapshots/snapshot_date=2026-09-19/12345-shard0.parquet"


def test_snapshot_key_strips_characters_that_are_illegal_in_object_keys():
    # Bucket keys may not contain "../", "//", or backslashes; the run id is the only
    # part a caller controls, so it must not be able to introduce them.
    key = storage.snapshot_key(date(2026, 9, 19), "run/../7", shard="a b\\c")
    filename = key.rsplit("/", 1)[1]
    assert "/" not in filename and "\\" not in filename
    assert "../" not in key and "//" not in key


def test_uri_tolerates_a_bucket_with_slashes():
    assert storage.uri("/my-bucket/", "a/b.parquet") == "s3://my-bucket/a/b.parquet"


def test_require_env_names_the_missing_variable(monkeypatch):
    monkeypatch.delenv(storage.BUCKET_ENV, raising=False)
    with pytest.raises(storage.StorageError, match=storage.BUCKET_ENV):
        storage.require_env(storage.BUCKET_ENV)


def test_configured_needs_all_three_credentials(monkeypatch):
    for name in (storage.ENDPOINT_ENV, storage.KEY_ID_ENV, storage.SECRET_ENV):
        monkeypatch.delenv(name, raising=False)
    assert not storage.configured()
    monkeypatch.setenv(storage.ENDPOINT_ENV, "s3.hf.co/someone")
    monkeypatch.setenv(storage.KEY_ID_ENV, "HFAK000")
    assert not storage.configured()
    monkeypatch.setenv(storage.SECRET_ENV, "shh")
    assert storage.configured()


def test_sql_literal_escapes_quotes():
    assert storage._sql_literal("it's") == "'it''s'"
