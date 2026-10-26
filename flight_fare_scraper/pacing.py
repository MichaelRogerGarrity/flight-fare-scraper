"""Spreading a run's searches across the day instead of firing them back to back.

A run of ~250 searches takes about six hours of actual searching, and with only the
5-20 second jitter between them it used that six hours and left the rest of the day
idle. Whether the site's tolerance is per hour or per day isn't known; if it is per
hour, the same searches spread over fourteen hours are gentler, and if it is per day,
spreading them costs nothing.

The pacer aims a whole run -- this shard and the ones still to come -- at a target end
time. Before each search it splits the time left between the searches left, takes out
what the searches themselves are measured to take, and pauses for the rest, randomised
so the timing isn't mechanical. Because it re-measures as it goes, a slow earlier shard
leaves later ones pacing faster rather than overrunning.
"""

import random
import time
from dataclasses import dataclass, field
from typing import Callable, Tuple

FLOOR_S: Tuple[float, float] = (5.0, 20.0)  # never less than the old fixed jitter
CEILING_S = 600.0                          # never idle more than ten minutes at a stretch
DEFAULT_WORK_S = 60.0                      # a search's own time, until this shard has measured some
SPREAD = (0.5, 1.5)                        # each pause is the computed mean times a random factor


@dataclass
class Pacer:
    until: float          # when the whole run should finish, seconds since the epoch
    later_searches: int   # searches in the shards that run after this one
    job_until: float      # when this shard must be done, to stay inside the job time limit
    clock: Callable[[], float] = time.time
    work_s: float = DEFAULT_WORK_S
    _measured: list = field(default_factory=list)

    def observe(self, seconds: float) -> None:
        """Record how long a search itself took, to refine the estimate."""
        self._measured.append(seconds)
        self.work_s = sum(self._measured) / len(self._measured)

    def mean_pause(self, remaining_here: int) -> float:
        """The pause that lands this shard on its share of the time left."""
        if remaining_here <= 0:
            return FLOOR_S[0]
        now = self.clock()
        total_left = remaining_here + self.later_searches
        share = (self.until - now) * remaining_here / total_left  # this shard's fair share
        share = min(share, self.job_until - now)                  # but never past the job limit
        mean = (share - remaining_here * self.work_s) / remaining_here
        return max(sum(FLOOR_S) / 2, min(CEILING_S, mean))

    def pause(self, remaining_here: int, rng: random.Random = random) -> float:
        mean = self.mean_pause(remaining_here)
        return max(FLOOR_S[0], mean * rng.uniform(*SPREAD))
