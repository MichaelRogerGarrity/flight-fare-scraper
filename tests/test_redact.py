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


def test_timeout_detail_survives_redaction(monkeypatch):
    # Which page failed, and whether any response arrived, is the whole diagnosis:
    # a silent block looks like no response on page 1, a slow site like a stuck page.
    monkeypatch.setenv(redact.REDACT_ENV, "1")
    text = "SearchTimeoutError: route-3f2a91c4b0de: page 3 still 'first-phase' after 45s"
    assert redact.error(text) == text


def test_other_errors_are_cut_to_their_type(monkeypatch):
    monkeypatch.setenv(redact.REDACT_ENV, "1")
    assert redact.error("BotBlockedError: route-x: redirected to https://www.kayak.com/help/bots.html") == "BotBlockedError"
    assert redact.error("Error: page.goto: Timeout 45000ms exceeded navigating to https://www.kayak.com/flights/MIA-TYO/") == "Error"


def test_safe_types_are_still_scrubbed_of_urls_and_routes(monkeypatch):
    monkeypatch.setenv(redact.REDACT_ENV, "1")
    scrubbed = redact.error("SearchTimeoutError: MIA-TYO page 1 at https://www.kayak.com/flights/MIA-TYO/2027")
    assert "MIA" not in scrubbed and "kayak" not in scrubbed
    assert scrubbed.startswith("SearchTimeoutError")


def test_detail_is_untouched_when_redaction_is_off(monkeypatch):
    monkeypatch.delenv(redact.REDACT_ENV, raising=False)
    text = "BotBlockedError: MIA-TYO: redirected to https://www.kayak.com/help/bots.html"
    assert redact.error(text) == text
