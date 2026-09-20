import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple

_BOUND = re.compile(r"^(?:\+(\d{1,2})@)?([01]\d|2[0-3])([0-5]\d)$")


@dataclass(frozen=True)
class TimeWindow:
    """An inclusive takeoff or landing window for one leg.

    Bounds are local wall-clock times, offset in whole days from that leg's
    departure date: long-haul landings often fall a day or two after departure,
    while a takeoff is always on the leg's own departure date.
    """

    start_day: int
    start: time
    end_day: int
    end: time

    def bounds(self, leg_date: date) -> Tuple[datetime, datetime]:
        return (
            datetime.combine(leg_date + timedelta(days=self.start_day), self.start),
            datetime.combine(leg_date + timedelta(days=self.end_day), self.end),
        )

    def contains(self, leg_date: date, moment: datetime) -> bool:
        low, high = self.bounds(leg_date)
        return low <= moment <= high


def parse_window(text: str, allow_day_offset: bool) -> TimeWindow:
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 2:
        shape = "'HHMM,HHMM' or '+N@HHMM,+N@HHMM'" if allow_day_offset else "'HHMM,HHMM'"
        raise ValueError(f"time window {text!r} must look like {shape}")

    bounds = []
    for part in parts:
        match = _BOUND.match(part)
        if not match:
            raise ValueError(f"invalid time {part!r} in window {text!r}")
        day = int(match.group(1) or 0)
        if day and not allow_day_offset:
            raise ValueError(
                f"takeoff window {text!r} can't use a day offset: a leg always takes off on its departure date"
            )
        bounds.append((day, time(int(match.group(2)), int(match.group(3)))))

    (start_day, start), (end_day, end) = bounds
    if (start_day, start) > (end_day, end):
        raise ValueError(f"time window {text!r} ends before it starts")
    return TimeWindow(start_day, start, end_day, end)


def parse_optional_window(text: Optional[str], allow_day_offset: bool) -> Optional[TimeWindow]:
    text = (text or "").strip()
    return parse_window(text, allow_day_offset) if text else None


def kayak_takeoff(window: TimeWindow) -> str:
    return f"{window.start:%H%M},{window.end:%H%M}"


def kayak_landing(window: TimeWindow, leg_date: date) -> str:
    low, high = window.bounds(leg_date)
    return f"{low:%m%d@%H%M},{high:%m%d@%H%M}"
