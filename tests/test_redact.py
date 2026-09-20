from flight_fare_scraper import redact
from flight_fare_scraper.config import build_query

QUERY = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false")
OTHER = build_query("MIA", "TYO", "2026-11-27", "2026-12-14", nonstop="false")


def test_label_is_readable_when_redaction_is_off(monkeypatch):
    monkeypatch.delenv(redact.REDACT_ENV, raising=False)
    assert redact.label(QUERY) == QUERY.label
    assert "MIA" in redact.label(QUERY)


def test_label_hides_the_route_when_redaction_is_on(monkeypatch):
    monkeypatch.setenv(redact.REDACT_ENV, "1")
    label = redact.label(QUERY)
    assert label.startswith("route-")
    for secret in ("MIA", "TYO", "2026-11-27", "2026-12-13"):
        assert secret not in label


def test_route_ids_are_stable_and_distinct(monkeypatch):
    monkeypatch.setenv(redact.REDACT_ENV, "1")
    assert redact.label(QUERY) == redact.label(QUERY)
    assert redact.label(QUERY) != redact.label(OTHER)


def test_the_salt_changes_the_id(monkeypatch):
    monkeypatch.setenv(redact.REDACT_ENV, "1")
    monkeypatch.setenv(redact.SALT_ENV, "one")
    salted = redact.label(QUERY)
    monkeypatch.setenv(redact.SALT_ENV, "two")
    assert redact.label(QUERY) != salted


def test_only_explicit_truthy_values_enable_redaction(monkeypatch):
    for value in ("0", "false", "no", ""):
        monkeypatch.setenv(redact.REDACT_ENV, value)
        assert not redact.enabled()
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(redact.REDACT_ENV, value)
        assert redact.enabled()
