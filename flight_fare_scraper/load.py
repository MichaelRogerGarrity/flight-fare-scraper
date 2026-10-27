"""How hard a run may lean on the site, and how that changes with what the site says.

On 2026-10-26 Kayak answered the home connection with CAPTCHAs after ~185 searches in
21 hours, the last 119 at ~38 an hour. Three controls, all set in the route spec's
optional "load" section (see examples/routes.spec.example.json):

- max_searches_per_hour: a floor on the time between two searches' starts. Pacing can
  spread a run out but never squeeze it past this; a run that falls behind ends at its
  deadline instead of speeding up.
- cooldown_hours: after the site refuses a run -- a CAPTCHA or the circuit breaker --
  no run starts for this long. The first search after it is the probe: a CAPTCHA stops
  the run there and starts a new cooldown.
- ramp: schedule levels, lightest first. A run uses its current level's tiers in place
  of the spec's own. After clean_days_per_step days that each finished without a
  refusal, the next run moves up a level; a refusal moves it down steps_down_on_block
  levels and starts the count again. That finds the volume the site tolerates instead
  of guessing at it.

Cooldown and ramp need somewhere to remember things between runs: a small JSON state
file (`track --load-state`). Without one -- as on the hosted runners, which start
fresh each day -- only the hourly cap applies and the spec's own tiers are used.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

from . import schedule

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Level:
    name: str
    tiers: Tuple[schedule.Tier, ...]


@dataclass(frozen=True)
class Settings:
    max_searches_per_hour: Optional[float] = None
    cooldown_hours: float = 0.0
    clean_days_per_step: int = 7
    steps_down_on_block: int = 1
    levels: Tuple[Level, ...] = ()

    @property
    def min_gap_s(self) -> Optional[float]:
        return 3600.0 / self.max_searches_per_hour if self.max_searches_per_hour else None


def _number(raw: dict, key: str, where: str, default, kind=float, minimum=0):
    value = raw.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ValueError(f"{where}: '{key}' must be a number of at least {minimum}, got {value!r}")
    return kind(value)


def parse(text: str) -> Settings:
    """The route spec's "load" section; defaults (no limits) if it has none."""
    raw = json.loads(text).get("load")
    if raw is None:
        return Settings()
    where = "route spec: load"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object")
    unknown = sorted(set(raw) - {"max_searches_per_hour", "cooldown_hours", "ramp"})
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {unknown}")
    ramp = raw.get("ramp") or {}
    if not isinstance(ramp, dict):
        raise ValueError(f"{where}: 'ramp' must be an object")
    unknown = sorted(set(ramp) - {"clean_days_per_step", "steps_down_on_block", "levels"})
    if unknown:
        raise ValueError(f"{where}.ramp: unknown key(s) {unknown}")
    levels = []
    for index, entry in enumerate(ramp.get("levels", [])):
        level_where = f"{where}.ramp.levels[{index}]"
        if not isinstance(entry, dict) or "tiers" not in entry:
            raise ValueError(f"{level_where} must be an object with 'tiers'")
        levels.append(Level(str(entry.get("name", index)), schedule.parse_tiers(entry["tiers"], f"{level_where}.tiers")))
    return Settings(
        max_searches_per_hour=_number(raw, "max_searches_per_hour", where, None, minimum=0.1),
        cooldown_hours=_number(raw, "cooldown_hours", where, 0.0),
        clean_days_per_step=_number(ramp, "clean_days_per_step", f"{where}.ramp", 7, int, 1),
        steps_down_on_block=_number(ramp, "steps_down_on_block", f"{where}.ramp", 1, int, 0),
        levels=tuple(levels),
    )


@dataclass
class State:
    level: int = 0                         # index into the ramp's levels
    clean_days: int = 0                    # clean days finished at this level
    last_clean_day: Optional[str] = None   # so a day resumed twice counts once
    refused_at: Optional[str] = None       # local time of the last refusal, ISO 8601
    refusal: str = ""                      # what it was: "captcha" or "breaker"
    history: list = field(default_factory=list)  # the last few level changes, newest last

    @classmethod
    def read(cls, path: str) -> "State":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        return cls(**{key: raw[key] for key in asdict(cls()) if key in raw})

    def write(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    def _note(self, now: datetime, text: str) -> str:
        self.history = (self.history + [f"{now:%Y-%m-%d %H:%M} {text}"])[-20:]
        return text


def cooldown_until(settings: Settings, state: State, now: datetime) -> Optional[datetime]:
    """When the cooldown after the last refusal ends, if it hasn't yet."""
    if not settings.cooldown_hours or not state.refused_at:
        return None
    until = datetime.fromisoformat(state.refused_at) + timedelta(hours=settings.cooldown_hours)
    return until if now < until else None


def current_level(settings: Settings, state: State, now: datetime) -> Optional[Level]:
    """The level this run uses, moving up first if enough clean days have been banked."""
    if not settings.levels:
        return None
    top = len(settings.levels) - 1
    state.level = min(max(state.level, 0), top)  # the levels may have been edited since
    if state.clean_days >= settings.clean_days_per_step and state.level < top:
        state.level += 1
        state.clean_days = 0
        logger.info("RAMP: %s", state._note(now, f"up to level {state.level} ({settings.levels[state.level].name}) "
                                                 f"after {settings.clean_days_per_step} clean day(s)"))
    return settings.levels[state.level]


def record(settings: Settings, state: State, day: date, stopped: str, refused: bool, now: datetime) -> None:
    """Fold a finished run into the state: a refusal steps down, a clean day counts up."""
    if refused:
        state.refused_at = now.isoformat(timespec="seconds")
        state.refusal = stopped
        state.clean_days = 0
        if settings.levels:
            before = state.level
            state.level = max(0, state.level - settings.steps_down_on_block)
            logger.warning("RAMP: %s", state._note(
                now, f"{stopped} at level {before}; down to level {state.level} "
                     f"({settings.levels[state.level].name}), cooling down {settings.cooldown_hours:g}h"))
        else:
            state._note(now, f"{stopped}; cooling down {settings.cooldown_hours:g}h")
    elif not stopped and state.last_clean_day != day.isoformat():
        state.clean_days += 1
        state.last_clean_day = day.isoformat()
        if settings.levels:
            logger.info("RAMP: clean day %d of %d at level %d", state.clean_days, settings.clean_days_per_step,
                        state.level)
