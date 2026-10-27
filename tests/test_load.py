"""Load controls: an hourly cap on searches, a cooldown after a refusal, and a ramp."""

import json
from datetime import date, datetime, timedelta

import pytest

from flight_fare_scraper import cli, load
from flight_fare_scraper.config import build_query
from flight_fare_scraper.runner import STOPPED_CAPTCHA, BatchReport, run_queries, search_record
from tests.factories import make_result
from tests.test_runner import ScriptedScraper

NOW = datetime(2026, 10, 27, 9, 0)


def _tiers(far_every):
    return [{"max_days_out": 30, "every_days": 1, "day_offsets": [0]},
            {"max_days_out": 365, "every_days": far_every, "day_offsets": [0]}]


def _spec(**load_section):
    return {"routes": [{"origin": "MIA", "destination": "TYO", "nights": 16, "nonstop": False}],
            "tiers": _tiers(9), "load": load_section}


RAMP = {"clean_days_per_step": 2, "steps_down_on_block": 1,
        "levels": [{"name": "light", "tiers": _tiers(9)}, {"name": "mid", "tiers": _tiers(6)},
                   {"name": "full", "tiers": _tiers(3)}]}


def _settings(**overrides):
    return load.parse(json.dumps(_spec(**{"max_searches_per_hour": 15, "cooldown_hours": 36, "ramp": RAMP,
                                          **overrides})))


# --- settings ----------------------------------------------------------------------------

def test_a_spec_without_a_load_section_has_no_limits():
    settings = load.parse(json.dumps({"routes": []}))
    assert settings.min_gap_s is None and settings.cooldown_hours == 0 and settings.levels == ()


def test_the_load_section_is_read():
    settings = _settings()
    assert settings.min_gap_s == 240
    assert [level.name for level in settings.levels] == ["light", "mid", "full"]
    assert settings.levels[0].tiers[1].every_days == 9


@pytest.mark.parametrize("section, message", [
    ({"max_searches_per_hour": -1}, "max_searches_per_hour"),
    ({"cooldown": 5}, "unknown key"),
    ({"ramp": {"levels": [{"name": "x", "tiers": _tiers(7)}]}}, "multiple of 7"),  # same rules as the spec's tiers
])
def test_bad_settings_are_refused(section, message):
    with pytest.raises(ValueError, match=message):
        load.parse(json.dumps(_spec(**section)))


# --- cooldown ------------------------------------------------------------------------------

def test_no_cooldown_without_a_refusal():
    assert load.cooldown_until(_settings(), load.State(), NOW) is None


def test_a_refusal_holds_runs_off_for_the_cooldown():
    state = load.State(refused_at="2026-10-26T21:40:00", refusal="captcha")
    assert load.cooldown_until(_settings(), state, NOW) == datetime(2026, 10, 28, 9, 40)
    assert load.cooldown_until(_settings(), state, datetime(2026, 10, 28, 9, 41)) is None


# --- ramp ----------------------------------------------------------------------------------

def test_clean_days_move_the_ramp_up_one_level():
    settings, state = _settings(), load.State()
    for day in (date(2026, 10, 27), date(2026, 10, 28)):
        assert load.current_level(settings, state, NOW).name == "light"
        load.record(settings, state, day, "", refused=False, now=NOW)
    assert load.current_level(settings, state, NOW).name == "mid"
    assert state.clean_days == 0 and "up to level 1" in state.history[-1]


def test_a_day_resumed_twice_counts_once():
    settings, state = _settings(), load.State()
    load.record(settings, state, date(2026, 10, 27), "", refused=False, now=NOW)
    load.record(settings, state, date(2026, 10, 27), "", refused=False, now=NOW)
    assert state.clean_days == 1


def test_a_run_cut_short_is_neither_clean_nor_a_refusal():
    settings, state = _settings(), load.State(clean_days=1)
    load.record(settings, state, date(2026, 10, 27), "deadline", refused=False, now=NOW)
    assert state.clean_days == 1 and state.refused_at is None


def test_a_refusal_steps_down_and_starts_the_count_again():
    settings, state = _settings(), load.State(level=2, clean_days=1)
    load.record(settings, state, date(2026, 10, 27), STOPPED_CAPTCHA, refused=True, now=NOW)
    assert state.level == 1 and state.clean_days == 0
    assert state.refusal == "captcha" and state.refused_at == "2026-10-27T09:00:00"


def test_the_ramp_stops_at_the_top_and_bottom():
    settings = _settings()
    top = load.State(level=2, clean_days=9)
    assert load.current_level(settings, top, NOW).name == "full" and top.level == 2
    bottom = load.State(level=0)
    load.record(settings, bottom, date(2026, 10, 27), STOPPED_CAPTCHA, refused=True, now=NOW)
    assert bottom.level == 0


def test_a_level_beyond_an_edited_ramp_is_brought_back_in_range():
    assert load.current_level(_settings(), load.State(level=9), NOW).name == "full"


def test_state_round_trips_and_a_missing_file_is_a_fresh_start(tmp_path):
    path = str(tmp_path / "state.json")
    assert load.State.read(path) == load.State()
    load.State(level=2, clean_days=3, refusal="captcha").write(path)
    assert load.State.read(path).level == 2


# --- the hourly cap, in the runner ---------------------------------------------------------

QUERIES = [build_query("MIA", "TYO", f"2026-12-{day:02d}", f"2026-12-{day + 16:02d}", nonstop="false")
           for day in range(1, 4)]


class FakeClock:
    """Time that moves only when the runner sleeps, plus a fixed cost per search."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_the_hourly_cap_spaces_search_starts_at_least_the_gap_apart():
    clock = FakeClock()
    scraper = ScriptedScraper({q.label: [[make_result()]] for q in QUERIES})
    original = scraper.search

    def slow_search(query):
        clock.now += 60  # each search takes a minute
        return original(query)
    scraper.search = slow_search
    starts = []
    run_queries(QUERIES, sleep=clock.sleep, clock=clock, scraper_for=lambda site: lambda headless: scraper,
                search_jitter_s=(5.0, 5.0), min_gap_s=240,
                on_search=lambda q, r, rec: starts.append(clock.now - 60))
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(240 <= gap <= 300 for gap in gaps)  # the floor, plus up to 25% on top


def test_without_a_cap_pauses_are_unchanged():
    clock = FakeClock()
    scraper = ScriptedScraper({q.label: [[make_result()]] for q in QUERIES})
    run_queries(QUERIES, sleep=clock.sleep, clock=clock, scraper_for=lambda site: lambda headless: scraper,
                search_jitter_s=(5.0, 5.0))
    assert clock.now == 10.0


# --- end to end ----------------------------------------------------------------------------

def _spec_file(tmp_path, **load_section):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(_spec(**load_section)))
    return str(path)


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(cli, "configure_logging", lambda verbose, log_file: None)


def _argv(tmp_path, spec, state):
    return ["track", "--spec", spec, "--today", "2026-10-27", "--db", str(tmp_path / "x.duckdb"),
            "--load-state", str(state)]


def test_a_run_during_the_cooldown_searches_nothing_and_exits_3(tmp_path, monkeypatch, quiet):
    state = tmp_path / "state.json"
    load.State(refused_at=(datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),
               refusal="captcha").write(str(state))
    searched = []
    monkeypatch.setattr(cli, "run_queries", lambda queries, **_: searched.append(queries) or BatchReport())
    assert cli.main(_argv(tmp_path, _spec_file(tmp_path, cooldown_hours=36, ramp=RAMP), state)) == 3
    assert searched == []


def test_a_run_uses_its_ramp_level_and_the_cap_and_banks_a_clean_day(tmp_path, monkeypatch, quiet):
    state = tmp_path / "state.json"
    seen = {}

    def fake_run(queries, **options):
        seen["count"], seen["gap"] = len(queries), options["min_gap_s"]
        return BatchReport()

    monkeypatch.setattr(cli, "run_queries", fake_run)
    assert cli.main(_argv(tmp_path, _spec_file(tmp_path, max_searches_per_hour=12, ramp=RAMP), state)) == 0
    light = seen["count"]
    assert seen["gap"] == 300
    assert json.loads(state.read_text())["clean_days"] == 1

    load.State(level=2).write(str(state))  # at the "full" level the far tier comes round every 3 days
    cli.main(_argv(tmp_path, _spec_file(tmp_path, ramp=RAMP), state))
    assert seen["count"] > light


def test_a_captcha_run_steps_the_ramp_down_and_starts_a_cooldown(tmp_path, monkeypatch, quiet):
    state = tmp_path / "state.json"
    load.State(level=1).write(str(state))

    def fake_run(queries, **options):
        return BatchReport(stopped=STOPPED_CAPTCHA,
                           records=[search_record(queries[0], None, 1, 3.0, 0, None)])

    monkeypatch.setattr(cli, "run_queries", fake_run)
    assert cli.main(_argv(tmp_path, _spec_file(tmp_path, cooldown_hours=36, ramp=RAMP), state)) == 1
    saved = json.loads(state.read_text())
    assert saved["level"] == 0 and saved["refusal"] == "captcha" and saved["refused_at"]


def test_without_a_state_file_only_the_cap_applies(tmp_path, monkeypatch, quiet):
    seen = {}
    monkeypatch.setattr(cli, "run_queries", lambda queries, **options: seen.update(gap=options["min_gap_s"])
                        or BatchReport())
    cli.main(["track", "--spec", _spec_file(tmp_path, max_searches_per_hour=15, cooldown_hours=36, ramp=RAMP),
              "--today", "2026-10-27", "--db", str(tmp_path / "x.duckdb")])
    assert seen["gap"] == 240
