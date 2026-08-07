"""Dataclasses from SPEC.md §4.

Only the fields step 2 needs are here. The computed fields of `Layover` —
usable_minutes, daylight_minutes, band, score, blocked_reason — arrive with the
steps that compute them.
"""

from dataclasses import dataclass
from datetime import date, datetime, time


@dataclass(frozen=True)
class Airport:
    iata: str
    city: str
    country_iso2: str
    is_schengen: bool
    tz_name: str               # IANA zone; a fixed offset cannot do DST
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
    activity_density: float   # hand-scored opinion, not measurement
    verified_on: date | None


@dataclass(frozen=True)
class EntryRule:
    passport_scope: str
    country_iso2: str
    entry_type: str
    max_stay_days: int | None
    passport_validity_days: int
    notes: str
    verified_on: date | None


@dataclass(frozen=True)
class Activity:
    hub_iata: str
    name: str
    lat: float
    lon: float
    interest: float            # 0.0-1.0, hand-scored opinion, not measurement
    minutes_needed: int
    cost_eur: float
    # A recurring daily clock rule, not an instant, so `time` and not
    # `datetime` — hard rule 6 is about instants.
    opens_local: time
    closes_local: time
    transfer_minutes_from_centre: int
    verified_on: date | None

    @property
    def time_cost_minutes(self) -> int:
        return self.minutes_needed + self.transfer_minutes_from_centre

    @property
    def interest_density(self) -> float:
        """SPEC.md §9 sorts by interest per minute spent."""
        return self.interest / self.time_cost_minutes


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
class Itinerary:
    id: str
    price_eur: float
    is_single_ticket: bool         # False => self-transfer, bags not through-checked
    outbound: list[Segment]
    inbound: list[Segment]


@dataclass(frozen=True)
class Layover:
    hub_iata: str
    arrival: datetime              # tz-aware, local to hub
    departure: datetime            # tz-aware, local to hub
    is_entry_point: bool
    requires_bag_reclaim: bool
    requires_terminal_change: bool

    @property
    def gross_minutes(self) -> int:
        """Derived, never stored — hard rule 7. What the UI calls the layover."""
        return int((self.departure - self.arrival).total_seconds() // 60)
