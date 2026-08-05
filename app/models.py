"""Dataclasses from SPEC.md §4.

Only the fields step 2 needs are here. The computed fields of `Layover` —
usable_minutes, daylight_minutes, band, score, blocked_reason — arrive with the
steps that compute them.
"""

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Airport:
    iata: str
    city: str
    country_iso2: str
    is_schengen: bool
    verified_on: date | None


@dataclass(frozen=True)
class Hub:
    """An airport with operational data. Airport facts live on Airport."""

    iata: str
    transfer_minutes: int
    transfer_cost_eur: float
    transfer_mode: str
    transfer_note: str | None
    disembark_minutes: int
    immigration_minutes: int
    recheck_buffer_minutes: int
    has_left_luggage: bool
    activity_density: float


@dataclass(frozen=True)
class Segment:
    carrier: str
    flight_number: str
    origin: str
    destination: str
    departure_local: datetime      # tz-aware
    arrival_local: datetime        # tz-aware
    is_international: bool


@dataclass(frozen=True)
class Layover:
    hub_iata: str
    arrival: datetime              # tz-aware, local to hub
    departure: datetime            # tz-aware, local to hub
    gross_minutes: int
    is_entry_point: bool
    requires_bag_reclaim: bool
