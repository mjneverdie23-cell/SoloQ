"""Layover computation — SPEC.md §5.1 and §5.3."""

from app.models import Hub, Layover, Segment

SAFETY_MARGIN = 45
BAG_RECLAIM_MINUTES = 30
LEAVING_SCHENGEN_BUFFER = 180


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


def usable_minutes(
    lay: Layover, hub: Hub, onward: Segment, schengen_airports: frozenset[str]
) -> int:
    """SPEC.md §5.1. Every deduction rounds against the traveller."""
    m = lay.gross_minutes
    m -= hub.disembark_minutes
    if lay.is_entry_point:
        m -= hub.immigration_minutes
    if lay.requires_bag_reclaim:
        m -= BAG_RECLAIM_MINUTES
    m -= hub.transfer_minutes * 2
    m -= recheck_buffer(hub, onward, schengen_airports)
    m -= SAFETY_MARGIN
    return max(0, m)


def band(usable: int) -> str:
    """SPEC.md §5.3. The §5.4 daylight overlay can override this; not built yet."""
    if usable < 180:
        return "NO_EXIT"
    if usable < 360:
        return "QUICK"
    if usable < 660:
        return "HALF_DAY"
    if usable < 1080:
        return "FULL_DAY"
    return "OVERNIGHT"
