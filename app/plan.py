"""Plan generation — SPEC.md §9 and §10 step 8.

Curated rows and a greedy fill. No LLM and no live API: curated rows are
testable and generated ones are not.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.layover import band, daily_overlap_minutes, open_hours_minutes
from app.models import Activity, Hub

# §9: stop when 80% of the usable window is allocated. A plan with no slack is
# a plan that makes people miss flights.
ALLOCATION_CEILING = 0.80

# §9. Local clock bands, counted by intersection with the city window rather
# than by band: a band lookup would charge for meals during hours when nothing
# is open, which is the error §5.4 exists to prevent.
MEAL_WINDOWS = ((7, 9), (12, 14), (18, 21))
MEAL_MINIMUM_OVERLAP = 45

# §9. A bed is triggered by the band, not by window overlap, and the difference
# is not arbitrary: mealtimes are recurring daily bands, so intersecting them is
# the right test. A night's sleep is not a recurring band — §5.3 has already
# decided whether this layover needs one, so that decision is the trigger.
OVERNIGHT_BANDS = ("OVERNIGHT", "OVERNIGHT_NIGHT_ARRIVAL")

# §9. An overnight window contains a night, and you sleep through it. Paying for
# the bed via overnight_cost_eur did not stop the fill offering 898 minutes of
# sightseeing inside it — the meal defect again, a non-discretionary need with
# nothing modelling it. In an OVERNIGHT band the plannable window is clipped to
# the first local 08:00-22:00 block it touches, then capped. No sleep blocks and
# no second schedule: the 14-hour band and the ceiling agree by construction.
PLANNABLE_OPEN_LOCAL = 8
PLANNABLE_CLOSE_LOCAL = 22
PLANNABLE_CEILING_MINUTES = 840


def load_activities(conn: sqlite3.Connection, hub_iata: str) -> list[Activity]:
    rows = conn.execute("SELECT * FROM activity WHERE hub_iata = ?", (hub_iata,))
    return [
        Activity(
            **{
                **dict(row),
                "opens_local": time.fromisoformat(row["opens_local"]),
                "closes_local": time.fromisoformat(row["closes_local"]),
                "closed_weekdays": tuple(json.loads(row["closed_weekdays"])),
                "verified_on": (
                    date.fromisoformat(row["verified_on"]) if row["verified_on"] else None
                ),
            }
        )
        for row in rows
    ]


@dataclass(frozen=True)
class Plan:
    hub_iata: str
    window_start: datetime
    window_end: datetime
    zone: ZoneInfo
    items: list[Activity]
    transfer_cost_eur: float      # return trip to the centre
    meal_cost_eur: float          # one budget meal at this hub
    overnight_cost_eur: float     # one budget bed at this hub

    @property
    def usable_minutes(self) -> int:
        return max(0, int((self.window_end - self.window_start).total_seconds() // 60))

    @property
    def allocated_minutes(self) -> int:
        return sum(item.time_cost_minutes for item in self.items)

    @property
    def activities_cost_eur(self) -> float:
        return sum(item.cost_eur for item in self.items)

    @property
    def meals(self) -> int:
        """Mealtimes the window actually overlaps, not a guess from the band.

        A meal is non-discretionary — you eat whether or not it would have
        scored well — so it is counted here rather than competing in the fill.
        """
        return sum(
            1
            for from_hour, to_hour in MEAL_WINDOWS
            if daily_overlap_minutes(
                self.window_start, self.window_end, self.zone, from_hour, to_hour
            ) >= MEAL_MINIMUM_OVERLAP
        )

    @property
    def food_cost_eur(self) -> float:
        return self.meals * self.meal_cost_eur

    @property
    def band(self) -> str:
        """§5.3/§5.4, off this plan's own window — one implementation, reused."""
        return band(
            self.usable_minutes,
            open_hours_minutes(self.window_start, self.window_end, self.zone),
        )

    @property
    def stay_cost_eur(self) -> float:
        """A bed, when the band says one is required rather than optional.

        FULL_DAY's day-use hotel is optional, and optional does not belong in a
        mandatory cost.
        """
        return self.overnight_cost_eur if self.band in OVERNIGHT_BANDS else 0.0

    @property
    def total_cost_eur(self) -> float:
        """§4's layover_plan_cost_eur: transfers + activities + food + stay."""
        return (
            self.transfer_cost_eur
            + self.activities_cost_eur
            + self.food_cost_eur
            + self.stay_cost_eur
        )

    @property
    def plannable_window(self) -> tuple[datetime, datetime]:
        """The part of the city window a plan may fill.

        Outside OVERNIGHT bands this is the whole window. Inside one it is the
        waking portion — the city window itself is untouched, so usable_minutes,
        meals and the bed still see the real layover.
        """
        return plannable_window(self.window_start, self.window_end, self.zone, self.band)

    @property
    def plannable_minutes(self) -> int:
        start, end = self.plannable_window
        return max(0, int((end - start).total_seconds() // 60))

    @property
    def slack_minutes(self) -> int:
        return self.plannable_minutes - self.allocated_minutes


def is_open_during(activity: Activity, start: datetime, end: datetime, zone: ZoneInfo) -> bool:
    """Does the venue open at all inside the city window?

    Both rules are resolved against each *local* date the window touches, in the
    hub's zone. That matters for the weekly closure: a window crossing local
    midnight spans two weekdays — RIX's 13:07–07:50 does exactly that — so the
    weekday must come from the local date rather than the departure date.

    A row shut on either day the window touches is dropped outright. That is
    conservative, and it makes the overnight case right for free: better a
    sightseeing hour lost than a traveller sent to a locked door.
    """
    if closed_on_a_day_the_window_touches(activity, start, end, zone):
        return False
    day = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    while day <= last:
        opens = datetime.combine(day, activity.opens_local, tzinfo=zone)
        closes = datetime.combine(day, activity.closes_local, tzinfo=zone)
        if min(end, closes) > max(start, opens):
            return True
        day += timedelta(days=1)
    return False


def closed_on_a_day_the_window_touches(
    activity: Activity, start: datetime, end: datetime, zone: ZoneInfo
) -> bool:
    day = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    while day <= last:
        if day.isoweekday() in activity.closed_weekdays:
            return True
        day += timedelta(days=1)
    return False


def plannable_window(
    start: datetime, end: datetime, zone: ZoneInfo, band_name: str
) -> tuple[datetime, datetime]:
    """§9's waking window. Identity outside OVERNIGHT bands."""
    if band_name not in OVERNIGHT_BANDS:
        return start, end
    day = start.astimezone(zone).date()
    opens = datetime.combine(day, time(PLANNABLE_OPEN_LOCAL), tzinfo=zone)
    closes = datetime.combine(day, time(PLANNABLE_CLOSE_LOCAL), tzinfo=zone)
    if start >= closes:                       # landed after the city shut
        day += timedelta(days=1)
        opens = datetime.combine(day, time(PLANNABLE_OPEN_LOCAL), tzinfo=zone)
        closes = datetime.combine(day, time(PLANNABLE_CLOSE_LOCAL), tzinfo=zone)
    clipped_start = max(start, opens)
    clipped_end = min(end, closes)
    if clipped_end <= clipped_start:
        return start, start                   # nothing waking to plan into
    ceiling = clipped_start + timedelta(minutes=PLANNABLE_CEILING_MINUTES)
    return clipped_start, min(clipped_end, ceiling)


def fill(
    activities: list[Activity],
    hub: Hub,
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
) -> Plan:
    """SPEC.md §9's greedy fill.

    Sort by interest per minute, drop anything whose opening hours miss the
    window entirely, and stop at 80% allocated so a fifth of the time stays
    unspent.
    """
    empty = _plan(hub, start, end, zone, [])
    plan_start, plan_end = empty.plannable_window
    budget = int(empty.plannable_minutes * ALLOCATION_CEILING)
    chosen: list[Activity] = []
    spent = 0
    for activity in sorted(activities, key=lambda a: a.interest_density, reverse=True):
        if not is_open_during(activity, plan_start, plan_end, zone):
            continue
        if spent + activity.time_cost_minutes > budget:
            continue
        chosen.append(activity)
        spent += activity.time_cost_minutes
    return _plan(hub, start, end, zone, chosen)


def _plan(hub: Hub, start: datetime, end: datetime, zone: ZoneInfo, items) -> Plan:
    return Plan(
        hub_iata=hub.iata,
        window_start=start,
        window_end=end,
        zone=zone,
        items=items,
        transfer_cost_eur=hub.transfer_cost_eur * 2,
        meal_cost_eur=hub.meal_cost_eur,
        overnight_cost_eur=hub.overnight_cost_eur,
    )
