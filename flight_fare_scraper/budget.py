"""Worst-case daily load for a route spec, before it ever runs.

Runs the real scheduler over every day of a period and reports what each day would
ask of the site: searches, page requests, and runtime. The searches-per-day figure is
the one to watch -- on 2026-10-22 a spec that roughly doubled it to ~780 got the
GitHub runners blocked within a day, after a month at ~400 had been clean -- and it is
the worst day, not the average, that matters.

Costs per search can be calibrated from the private per-search records; otherwise
they default to what those records measured in October 2026.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from . import schedule

JITTER_S = 12.5  # mean pause between searches


@dataclass
class Cost:
    """What one search on a route costs."""
    seconds: float
    pages: float               # result pages fetched in the main search
    nonstop_share: float       # how often the nonstop-only search also runs
    nonstop_pages: float       # pages that search fetches when it does

    @property
    def requests(self) -> float:
        """Page loads plus "show more" clicks: one load, a click per further page, and
        the same again for the nonstop search when it runs."""
        return self.pages + self.nonstop_share * self.nonstop_pages


# Measured 2026-10-13..21 (records of successful searches). A route searched nonstop-only
# never needs a second search; a connecting one nearly always does on long-haul.
DEFAULT_NONSTOP_ONLY = Cost(seconds=30.0, pages=3.0, nonstop_share=0.0, nonstop_pages=0.0)
DEFAULT_CONNECTING = Cost(seconds=71.0, pages=5.0, nonstop_share=1.0, nonstop_pages=1.2)


@dataclass
class Day:
    day: date
    searches: int = 0
    requests: float = 0.0
    seconds: float = 0.0
    by_route: Counter = field(default_factory=Counter)
    by_tier: Counter = field(default_factory=Counter)


@dataclass
class Report:
    days: List[Day]
    weekday_coverage: Optional[float]  # share of windows checked 7+ times that hit all 7 weekdays
    # Windows checked repeatedly yet always on the same weekday -- what an interval in
    # whole weeks produces, and what this project's rule against them exists to prevent.
    stuck_on_one_weekday: int = 0

    @property
    def worst(self) -> Day:
        return max(self.days, key=lambda d: d.searches)

    def over(self, limit: int) -> int:
        return sum(d.searches > limit for d in self.days)

    def average(self, attribute: str) -> float:
        return sum(getattr(d, attribute) for d in self.days) / len(self.days)


def load_costs(records_glob: str) -> Dict[Tuple[str, str, bool], Cost]:
    """Average cost per (origin, destination, nonstop_only) from per-search records."""
    import duckdb  # only needed when calibrating
    rows = duckdb.sql(f"""
        SELECT origin, destination, nonstop_only, avg(seconds), avg(pages_fetched),
               avg(nonstop_pass::INT), coalesce(avg(nonstop_pages_fetched), 0)
        FROM read_parquet('{records_glob}', union_by_name = true)
        WHERE outcome <> 'failed' GROUP BY ALL
    """).fetchall()
    return {(o, d, bool(n)): Cost(s, p or 1.0, share or 0.0, np or 0.0) for o, d, n, s, p, share, np in rows}


def _tier_name(spec: schedule.Spec, days_out: int) -> str:
    tier = schedule.tier_for(spec, days_out)
    return f"<= {tier.max_days_out}d every {tier.every_days}d" if tier else "outside"


def simulate(spec: schedule.Spec, start: date, days: int,
             costs: Optional[Dict[Tuple[str, str, bool], Cost]] = None) -> Report:
    costs = costs or {}
    out: List[Day] = []
    checks: Dict[tuple, set] = {}
    counts: Counter = Counter()
    for offset in range(days):
        today = start + timedelta(days=offset)
        tally = Day(today)
        for query in schedule.due_today(spec, today):
            cost = costs.get((query.origin, query.destination, query.nonstop_only)) or (
                DEFAULT_NONSTOP_ONLY if query.nonstop_only else DEFAULT_CONNECTING)
            tally.searches += 1
            tally.requests += cost.requests
            tally.seconds += cost.seconds + JITTER_S
            tally.by_route[f"{query.origin}-{query.destination}"] += 1
            tally.by_tier[_tier_name(spec, (query.depart_date - today).days)] += 1
            key = (query.origin, query.destination, query.depart_date, query.return_date)
            checks.setdefault(key, set()).add(today.weekday())
            counts[key] += 1
        out.append(tally)
    often = [key for key, n in counts.items() if n >= 7]
    coverage = (sum(len(checks[key]) == 7 for key in often) / len(often)) if often else None
    stuck = sum(1 for key, n in counts.items() if n >= 3 and len(checks[key]) == 1)
    return Report(out, coverage, stuck)


def render(report: Report, limit: int, target_hours: float = 14.0) -> str:
    worst = report.worst
    lines = [
        f"searches/day   average {report.average('searches'):6.0f}   worst {worst.searches:5d} on {worst.day}"
        f"   days over {limit}: {report.over(limit)} of {len(report.days)}",
        f"requests/day   average {report.average('requests'):6.0f}   worst {max(d.requests for d in report.days):5.0f}"
        f"   (page loads plus 'show more' clicks)",
        f"runtime/day    average {report.average('seconds') / 3600:6.1f}h  worst {max(d.seconds for d in report.days) / 3600:5.1f}h"
        f"   (searching only; add the start delay)",
    ]
    # Pacing spreads the searches to end about target_hours after the run starts. What
    # matters is the gap that leaves between searches, especially on the busiest day.
    target = target_hours * 3600
    def gap(d: Day) -> float:
        return max(0.0, (target - d.seconds) / d.searches) if d.searches else 0.0
    lines.append(f"paced to {target_hours:g}h  searches spaced ~{sum(gap(d) for d in report.days) / len(report.days):.0f}s apart"
                 f" on average, ~{gap(worst):.0f}s on the busiest day")
    if report.weekday_coverage is not None:
        lines.append(f"weekday spread {report.weekday_coverage:.0%} of windows checked 7+ times were checked on all 7 weekdays;"
                     f" {report.stuck_on_one_weekday} checked 3+ times were stuck on one weekday")
    lines.append(f"worst day by tier:  " + ", ".join(f"{k} {v}" for k, v in worst.by_tier.most_common()))
    lines.append(f"worst day by route: " + ", ".join(f"{k} {v}" for k, v in worst.by_route.most_common()))
    return "\n".join(lines)
