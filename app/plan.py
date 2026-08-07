"""Plan generation — SPEC.md §9 and §10 step 8.

Curated rows and a greedy fill. No LLM and no live API: curated rows are
testable and generated ones are not.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.models import Activity

# §9: stop when 80% of the usable window is allocated. A plan with no slack is
# a plan that makes people miss flights.
ALLOCATION_CEILING = 0.80


def load_activities(conn: sqlite3.Connection, hub_iata: str) -> list[Activity]:
    rows = conn.execute("SELECT * FROM activity WHERE hub_iata = ?", (hub_iata,))
    return [
        Activity(
            **{
                **dict(row),
                "opens_local": time.fromisoformat(row["opens_local"]),
                "closes_local": time.fromisoformat(row["closes_local"]),
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
    items: list[Activity]

    @property
    def usable_minutes(self) -> int:
        return max(0, int((self.window_end - self.window_start).total_seconds() // 60))

    @property
    def allocated_minutes(self) -> int:
        return sum(item.time_cost_minutes for item in self.items)

    @property
    def cost_eur(self) -> float:
        return sum(item.cost_eur for item in self.items)

    @property
    def slack_minutes(self) -> int:
        return self.usable_minutes - self.allocated_minutes


def is_open_during(activity: Activity, start: datetime, end: datetime, zone: ZoneInfo) -> bool:
    """Does the venue's daily window intersect the city window at all?

    The clock rule is resolved against each local date the window touches, in
    the hub's zone — the same reason §5.4 needs a zone rather than an offset.
    """
    day = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    while day <= last:
        opens = datetime.combine(day, activity.opens_local, tzinfo=zone)
        closes = datetime.combine(day, activity.closes_local, tzinfo=zone)
        if min(end, closes) > max(start, opens):
            return True
        day += timedelta(days=1)
    return False


def fill(
    activities: list[Activity],
    hub_iata: str,
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
) -> Plan:
    """SPEC.md §9's greedy fill.

    Sort by interest per minute, drop anything whose opening hours miss the
    window entirely, and stop at 80% allocated so a fifth of the time stays
    unspent.
    """
    plan = Plan(hub_iata, start, end, [])
    budget = int(plan.usable_minutes * ALLOCATION_CEILING)
    chosen: list[Activity] = []
    spent = 0
    for activity in sorted(activities, key=lambda a: a.interest_density, reverse=True):
        if not is_open_during(activity, start, end, zone):
            continue
        if spent + activity.time_cost_minutes > budget:
            continue
        chosen.append(activity)
        spent += activity.time_cost_minutes
    return Plan(hub_iata, start, end, chosen)
