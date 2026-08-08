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
    """RIX 23h: OVERNIGHT, three meals and a bed — 3 + 21 + 24 + 25 = 73."""
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
    assert plan.total_cost_eur == 73.00


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


# --- the other five hubs' curated rows (§9) ---------------------------------

ITINERARIES = Path(__file__).resolve().parent.parent / "fixtures" / "itineraries"


def hub_and_rows(tmp_path, iata):
    """Same (rows, hub) shape as `seeded`, for the hubs that landed after DXB."""
    conn = connect(tmp_path / "layover.db")
    load_seed(conn)
    hub = Hub(**dict(conn.execute("SELECT * FROM hub WHERE iata = ?", (iata,)).fetchone()))
    rows = load_activities(conn, iata)
    conn.close()
    return rows, hub


def window_of(fixture_id):
    """The city window §5 computed for a fixture, not one invented here."""
    expected = json.loads((ITINERARIES / f"{fixture_id}.json").read_text())["expected"]
    return (
        datetime.fromisoformat(expected["window_start_local"]),
        datetime.fromisoformat(expected["window_end_local"]),
    )


ISTANBUL = ZoneInfo("Europe/Istanbul")
IST_WINDOW = window_of("ist_negative_saving")


@pytest.fixture
def ist(tmp_path):
    return hub_and_rows(tmp_path, "IST")


@pytest.fixture
def ist_plan(ist):
    return fill(*ist, *IST_WINDOW, ISTANBUL)


def test_eight_ist_activities_load(ist):
    rows, _ = ist
    assert len(rows) == 8
    assert all(a.hub_iata == "IST" and a.verified_on is None for a in rows)


def test_ist_plan_over_the_negative_saving_window(ist_plan):
    """09:55–15:15, 320 usable. IST's 60-minute transfer is already spent.

    Hand-derived: 50 + 70 + 75 = 195 of a 256-minute budget, and the €25
    cistern is the only admission that fits.
    """
    assert [a.name for a in ist_plan.items] == [
        "Istiklal Caddesi walk, Taksim",
        "Blue Mosque, Sultanahmet",
        "Basilica Cistern",
    ]
    assert ist_plan.usable_minutes == 320
    assert ist_plan.allocated_minutes == 195
    assert ist_plan.slack_minutes == 125
    assert ist_plan.activities_cost_eur == 25.00
    assert ist_plan.total_cost_eur == 35.60      # 3.60 metro + 25.00 + 7.00 meal


def test_ist_excludes_the_sunset_ferry(ist, ist_plan):
    """The ferry sails at 17:30; the fixture window shuts at 15:15.

    At the fixture window this is over-determined: the ferry is shut *and*
    costs 118 minutes against 61 left in the budget, so removing the
    opening-hours filter changes nothing and an assertion here proves only that
    the ferry is absent, not that the filter did it. The second window below is
    long enough that budget is not binding, which is what makes closure the
    load-bearing reason — remove the filter and the ferry enters the plan.
    """
    rows, hub = ist
    ferry = next(a for a in rows if "sunset ferry" in a.name)
    assert ferry.opens_local == time(17, 30)
    assert is_open_during(ferry, *IST_WINDOW, ISTANBUL) is False
    assert ferry not in ist_plan.items

    roomy = (
        datetime(2026, 9, 1, 4, 50, tzinfo=ISTANBUL),
        datetime(2026, 9, 1, 17, 0, tzinfo=ISTANBUL),
    )
    plan = fill(rows, hub, *roomy, ISTANBUL)
    # Everything ranked above the ferry is taken and there is still room for it.
    assert plan.allocated_minutes + ferry.time_cost_minutes <= int(
        plan.usable_minutes * ALLOCATION_CEILING
    )
    assert is_open_during(ferry, *roomy, ISTANBUL) is False
    assert ferry not in plan.items


def test_ist_night_window_reaches_nothing(ist):
    """ist_night_2200_1000's 23:55–05:15: every curated row is shut."""
    plan = fill(*ist, *window_of("ist_night_2200_1000"), ISTANBUL)
    assert plan.items == []
    assert plan.band == "NO_EXIT"


QATAR = ZoneInfo("Asia/Qatar")
# doh_150min is the only DOH fixture and its window is inverted, so there is no
# computed window to fill. A representative daytime one stands in: 09:00–15:00
# local, 360 minutes, the HALF_DAY a real Doha stopover would land in.
DOH_WINDOW = (
    datetime(2026, 9, 1, 9, 0, tzinfo=QATAR),
    datetime(2026, 9, 1, 15, 0, tzinfo=QATAR),
)


@pytest.fixture
def doh(tmp_path):
    return hub_and_rows(tmp_path, "DOH")


@pytest.fixture
def doh_plan(doh):
    return fill(*doh, *DOH_WINDOW, QATAR)


def test_eight_doh_activities_load(doh):
    rows, _ = doh
    assert len(rows) == 8
    assert all(a.hub_iata == "DOH" and a.verified_on is None for a in rows)


def test_doh_plan_over_a_representative_daytime_window(doh_plan):
    """53 + 63 + 95 = 211 of a 288-minute budget. Every stop that fits is free."""
    assert [a.name for a in doh_plan.items] == [
        "Corniche promenade walk",
        "Msheireb Museums",
        "Souq Waqif",
    ]
    assert doh_plan.usable_minutes == 360
    assert doh_plan.allocated_minutes == 211
    assert doh_plan.slack_minutes == 149
    assert doh_plan.activities_cost_eur == 0.00
    assert doh_plan.total_cost_eur == 7.10       # 1.10 metro + 0.00 + 6.00 meal


def test_doh_excludes_the_sunset_dhow(doh, doh_plan):
    """The excluded row: the dhow leaves at 17:00 and the window shuts at 15:00.

    It out-ranks two stops that were taken, so only the opening-hours filter
    keeps it out.
    """
    rows, _ = doh
    dhow = next(a for a in rows if "Dhow cruise" in a.name)
    assert dhow.opens_local == time(17, 0)
    assert is_open_during(dhow, *DOH_WINDOW, QATAR) is False
    assert dhow not in doh_plan.items


def test_doh_fixture_window_is_inverted_and_plans_nothing(doh):
    """doh_150min: the deductions outrun the layover, so there is no window."""
    plan = fill(*doh, *window_of("doh_150min"), QATAR)
    assert plan.usable_minutes == 0
    assert plan.items == []


# AUH has no fixture at all, so this window is chosen rather than computed:
# 09:00–16:00 local, 420 usable, the HALF_DAY a daytime Abu Dhabi stop lands in.
AUH_WINDOW = (
    datetime(2026, 9, 1, 9, 0, tzinfo=DUBAI),
    datetime(2026, 9, 1, 16, 0, tzinfo=DUBAI),
)


@pytest.fixture
def auh(tmp_path):
    return hub_and_rows(tmp_path, "AUH")


@pytest.fixture
def auh_plan(auh):
    return fill(*auh, *AUH_WINDOW, DUBAI)


def test_eight_auh_activities_load(auh):
    rows, _ = auh
    assert len(rows) == 8
    assert all(a.hub_iata == "AUH" and a.verified_on is None for a in rows)


def test_auh_plan_over_a_representative_daytime_window(auh_plan):
    """A Tuesday: nothing is shut, so every exclusion here is budget."""
    assert [a.name for a in auh_plan.items] == [
        "Observation Deck at 300, Etihad Towers",
        "Corniche beach walk",
        "Mina Zayed date and fish market",
        "Qasr Al Hosn",
        "Heritage Village",
    ]
    assert auh_plan.usable_minutes == 420
    assert auh_plan.allocated_minutes == 332
    assert auh_plan.slack_minutes == 88
    assert auh_plan.activities_cost_eur == 26.00
    assert auh_plan.total_cost_eur == 36.00      # 2.00 bus + 26.00 + 8.00 meal


def test_auh_excludes_the_louvre_on_a_monday(auh):
    """AUH's excluded row is a weekly closure, not an evening opening.

    Louvre Abu Dhabi is shut on Mondays. On a Monday window it drops out even
    though it fits the budget and outranks two rows that are taken, so the
    weekday filter is the only thing keeping it out.
    """
    rows, hub = auh
    louvre = next(a for a in rows if "Louvre" in a.name)
    assert louvre.closed_weekdays == (1,)

    # Roomy on purpose. The fill sorts by interest per minute, so a 140-minute
    # museum never out-ranks short walks — at a tight window budget removes it
    # first and a closure assertion would prove nothing. Same trap as the IST
    # ferry.
    monday = (
        datetime(2026, 8, 31, 6, 0, tzinfo=DUBAI),      # a Monday
        datetime(2026, 8, 31, 20, 0, tzinfo=DUBAI),
    )
    assert monday[0].isoweekday() == 1
    plan = fill(rows, hub, *monday, DUBAI)
    assert louvre not in plan.items
    assert plan.allocated_minutes + louvre.time_cost_minutes <= int(
        plan.usable_minutes * ALLOCATION_CEILING
    )

    tuesday = (monday[0] + timedelta(days=1), monday[1] + timedelta(days=1))
    assert louvre in fill(rows, hub, *tuesday, DUBAI).items


WARSAW = ZoneInfo("Europe/Warsaw")
WAW_WINDOW = window_of("waw_inbound_bkk_osl")


@pytest.fixture
def waw(tmp_path):
    return hub_and_rows(tmp_path, "WAW")


@pytest.fixture
def waw_plan(waw):
    return fill(*waw, *WAW_WINDOW, WARSAW)


def test_eight_waw_activities_load(waw):
    rows, _ = waw
    assert len(rows) == 8
    assert all(a.hub_iata == "WAW" and a.verified_on is None for a in rows)


def test_waw_plan_over_the_inbound_window(waw_plan):
    """09:10–16:50, 460 usable: the longest half day of the six hubs.

    47 + 70 + 57 + 102 = 276 of a 368-minute budget, and the two big museums
    are exactly what 92 remaining minutes cannot buy.
    """
    assert [a.name for a in waw_plan.items] == [
        "Palace of Culture viewing terrace",
        "Old Town Market Square",
        "Vistula boulevards walk",
        "Royal Castle",
    ]
    assert waw_plan.usable_minutes == 460
    assert waw_plan.allocated_minutes == 276
    assert waw_plan.slack_minutes == 184
    assert waw_plan.activities_cost_eur == 14.00
    assert waw_plan.total_cost_eur == 23.20      # 2.20 train + 14.00 + 7.00 meal


def test_waw_excludes_two_museums_shut_on_a_tuesday(waw, waw_plan):
    """waw_inbound_bkk_osl lands on a Tuesday, and Warsaw shuts museums then.

    Asserted at a roomy Tuesday window rather than the fixture's, for the same
    reason as AUH: both museums are long, so at the fixture window budget
    removes them before the weekday filter is consulted and the assertion would
    be vacuous. Given room, the closure is the only thing keeping them out —
    the identical window on Wednesday takes them.
    """
    rows, hub = waw
    assert WAW_WINDOW[0].isoweekday() == 2
    museums = ("POLIN Museum of Polish Jews", "Warsaw Uprising Museum")
    for name in museums:
        assert next(a for a in rows if a.name == name).closed_weekdays == (2,)
        assert name not in [a.name for a in waw_plan.items]

    tuesday = (
        datetime(2026, 9, 1, 7, 0, tzinfo=WARSAW),
        datetime(2026, 9, 1, 19, 0, tzinfo=WARSAW),
    )
    shut = fill(rows, hub, *tuesday, WARSAW)
    assert all(name not in [a.name for a in shut.items] for name in museums)

    # The Uprising Museum is the one that carries the proof: it fits the
    # Tuesday budget with room to spare and still does not appear, and the
    # identical Wednesday window takes it. POLIN stays out on Wednesday too,
    # but on budget — the museum above it in density order eats the room.
    uprising = next(a for a in rows if a.name == museums[1])
    assert shut.allocated_minutes + uprising.time_cost_minutes <= int(
        shut.usable_minutes * ALLOCATION_CEILING
    )
    wednesday = (tuesday[0] + timedelta(days=1), tuesday[1] + timedelta(days=1))
    assert museums[1] in [a.name for a in fill(rows, hub, *wednesday, WARSAW).items]


def test_waw_evening_fountain_show_is_shut_by_the_clock_too(waw):
    """A Saturday, when the show does run, and still outside a daytime window."""
    rows, _ = waw
    show = next(a for a in rows if "Fountain Park" in a.name)
    saturday = (
        datetime(2026, 9, 5, 9, 10, tzinfo=WARSAW),
        datetime(2026, 9, 5, 16, 50, tzinfo=WARSAW),
    )
    assert saturday[0].isoweekday() == 6 and 6 not in show.closed_weekdays
    assert is_open_during(show, *saturday, WARSAW) is False


RIGA = ZoneInfo("Europe/Riga")
RIX_WINDOW = window_of("rix_23h_overnight")


@pytest.fixture
def rix(tmp_path):
    return hub_and_rows(tmp_path, "RIX")


@pytest.fixture
def rix_plan(rix):
    return fill(*rix, *RIX_WINDOW, RIGA)


def test_eight_rix_activities_load(rix):
    rows, _ = rix
    assert len(rows) == 8
    assert all(a.hub_iata == "RIX" and a.verified_on is None for a in rows)


def test_rix_plan_stops_at_bedtime(rix_plan):
    """13:07 to 07:50 next day is 1123 usable minutes, but you sleep in it.

    The plannable window is clipped to the waking part — 13:07 to 22:00, 533
    minutes — so the fill offers an evening in Riga rather than 898 minutes of
    sightseeing spanning a night. Jurmala is the casualty: a 215-minute beach
    trip no longer fits, which is correct.
    """
    assert rix_plan.usable_minutes == 1123          # the city window is untouched
    assert rix_plan.plannable_minutes == 533
    assert [a.name for a in rix_plan.items] == [
        "St Peter's Church tower",
        "House of the Blackheads",
        "Art Nouveau district, Alberta iela",
        "Riga Central Market",
        "Vecriga old town walk",
        "Museum of the Occupation of Latvia",
    ]
    assert "Jurmala beach by train" not in [a.name for a in rix_plan.items]
    assert rix_plan.allocated_minutes == 394
    assert rix_plan.slack_minutes == 139
    assert rix_plan.activities_cost_eur == 21.00
    assert rix_plan.total_cost_eur == 73.00      # 3.00 bus + 21.00 + 24.00 + 25.00 bed


def test_the_ceiling_only_binds_on_overnight_bands(activities, dxb_hub, dxb_plan):
    """A HALF_DAY plan sees its whole window; nothing is clipped."""
    assert dxb_plan.band == "HALF_DAY"
    assert dxb_plan.plannable_window == (dxb_plan.window_start, dxb_plan.window_end)
    assert dxb_plan.plannable_minutes == dxb_plan.usable_minutes


def test_the_ceiling_caps_at_fourteen_hours(activities, dxb_hub):
    """A window whose waking part exceeds 840 minutes is cut to 840."""
    from app.plan import PLANNABLE_CEILING_MINUTES

    start = datetime(2026, 9, 1, 6, 0, tzinfo=DUBAI)
    plan = fill(activities, dxb_hub, start, start + timedelta(hours=30), DUBAI)
    assert plan.band in ("OVERNIGHT", "OVERNIGHT_NIGHT_ARRIVAL")
    assert plan.plannable_minutes == PLANNABLE_CEILING_MINUTES
    assert plan.plannable_minutes < plan.usable_minutes


def test_rix_excludes_the_noon_organ_programme(rix, rix_plan):
    """The excluded row, and the only one not excluded by an evening opening.

    Riga Cathedral's organ programme runs at noon. The window opens at 13:07,
    an hour after that day's, and closes at 07:50 the next morning, four hours
    before the following one. A fixed daily slot is the one shape a long
    overnight window can still miss — an evening opener could not, because this
    window covers every evening hour.
    """
    rows, _ = rix
    tour = next(a for a in rows if "organ programme" in a.name)
    assert (tour.opens_local, tour.closes_local) == (time(12, 0), time(12, 25))
    assert is_open_during(tour, *RIX_WINDOW, RIGA) is False
    assert tour not in rix_plan.items
    # Nothing to do with the budget: 514 minutes of slack were left unspent.
    assert rix_plan.slack_minutes > tour.time_cost_minutes


def test_every_curated_row_is_unverified_and_in_range(tmp_path):
    """All 48 rows of §9 now exist. §7 applies to them as it does to the hubs."""
    conn = connect(tmp_path / "layover.db")
    load_seed(conn)
    rows = [load_activities(conn, iata) for iata in ("IST", "DOH", "DXB", "AUH", "WAW", "RIX")]
    conn.close()
    assert [len(hub_rows) for hub_rows in rows] == [8] * 6
    for a in [a for hub_rows in rows for a in hub_rows]:
        assert a.verified_on is None, a.name
        assert 0.0 <= a.interest <= 1.0, a.name
        assert a.minutes_needed > 0 and a.cost_eur >= 0, a.name
        assert a.opens_local < a.closes_local, a.name
