from dataclasses import dataclass
from datetime import date
from typing import Optional

from .windows import TimeWindow


@dataclass
class SearchQuery:
    # Always 1 adult in economy: every recorded fare is the price of one ticket.
    origin: str
    destination: str
    depart_date: date
    return_date: date
    nonstop_only: bool = True
    outbound_takeoff: Optional[TimeWindow] = None
    return_takeoff: Optional[TimeWindow] = None
    outbound_landing: Optional[TimeWindow] = None
    return_landing: Optional[TimeWindow] = None
    site: str = "kayak"

    @property
    def label(self) -> str:
        return f"{self.origin}-{self.destination} {self.depart_date}/{self.return_date}"


@dataclass
class FlightResult:
    site: str
    origin: str
    destination: str
    depart_date: str
    return_date: str
    price: float  # the fare alone; bag fees are recorded separately and never folded in
    currency: str
    booking_provider: str  # who you'd book through (airline direct, or an OTA like Expedia/Kiwi)
    outbound_airline: Optional[str] = None  # marketing carrier of the leg's first segment
    outbound_operated_by: Optional[str] = None  # set only if a codeshare operates that first segment
    outbound_depart: Optional[str] = None
    outbound_arrive: Optional[str] = None
    outbound_stops: Optional[int] = None
    outbound_duration_min: Optional[int] = None
    outbound_layover_airports: Optional[str] = None  # comma-joined connecting airport(s), if any
    outbound_layover_min: Optional[int] = None  # pure connection time, separate from total duration
    outbound_airport_change: Optional[bool] = None  # a connection departs from a different airport (e.g. land SFO, leave SJC)
    outbound_equipment: Optional[str] = None
    return_airline: Optional[str] = None
    return_operated_by: Optional[str] = None
    return_depart: Optional[str] = None
    return_arrive: Optional[str] = None
    return_stops: Optional[int] = None
    return_duration_min: Optional[int] = None
    return_layover_airports: Optional[str] = None
    return_layover_min: Optional[int] = None
    return_airport_change: Optional[bool] = None
    return_equipment: Optional[str] = None
    cabin_class: Optional[str] = None  # distinct cabins across every segment, e.g. "Economy" or "Economy,First"
    carry_on_status: Optional[str] = None  # Kayak's raw status, e.g. INCLUDED, FEE, UNAVAILABLE (not allowed), UNKNOWN
    carry_on_fee: Optional[float] = None  # 0 when included; None unless the status is INCLUDED or FEE
    checked_bag_status: Optional[str] = None
    checked_bag_fee: Optional[float] = None  # first checked bag
    second_checked_bag_fee: Optional[float] = None
    free_cancellation: Optional[bool] = None
    virtual_interline: Optional[bool] = None  # stitched from airlines that don't interline, i.e. separate tickets
    self_transfer_protection: Optional[bool] = None  # the seller offers protection if a self-transfer connection is missed
    price_prediction: Optional[str] = None  # Kayak's own buy/wait call for this search
    price_prediction_change: Optional[float] = None
    price_prediction_days: Optional[int] = None  # Kayak's daysHorizon for that call
    booking_url: Optional[str] = None  # session-scoped: exported to CSV/JSON/XLSX, never stored in the DB
