from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.db import connect
from app.geo import load_schengen_airports
from app.layover import band, is_entry_point, recheck_buffer, usable_minutes
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
        # Round down: never credit a minute we cannot prove (hard rule 3).
        gross_minutes=int((departure - arrival).total_seconds() // 60),
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
    assert band(usable) == "HALF_DAY"


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
    assert band(usable) == "HALF_DAY"


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
    assert band(usable) == "NO_EXIT"


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
    assert band(usable) == expected
