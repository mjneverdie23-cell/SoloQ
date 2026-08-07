"""Plan generation — SPEC.md §9 and §10 step 8."""

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.db import connect
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
def activities(tmp_path):
    conn = connect(tmp_path / "layover.db")
    load_seed(conn)
    rows = load_activities(conn, "DXB")
    conn.close()
    return rows


@pytest.fixture
def dxb_plan(activities):
    return fill(activities, "DXB", WINDOW_START, WINDOW_END, DUBAI)


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
    assert dxb_plan.cost_eur == 1.25
    assert dxb_plan.allocated_minutes == 221


def test_a_night_window_gets_a_different_plan(activities):
    """The same hub at 23:55–05:15 can reach almost nothing — §5.4's case."""
    start = datetime(2026, 9, 1, 23, 55, tzinfo=DUBAI)
    plan = fill(activities, "DXB", start, start + timedelta(minutes=320), DUBAI)
    assert all("Souk" not in a.name for a in plan.items)   # souks shut at 22:00
    assert plan.cost_eur < dxb_plan_cost_for_daytime(activities)


def dxb_plan_cost_for_daytime(activities):
    return fill(activities, "DXB", WINDOW_START, WINDOW_END, DUBAI).cost_eur


def test_empty_window_yields_an_empty_plan(activities):
    """doh_150min's window is inverted; nothing should be scheduled into it."""
    start = datetime(2026, 9, 2, 1, 10, tzinfo=DUBAI)
    end = datetime(2026, 9, 1, 22, 20, tzinfo=DUBAI)
    plan = fill(activities, "DXB", start, end, DUBAI)
    assert plan.usable_minutes == 0
    assert plan.items == []
    assert plan.cost_eur == 0


def test_plan_figures_are_derived_not_stored(activities):
    """Hard rule 7 — every figure on Plan comes off the window and the items."""
    plan = Plan("DXB", WINDOW_START, WINDOW_END, activities[:2])
    assert plan.allocated_minutes == sum(a.time_cost_minutes for a in activities[:2])
    assert plan.cost_eur == sum(a.cost_eur for a in activities[:2])
    assert plan.slack_minutes == plan.usable_minutes - plan.allocated_minutes
