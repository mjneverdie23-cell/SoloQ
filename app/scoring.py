"""Scoring, hard gates and the §7 staleness gate — SPEC.md §5.5, §5.6 and §7."""

from dataclasses import dataclass
from datetime import date

from app.models import Airport, EntryRule, Hub, Itinerary, Layover

WEIGHTS = {
    "usable": 0.30,       # normalised against 720 min, capped
    "open_hours": 0.20,   # normalised against 480 min, capped
    "access": 0.20,       # 1 - (transfer_minutes / 90), floored at 0
    "entry_ease": 0.20,
    "density": 0.10,      # hand-scored editorial judgement, not measurement
}
PENALTIES = {
    "bag_reclaim": -15,
    "terminal_change": -5,
}

# Nearly inert in v0 and deliberately left that way: every seeded rule is
# schengen_internal or visa_free, so this term only ever takes 1.0 or 0.9 —
# 20% of the weight moving at most two points — and visa_required is
# unreachable because it is also a hard gate. It becomes the most
# discriminating term the moment v1 adds a non-Nordic passport. Do not
# calibrate the other weights against it, and do not tune it away.
ENTRY_EASE = {
    "schengen_internal": 1.0,
    "visa_free": 0.9,
    "voa": 0.6,
    "evisa": 0.3,
    "visa_required": 0.0,
    "no_landside_access": 0.0,
}

USABLE_NORMAL = 720
OPEN_HOURS_NORMAL = 480
ACCESS_NORMAL = 90
MINIMUM_USABLE = 180
BLOCKING_ENTRY_TYPES = ("visa_required", "no_landside_access")
STALENESS_DAYS = 180


@dataclass(frozen=True)
class Assessment:
    score: int
    blocked_reasons: list[str]
    stale_reasons: list[str]

    @property
    def is_recommendable(self) -> bool:
        """§7: stale data is never recommended, only shown with a warning."""
        return not self.blocked_reasons and not self.stale_reasons


def is_stale(verified_on: date | None, today: date) -> bool:
    """§7, failing closed. Null means stale, not "no requirement"."""
    if verified_on is None:
        return True
    return (today - verified_on).days > STALENESS_DAYS


def stale_reasons(
    hub: Hub, airport: Airport, entry_rule: EntryRule, today: date
) -> list[str]:
    """Every reference row behind this result that is unverified or expired."""
    sources = (
        ("hub", hub.verified_on),
        ("airport", airport.verified_on),
        ("entry rule", entry_rule.verified_on),
    )
    return [
        f"{name} data is unverified or over {STALENESS_DAYS} days old"
        for name, verified_on in sources
        if is_stale(verified_on, today)
    ]


def blocked_reasons(
    lay: Layover, hub: Hub, entry_rule: EntryRule, itin: Itinerary, usable: int
) -> list[str]:
    """§5.6. Every failing gate, never just the first.

    Someone told only "layover too short" will go and find a longer one, then
    hit the visa wall they were never shown.
    """
    reasons = []
    if entry_rule.entry_type in BLOCKING_ENTRY_TYPES:
        reasons.append(f"entry type {entry_rule.entry_type} — cannot go landside")
    if usable < MINIMUM_USABLE:
        reasons.append(f"only {usable} usable minutes, under the {MINIMUM_USABLE} floor")
    if not itin.is_single_ticket and lay.requires_bag_reclaim and not hub.has_left_luggage:
        reasons.append("self-transfer with bags to reclaim and no left luggage at the hub")
    return reasons


def assess(
    lay: Layover,
    hub: Hub,
    airport: Airport,
    entry_rule: EntryRule,
    itin: Itinerary,
    usable: int,
    open_hours: int,
    today: date,
) -> Assessment:
    """§5.5 weighted score, zeroed by any §5.6 gate, plus the §7 staleness flags."""
    blocked = blocked_reasons(lay, hub, entry_rule, itin, usable)
    stale = stale_reasons(hub, airport, entry_rule, today)
    if blocked:
        return Assessment(0, blocked, stale)

    weighted = (
        WEIGHTS["usable"] * min(usable / USABLE_NORMAL, 1.0)
        + WEIGHTS["open_hours"] * min(open_hours / OPEN_HOURS_NORMAL, 1.0)
        + WEIGHTS["access"] * max(0.0, 1 - hub.transfer_minutes / ACCESS_NORMAL)
        + WEIGHTS["entry_ease"] * ENTRY_EASE[entry_rule.entry_type]
        + WEIGHTS["density"] * hub.activity_density
    )
    value = weighted * 100
    if lay.requires_bag_reclaim:
        value += PENALTIES["bag_reclaim"]
    if lay.requires_terminal_change:
        value += PENALTIES["terminal_change"]
    # Round down, like every other number in this codebase (hard rule 3).
    return Assessment(max(0, min(100, int(value))), blocked, stale)
