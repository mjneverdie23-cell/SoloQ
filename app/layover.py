"""Layover computation — SPEC.md §5.1, §5.3 and §5.4."""

from datetime import datetime, time, timedelta

from app.models import Hub, Layover, Segment

SAFETY_MARGIN = 45
BAG_RECLAIM_MINUTES = 30
LEAVING_SCHENGEN_BUFFER = 180

CITY_OPEN_LOCAL = 8
CITY_CLOSE_LOCAL = 21
OPEN_HOURS_FLOOR = 120        # below this, sightseeing is not the plan
NIGHT_OVERNIGHT_FLOOR = 480   # ...but a bed still is, if the layover is long


def is_entry_point(
    hub: Hub, arriving_from: Segment, schengen_airports: frozenset[str]
) -> bool:
    """SPEC.md §5.2.

    Immigration happens at the first point of entry into the customs union, not
    at the final destination.
    """
    if hub.iata in schengen_airports:
        # Only an entry point if the inbound leg came from outside Schengen.
        return arriving_from.origin not in schengen_airports
    return True   # non-Schengen hubs: always clear immigration to go landside


def recheck_buffer(hub: Hub, onward: Segment, schengen_airports: frozenset[str]) -> int:
    """SPEC.md §5.1.

    A Schengen hub's 120 assumes an intra-Schengen departure. Leaving the zone
    means full exit control, which is what the non-Schengen 180 already covers.
    """
    if hub.iata in schengen_airports and onward.destination not in schengen_airports:
        return LEAVING_SCHENGEN_BUFFER
    return hub.recheck_buffer_minutes


def city_window(
    lay: Layover, hub: Hub, onward: Segment, schengen_airports: frozenset[str]
) -> tuple[datetime, datetime]:
    """SPEC.md §5.1. When the traveller is actually outside the airport.

    These timestamps are what a traveller acts on — "leave around 23:55, be back
    by 05:15". usable_minutes is the internal number derived from them.
    """
    start = lay.arrival + timedelta(
        minutes=(
            hub.disembark_minutes
            + (hub.immigration_minutes if lay.is_entry_point else 0)
            + (BAG_RECLAIM_MINUTES if lay.requires_bag_reclaim else 0)
            + hub.transfer_minutes
        )
    )
    end = lay.departure - timedelta(
        minutes=(
            recheck_buffer(hub, onward, schengen_airports)
            + hub.transfer_minutes
            + SAFETY_MARGIN   # protects the return, not the arrival
        )
    )
    return start, end


def usable_minutes(
    lay: Layover, hub: Hub, onward: Segment, schengen_airports: frozenset[str]
) -> int:
    """SPEC.md §5.1, derived from the window so the two cannot drift apart."""
    start, end = city_window(lay, hub, onward, schengen_airports)
    return max(0, _floor_minutes(end - start))


def open_hours_minutes(start: datetime, end: datetime) -> int:
    """SPEC.md §5.4. Overlap of the city window with local 08:00–21:00.

    Open hours, not daylight: museums keep the same hours in a Riga December.
    """
    total = 0
    day = start.date()
    while day <= end.date():
        opens = datetime.combine(day, time(CITY_OPEN_LOCAL), tzinfo=start.tzinfo)
        closes = datetime.combine(day, time(CITY_CLOSE_LOCAL), tzinfo=start.tzinfo)
        total += max(0, _floor_minutes(min(end, closes) - max(start, opens)))
        day += timedelta(days=1)
    return total


def duration_band(usable: int) -> str:
    """SPEC.md §5.3, on duration alone."""
    if usable < 180:
        return "NO_EXIT"
    if usable < 360:
        return "QUICK"
    if usable < 660:
        return "HALF_DAY"
    if usable < 1080:
        return "FULL_DAY"
    return "OVERNIGHT"


def band(usable: int, open_hours: int) -> str:
    """SPEC.md §5.3 with the §5.4 overlay applied.

    A 22:00–10:00 layover is a hotel opportunity, not a sightseeing one.
    """
    if open_hours < OPEN_HOURS_FLOOR:
        if usable >= NIGHT_OVERNIGHT_FLOOR:
            return "OVERNIGHT_NIGHT_ARRIVAL"
        return "NO_EXIT"
    return duration_band(usable)


def _floor_minutes(delta: timedelta) -> int:
    """Round down. Never credit a minute we cannot prove (hard rule 3)."""
    return int(delta.total_seconds() // 60)
