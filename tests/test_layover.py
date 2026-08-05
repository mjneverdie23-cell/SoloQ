from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.db import connect
from app.geo import load_schengen_airports
from app.layover import (
    band,
    city_window,
    duration_band,
    is_entry_point,
    open_hours_minutes,
    recheck_buffer,
    usable_minutes,
)
from app.models import Hub, Layover, Segment
from app.seed import load_seed


@pytest.fixture
def seeded(tmp_path):
    """Hubs and the Schengen set from the seed, so neither is hardcoded here."""
    conn = connect(tmp_path / "layover.db")
    load_seed(conn)
    hubs = {row["iata"]: Hub(**dict(row)) for row in conn.execute("SELECT * FROM hub")}
    schengen = load_schengen_airports(conn)
    conn.close()
    return hubs, schengen


@pytest.fixture
def hubs(seeded):
    return seeded[0]


@pytest.fixture
def schengen(seeded):
    return seeded[1]


def onward_to(destination: str) -> Segment:
    """An onward leg. Only `destination` is read by §5.1."""
    departure = datetime(2026, 9, 1, 20, 0, tzinfo=ZoneInfo("Asia/Dubai"))
    return Segment(
        carrier="EK",
        flight_number="EK384",
        origin="DXB",
        destination=destination,
        departure_local=departure,
        arrival_local=departure + timedelta(hours=6),
        is_international=True,
    )


def arriving_from(origin: str) -> Segment:
    """The inbound leg into the hub. Only `origin` is read by §5.2."""
    arrival = datetime(2026, 9, 1, 8, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    return Segment(
        carrier="LO",
        flight_number="LO78",
        origin=origin,
        destination="WAW",
        departure_local=arrival - timedelta(hours=10),
        arrival_local=arrival,
        is_international=True,
    )


def layover_at(hub_iata, tz, arrive_hour, gross_minutes, *, is_entry_point, requires_bag_reclaim):
    """Build a layover from tz-aware endpoints, deriving gross_minutes from them."""
    arrival = datetime(2026, 9, 1, arrive_hour, 0, tzinfo=ZoneInfo(tz))
    departure = arrival + timedelta(minutes=gross_minutes)
    return Layover(
        hub_iata=hub_iata,
        arrival=arrival,
        departure=departure,
        is_entry_point=is_entry_point,
        requires_bag_reclaim=requires_bag_reclaim,
    )


def test_dxb_12h_halfday(hubs, schengen):
    """SPEC.md §5.3 worked example. 720 - 25 - 35 - 60 - 180 - 45 = 375."""
    lay = layover_at(
        "DXB", "Asia/Dubai", 8, 720, is_entry_point=True, requires_bag_reclaim=False
    )
    assert lay.gross_minutes == 720

    usable = usable_minutes(lay, hubs["DXB"], onward_to("BKK"), schengen)
    assert usable == 375

    start, end = city_window(lay, hubs["DXB"], onward_to("BKK"), schengen)
    assert (start.hour, start.minute) == (9, 30)    # 08:00 + 25 + 35 + 30
    assert (end.hour, end.minute) == (15, 45)       # 20:00 - 180 - 30 - 45
    assert band(usable, open_hours_minutes(start, end)) == "HALF_DAY"


def test_schengen_entry_inbound(hubs, schengen):
    # BKK -> WAW -> OSL: Warsaw is where the customs union is entered, so
    # immigration happens there and not at Oslo.
    assert is_entry_point(hubs["WAW"], arriving_from("BKK"), schengen) is True


def test_schengen_entry_outbound(hubs, schengen):
    # OSL -> WAW -> BKK: already inside Schengen, so Warsaw is not an entry point.
    assert is_entry_point(hubs["WAW"], arriving_from("OSL"), schengen) is False


def test_rix_follows_the_same_entry_rule(hubs, schengen):
    assert is_entry_point(hubs["RIX"], arriving_from("BKK"), schengen) is True
    assert is_entry_point(hubs["RIX"], arriving_from("OSL"), schengen) is False


def test_non_schengen_hubs_are_always_entry_points(hubs, schengen):
    for iata in ("IST", "DOH", "DXB", "AUH"):
        for origin in ("OSL", "BKK"):
            assert is_entry_point(hubs[iata], arriving_from(origin), schengen) is True


def test_waw_inbound_is_the_best_case_in_the_set(hubs, schengen):
    """BKK -> WAW -> OSL: entry point, but the cheap buffer and no bag reclaim."""
    lay = layover_at(
        "WAW", "Europe/Warsaw", 8, 720, is_entry_point=True, requires_bag_reclaim=False
    )
    usable = usable_minutes(lay, hubs["WAW"], onward_to("OSL"), schengen)
    # 720 - 15 disembark - 30 immigration - 50 transfer - 120 recheck - 45 margin
    assert usable == 460

    start, end = city_window(lay, hubs["WAW"], onward_to("OSL"), schengen)
    assert band(usable, open_hours_minutes(start, end)) == "HALF_DAY"


def test_waw_inbound_keeps_the_shorter_buffer(hubs, schengen):
    # BKK -> WAW -> OSL: the onward leg stays inside Schengen.
    assert recheck_buffer(hubs["WAW"], onward_to("OSL"), schengen) == 120


def test_waw_outbound_pays_the_full_buffer(hubs, schengen):
    # OSL -> WAW -> BKK: leaving the zone means full Schengen exit control.
    assert recheck_buffer(hubs["WAW"], onward_to("BKK"), schengen) == 180


def test_waw_outbound_costs_an_hour_of_usable_time(hubs, schengen):
    """The direction of travel is worth 60 minutes at a Schengen hub."""
    lay = layover_at(
        "WAW", "Europe/Warsaw", 8, 720, is_entry_point=False, requires_bag_reclaim=False
    )
    inbound = usable_minutes(lay, hubs["WAW"], onward_to("OSL"), schengen)
    outbound = usable_minutes(lay, hubs["WAW"], onward_to("BKK"), schengen)
    assert inbound - outbound == 60


def test_non_schengen_hub_buffer_never_moves(hubs, schengen):
    for iata in ("IST", "DOH", "DXB", "AUH"):
        for destination in ("OSL", "BKK"):
            buffer = recheck_buffer(hubs[iata], onward_to(destination), schengen)
            assert buffer == 180, f"{iata} -> {destination}"


def test_immigration_only_deducted_at_an_entry_point(hubs, schengen):
    kwargs = dict(requires_bag_reclaim=False)
    entry = layover_at("DXB", "Asia/Dubai", 8, 720, is_entry_point=True, **kwargs)
    transit = layover_at("DXB", "Asia/Dubai", 8, 720, is_entry_point=False, **kwargs)
    difference = usable_minutes(
        transit, hubs["DXB"], onward_to("BKK"), schengen
    ) - usable_minutes(entry, hubs["DXB"], onward_to("BKK"), schengen)
    assert difference == hubs["DXB"].immigration_minutes


def test_bag_reclaim_costs_thirty(hubs, schengen):
    lay = layover_at(
        "DXB", "Asia/Dubai", 8, 720, is_entry_point=True, requires_bag_reclaim=True
    )
    usable = usable_minutes(lay, hubs["DXB"], onward_to("BKK"), schengen)
    assert usable == 375 - 30


def test_usable_minutes_floors_at_zero(hubs, schengen):
    """150 minutes gross — deductions exceed the layover, and time never goes negative."""
    lay = layover_at(
        "DOH", "Asia/Qatar", 8, 150, is_entry_point=True, requires_bag_reclaim=False
    )
    usable = usable_minutes(lay, hubs["DOH"], onward_to("BKK"), schengen)
    assert usable == 0
    assert duration_band(usable) == "NO_EXIT"


@pytest.mark.parametrize(
    "usable,expected",
    [
        (0, "NO_EXIT"),
        (179, "NO_EXIT"),
        (180, "QUICK"),
        (359, "QUICK"),
        (360, "HALF_DAY"),
        (659, "HALF_DAY"),
        (660, "FULL_DAY"),
        (1079, "FULL_DAY"),
        (1080, "OVERNIGHT"),
    ],
)
def test_band_boundaries(usable, expected):
    assert duration_band(usable) == expected


def test_night_arrival(hubs, schengen):
    """ist_night_2200_1000 — a twelve-hour layover worth nothing.

    Arrive 22:00, depart 10:00. The city window lands entirely in the small
    hours, so the duration says HALF_DAY and §5.4 correctly says otherwise.
    """
    lay = layover_at(
        "IST", "Europe/Istanbul", 22, 720, is_entry_point=True, requires_bag_reclaim=False
    )
    start, end = city_window(lay, hubs["IST"], onward_to("BKK"), schengen)
    usable = usable_minutes(lay, hubs["IST"], onward_to("BKK"), schengen)
    open_hours = open_hours_minutes(start, end)

    assert (start.hour, start.minute) == (23, 55)   # 22:00 + 25 + 30 + 60
    assert (end.hour, end.minute) == (5, 15)        # 10:00 - 180 - 60 - 45
    assert usable == 320
    assert open_hours == 0
    assert duration_band(usable) == "QUICK"         # what duration alone claims
    assert band(usable, open_hours) == "NO_EXIT"    # what §5.4 knows


def test_long_night_layover_is_a_bed_not_a_gate(hubs, schengen):
    """§5.4's other branch: nothing open, but long enough that a hotel is the plan.

    Arrive 20:00, depart 09:00. The window is 21:07–05:50, which clears the
    480-minute floor without touching a single open hour.
    """
    lay = layover_at(
        "RIX", "Europe/Riga", 20, 780, is_entry_point=True, requires_bag_reclaim=False
    )
    start, end = city_window(lay, hubs["RIX"], onward_to("OSL"), schengen)
    usable = usable_minutes(lay, hubs["RIX"], onward_to("OSL"), schengen)
    open_hours = open_hours_minutes(start, end)

    assert open_hours < 120
    assert usable >= 480
    assert band(usable, open_hours) == "OVERNIGHT_NIGHT_ARRIVAL"


def test_open_hours_counts_only_the_08_to_21_overlap():
    """A window straddling the close: 18:00–23:00 local contributes three hours."""
    tz = ZoneInfo("Europe/Warsaw")
    start = datetime(2026, 9, 1, 18, 0, tzinfo=tz)
    assert open_hours_minutes(start, start + timedelta(hours=5)) == 180


def test_open_hours_spans_multiple_days():
    """A 30-hour window collects two separate open-hours blocks, not one."""
    tz = ZoneInfo("Europe/Warsaw")
    start = datetime(2026, 9, 1, 12, 0, tzinfo=tz)
    # 12:00-21:00 today (540) + 08:00-18:00 tomorrow (600).
    assert open_hours_minutes(start, start + timedelta(hours=30)) == 540 + 600


def _usable_by_deduction_list(lay, hub, onward, schengen):
    """The pre-refactor §5.1 form, recomputed independently of city_window."""
    m = lay.gross_minutes
    m -= hub.disembark_minutes
    if lay.is_entry_point:
        m -= hub.immigration_minutes
    if lay.requires_bag_reclaim:
        m -= 30
    m -= hub.transfer_minutes * 2
    m -= recheck_buffer(hub, onward, schengen)
    m -= 45
    return max(0, m)


@pytest.mark.parametrize("hub_iata,tz", [
    ("IST", "Europe/Istanbul"), ("DOH", "Asia/Qatar"), ("DXB", "Asia/Dubai"),
    ("AUH", "Asia/Dubai"), ("WAW", "Europe/Warsaw"), ("RIX", "Europe/Riga"),
])
@pytest.mark.parametrize("gross", [150, 480, 720, 1320])
@pytest.mark.parametrize("entry", [True, False])
@pytest.mark.parametrize("bags", [True, False])
@pytest.mark.parametrize("destination", ["OSL", "BKK"])
def test_window_delta_equals_the_deduction_list(
    hubs, schengen, hub_iata, tz, gross, entry, bags, destination
):
    """SPEC.md §5.1: the two forms are algebraically identical.

    usable_minutes is derived from city_window now. If a future edit moves a
    term into one and not the other, this is what catches it.
    """
    lay = layover_at(hub_iata, tz, 8, gross, is_entry_point=entry, requires_bag_reclaim=bags)
    onward = onward_to(destination)
    assert usable_minutes(lay, hubs[hub_iata], onward, schengen) == _usable_by_deduction_list(
        lay, hubs[hub_iata], onward, schengen
    )
