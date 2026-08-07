"""Plan generation — SPEC.md §9 and §10 step 8."""

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.db import connect
from app.models import Hub
from app.plan import ALLOCATION_CEILING, Plan, fill, is_open_during, load_activities
from app.seed import load_seed

DUBAI = ZoneInfo("Asia/Dubai")
FIXTURE = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures" / "itineraries"
     / "dxb_12h_halfday.json").read_text()
)
WINDOW_START = datetime.fromisoformat(FIXTURE["expected"]["window_start_local"])
WINDOW_END = datetime.fromisoformat(FIXTURE["expected"]["window_end_local"])


@pytest.fixture
def seeded(tmp_path):
    conn = connect(tmp_path / "layover.db")
    load_seed(conn)
    hub = Hub(**dict(conn.execute("SELECT * FROM hub WHERE iata = 'DXB'").fetchone()))
    rows = load_activities(conn, "DXB")
    conn.close()
    return rows, hub


@pytest.fixture
def activities(seeded):
    return seeded[0]


@pytest.fixture
def dxb_hub(seeded):
    return seeded[1]


@pytest.fixture
def dxb_plan(seeded):
    activities, hub = seeded
    return fill(activities, hub, WINDOW_START, WINDOW_END, DUBAI)


def test_eight_dxb_activities_load(activities):
    assert len(activities) == 8
    assert all(a.hub_iata == "DXB" for a in activities)


def test_every_activity_is_unverified(activities):
    """Opening hours and prices go stale — §7 applies to these as much as to hubs."""
    assert all(a.verified_on is None for a in activities)


def test_activity_fields_are_in_range(activities):
    for a in activities:
        assert 0.0 <= a.interest <= 1.0, a.name
        assert a.minutes_needed > 0, a.name
        assert a.cost_eur >= 0, a.name
        assert a.transfer_minutes_from_centre >= 0, a.name
        assert isinstance(a.opens_local, time) and isinstance(a.closes_local, time)


def test_plan_fits_inside_the_window(dxb_plan):
    assert dxb_plan.usable_minutes == FIXTURE["expected"]["usable_minutes"]
    assert dxb_plan.allocated_minutes <= dxb_plan.usable_minutes


def test_plan_leaves_at_least_twenty_percent_slack(dxb_plan):
    """§9: a plan with no slack is a plan that makes people miss flights."""
    assert dxb_plan.allocated_minutes <= dxb_plan.usable_minutes * ALLOCATION_CEILING
    assert dxb_plan.slack_minutes >= dxb_plan.usable_minutes * (1 - ALLOCATION_CEILING)


def test_closed_venues_are_dropped(dxb_plan, activities):
    """The fountain show starts at 18:00; the window closes at 15:45."""
    fountain = next(a for a in activities if "Fountain" in a.name)
    assert fountain.opens_local == time(18, 0)
    assert is_open_during(fountain, WINDOW_START, WINDOW_END, DUBAI) is False
    assert fountain not in dxb_plan.items


def test_plan_is_sorted_by_interest_per_minute(dxb_plan):
    densities = [a.interest_density for a in dxb_plan.items]
    assert densities == sorted(densities, reverse=True)


def test_dxb_plan_costs_what_its_items_cost(dxb_plan):
    """Hand-derived: 0.25 abra + 0.00 spice + 1.00 museum + 0.00 gold = 1.25."""
    assert [a.name.split(",")[0] for a in dxb_plan.items] == [
        "Abra across Dubai Creek",
        "Spice Souk",
        "Dubai Museum",
        "Gold Souk",
    ]
    assert dxb_plan.activities_cost_eur == 1.25
    assert dxb_plan.allocated_minutes == 221


def test_a_night_window_gets_a_different_plan(activities, dxb_hub, dxb_plan):
    """The same hub at 23:55–05:15 can reach almost nothing — §5.4's case."""
    start = datetime(2026, 9, 1, 23, 55, tzinfo=DUBAI)
    plan = fill(activities, dxb_hub, start, start + timedelta(minutes=320), DUBAI)
    assert all("Souk" not in a.name for a in plan.items)   # souks shut at 22:00
    assert plan.activities_cost_eur < dxb_plan.activities_cost_eur


def test_empty_window_yields_an_empty_plan(activities, dxb_hub):
    """doh_150min's window is inverted; nothing should be scheduled into it."""
    start = datetime(2026, 9, 2, 1, 10, tzinfo=DUBAI)
    end = datetime(2026, 9, 1, 22, 20, tzinfo=DUBAI)
    plan = fill(activities, dxb_hub, start, end, DUBAI)
    assert plan.usable_minutes == 0
    assert plan.items == []
    assert plan.activities_cost_eur == 0


# --- §9 meals ---------------------------------------------------------------


def test_the_worked_example_eats_lunch_and_only_lunch(dxb_plan):
    """09:30–15:45 overlaps 12:00–14:00 and neither breakfast nor dinner."""
    assert dxb_plan.meals == 1
    assert dxb_plan.food_cost_eur == 8.00
    assert dxb_plan.transfer_cost_eur == 4.00
    assert dxb_plan.total_cost_eur == 13.25


def test_a_night_window_buys_no_meals(activities, dxb_hub):
    """ist_night_2200_1000's shape: 23:55–05:15 overlaps no mealtime at all.

    A band-based lookup would have charged for meals during hours when nothing
    is open — the error §5.4 exists to prevent, arriving through the kitchen.
    """
    start = datetime(2026, 9, 1, 23, 55, tzinfo=DUBAI)
    plan = fill(activities, dxb_hub, start, start + timedelta(minutes=320), DUBAI)
    assert plan.meals == 0
    assert plan.food_cost_eur == 0


def test_a_long_window_eats_more_than_once(activities, dxb_hub):
    """08:00 to 20:00 catches breakfast, lunch and dinner."""
    start = datetime(2026, 9, 1, 7, 0, tzinfo=DUBAI)
    plan = fill(activities, dxb_hub, start, start + timedelta(hours=13), DUBAI)
    assert plan.meals == 3
    assert plan.food_cost_eur == 24.00


def test_a_brush_past_a_mealtime_does_not_count(activities, dxb_hub):
    """§9's 45-minute floor: arriving at 13:40 for lunch is not a meal."""
    start = datetime(2026, 9, 1, 13, 40, tzinfo=DUBAI)
    plan = fill(activities, dxb_hub, start, start + timedelta(hours=3), DUBAI)
    assert plan.meals == 0


def test_plan_figures_are_derived_not_stored(activities, dxb_hub):
    """Hard rule 7 — every figure on Plan comes off the window and the items."""
    plan = Plan("DXB", WINDOW_START, WINDOW_END, DUBAI, activities[:2], 4.0, 8.0, 35.0)
    assert plan.allocated_minutes == sum(a.time_cost_minutes for a in activities[:2])
    assert plan.activities_cost_eur == sum(a.cost_eur for a in activities[:2])
    assert plan.slack_minutes == plan.usable_minutes - plan.allocated_minutes
    assert plan.total_cost_eur == (
        4.0 + plan.activities_cost_eur + plan.food_cost_eur + plan.stay_cost_eur
    )


# --- §9 a bed, when the band requires one ------------------------------------


def test_a_half_day_buys_no_bed(dxb_plan):
    """HALF_DAY needs nowhere to sleep, so the worked example is unchanged."""
    assert dxb_plan.band == "HALF_DAY"
    assert dxb_plan.stay_cost_eur == 0.0
    assert dxb_plan.total_cost_eur == 13.25


def test_an_overnight_band_buys_a_bed(tmp_path):
    """RIX 23h: OVERNIGHT, three meals and a bed — 3 + 0 + 24 + 25 = 52."""
    from app.models import Hub

    conn = connect(tmp_path / "layover.db")
    load_seed(conn)
    hub = Hub(**dict(conn.execute("SELECT * FROM hub WHERE iata = 'RIX'").fetchone()))
    rows = load_activities(conn, "RIX")
    conn.close()

    riga = ZoneInfo("Europe/Riga")
    start = datetime(2026, 9, 1, 13, 7, tzinfo=riga)
    end = datetime(2026, 9, 2, 7, 50, tzinfo=riga)
    plan = fill(rows, hub, start, end, riga)

    assert plan.band == "OVERNIGHT"
    assert plan.stay_cost_eur == 25.00
    assert plan.meals == 3
    assert plan.total_cost_eur == 52.00
    # RIX has no curated rows yet, so admissions are structurally zero and this
    # figure moves once the remaining five hubs land.
    assert rows == [] and plan.activities_cost_eur == 0


def test_a_night_arrival_also_buys_a_bed(activities, dxb_hub):
    """OVERNIGHT_NIGHT_ARRIVAL is a bed with nothing open — still a bed."""
    start = datetime(2026, 9, 1, 21, 7, tzinfo=DUBAI)
    plan = fill(activities, dxb_hub, start, start + timedelta(minutes=523), DUBAI)
    assert plan.band == "OVERNIGHT_NIGHT_ARRIVAL"
    assert plan.stay_cost_eur == 35.00


def test_a_full_day_does_not_buy_a_bed(activities, dxb_hub):
    """§5.3's day-use hotel is optional, and optional is not a mandatory cost.

    Without this, widening OVERNIGHT_BANDS to include FULL_DAY breaks nothing —
    no other test or fixture reaches that band.
    """
    start = datetime(2026, 9, 1, 8, 0, tzinfo=DUBAI)
    plan = fill(activities, dxb_hub, start, start + timedelta(minutes=700), DUBAI)
    assert plan.band == "FULL_DAY"
    assert plan.stay_cost_eur == 0.0
