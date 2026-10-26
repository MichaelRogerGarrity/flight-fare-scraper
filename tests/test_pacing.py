"""Spreading a run across the day."""

import json
import random

import pytest

from flight_fare_scraper import cli
from flight_fare_scraper.config import build_query
from flight_fare_scraper.pacing import CEILING_S, FLOOR_S, Pacer
from flight_fare_scraper.runner import run_queries
from tests.factories import make_result
from tests.test_runner import ScriptedScraper

HOUR = 3600.0


def pacer(now=0.0, until=14 * HOUR, later=187, job_hours=5.0, work=60.0):
    clock = {"t": now}
    p = Pacer(until=until, later_searches=later, job_until=now + job_hours * HOUR, clock=lambda: clock["t"], work_s=work)
    return p, clock


def test_a_shard_gets_its_fair_share_of_the_time_left():
    # 249 searches over 14 hours, 62 of them in this shard: its share is ~3.5h, minus
    # 62 searches at a minute each, spread over 62 pauses -- about 2.4 minutes apiece.
    p, _ = pacer(later=187)
    share = 14 * HOUR * 62 / 249
    assert p.mean_pause(62) == pytest.approx((share - 62 * 60) / 62)
    assert 130 < p.mean_pause(62) < 160


def test_pacing_never_pushes_a_shard_past_the_job_limit():
    # One shard holding everything over 14 hours would want ~13 hours: capped at 5.
    p, _ = pacer(later=0, job_hours=5.0)
    assert p.mean_pause(70) == pytest.approx((5 * HOUR - 70 * 60) / 70)


def test_the_floor_and_ceiling_hold():
    late, _ = pacer(now=14 * HOUR)  # already past the target
    assert late.mean_pause(50) == pytest.approx(sum(FLOOR_S) / 2)
    idle, _ = pacer(until=100 * HOUR, later=0, job_hours=100)  # absurdly generous window
    assert idle.mean_pause(3) == CEILING_S


def test_a_slow_start_makes_later_pauses_shorter():
    """The pacer re-measures, so falling behind is made up rather than carried."""
    p, clock = pacer()
    on_time = p.mean_pause(62)
    clock["t"] = 2 * HOUR  # two hours gone, but still 62 to do here
    assert p.mean_pause(62) < on_time


def test_measured_search_time_feeds_the_estimate():
    p, _ = pacer(work=60.0)
    before = p.mean_pause(62)
    for _ in range(5):
        p.observe(120.0)  # searches turn out to take two minutes
    assert p.work_s == 120.0
    assert p.mean_pause(62) < before


def test_each_pause_is_randomised_around_the_mean():
    p, _ = pacer()
    rng = random.Random(7)
    pauses = [p.pause(62, rng) for _ in range(200)]
    mean = p.mean_pause(62)
    assert min(pauses) >= 0.5 * mean - 1e-9 and max(pauses) <= 1.5 * mean + 1e-9
    assert len({round(x) for x in pauses}) > 50  # not one fixed rhythm


def test_the_runner_sleeps_what_the_pacer_says():
    queries = [build_query("MIA", "TYO", f"2027-01-{d:02d}", f"2027-01-{d + 16:02d}", nonstop="false") for d in (1, 2, 3)]
    scraper = ScriptedScraper({q.label: [[make_result()]] for q in queries})

    class FixedPacer:
        def __init__(self):
            self.observed = []

        def pause(self, remaining):
            return 100.0 + remaining

        def observe(self, seconds):
            self.observed.append(seconds)

    fixed, sleeps = FixedPacer(), []
    run_queries(queries, delays=(), sleep=sleeps.append, scraper_for=lambda site: lambda headless: scraper,
                search_jitter_s=(5.0, 20.0), pacer=fixed)
    assert sleeps == [102.0, 101.0]  # before the 2nd and 3rd searches, not the 1st
    assert len(fixed.observed) == 3


def test_plan_publishes_the_pacing_target(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"routes": [{"origin": "MIA", "destination": "TYO", "nights": 16, "nonstop": False}]}))
    out = tmp_path / "github_output"
    assert cli.main(["plan", "--spec", str(spec), "--today", "2026-11-01", "--max-per-shard", "10",
                     "--cron", "37 5 * * *", "--matrix-output", str(out)]) == 0
    values = dict(line.split("=", 1) for line in out.read_text().splitlines())
    from datetime import datetime
    assert datetime.fromisoformat(values["pace_until"]) < datetime.fromisoformat(values["deadline"])
    after = json.loads(values["searches_after"])
    assert len(after) == int(values["count"]) and after[-1] == 0
    assert after == sorted(after, reverse=True)
