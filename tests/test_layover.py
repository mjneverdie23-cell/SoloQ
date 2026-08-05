from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.db import connect
from app.layover import band, recheck_buffer, usable_minutes
from app.models import Hub, Layover, Segment
from app.seed import load_seed

# The Schengen airports of the v0 dataset: OSL is the only origin, WAW and RIX
# the only Schengen hubs, and all ten destinations are outside the zone. This is
# reference data and does not belong in a test — it is injected here only until
# it is seeded. See the step 2 handover note.
V0_SCHENGEN_AIRPORTS = frozenset({"OSL", "WAW", "RIX"})


@pytest.fixture
def hubs(tmp_path):
    """The real seeded hubs, so the worked example uses hubs.seed.json values."""
    conn = connect(tmp_path / "layover.db")
    load_seed(conn)
    hubs = {row["iata"]: Hub(**dict(row)) for row in conn.execute("SELECT * FROM hub")}
    conn.close()
    return hubs


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


def test_dxb_12h_halfday(hubs):
    """SPEC.md §5.3 worked example. 720 - 25 - 35 - 60 - 180 - 45 = 375."""
    lay = layover_at(
        "DXB", "Asia/Dubai", 8, 720, is_entry_point=True, requires_bag_reclaim=False
    )
    assert lay.gross_minutes == 720

    usable = usable_minutes(lay, hubs["DXB"], onward_to("BKK"), V0_SCHENGEN_AIRPORTS)
    assert usable == 375
    assert band(usable) == "HALF_DAY"


def test_waw_inbound_keeps_the_shorter_buffer(hubs):
    # BKK -> WAW -> OSL: the onward leg stays inside Schengen.
    assert recheck_buffer(hubs["WAW"], onward_to("OSL"), V0_SCHENGEN_AIRPORTS) == 120


def test_waw_outbound_pays_the_full_buffer(hubs):
    # OSL -> WAW -> BKK: leaving the zone means full Schengen exit control.
    assert recheck_buffer(hubs["WAW"], onward_to("BKK"), V0_SCHENGEN_AIRPORTS) == 180


def test_waw_outbound_costs_an_hour_of_usable_time(hubs):
    """The direction of travel is worth 60 minutes at a Schengen hub."""
    lay = layover_at(
        "WAW", "Europe/Warsaw", 8, 720, is_entry_point=False, requires_bag_reclaim=False
    )
    inbound = usable_minutes(lay, hubs["WAW"], onward_to("OSL"), V0_SCHENGEN_AIRPORTS)
    outbound = usable_minutes(lay, hubs["WAW"], onward_to("BKK"), V0_SCHENGEN_AIRPORTS)
    assert inbound - outbound == 60


def test_non_schengen_hub_buffer_never_moves(hubs):
    for iata in ("IST", "DOH", "DXB", "AUH"):
        for destination in ("OSL", "BKK"):
            buffer = recheck_buffer(hubs[iata], onward_to(destination), V0_SCHENGEN_AIRPORTS)
            assert buffer == 180, f"{iata} -> {destination}"


def test_immigration_only_deducted_at_an_entry_point(hubs):
    kwargs = dict(requires_bag_reclaim=False)
    entry = layover_at("DXB", "Asia/Dubai", 8, 720, is_entry_point=True, **kwargs)
    transit = layover_at("DXB", "Asia/Dubai", 8, 720, is_entry_point=False, **kwargs)
    difference = usable_minutes(
        transit, hubs["DXB"], onward_to("BKK"), V0_SCHENGEN_AIRPORTS
    ) - usable_minutes(entry, hubs["DXB"], onward_to("BKK"), V0_SCHENGEN_AIRPORTS)
    assert difference == hubs["DXB"].immigration_minutes


def test_bag_reclaim_costs_thirty(hubs):
    lay = layover_at(
        "DXB", "Asia/Dubai", 8, 720, is_entry_point=True, requires_bag_reclaim=True
    )
    usable = usable_minutes(lay, hubs["DXB"], onward_to("BKK"), V0_SCHENGEN_AIRPORTS)
    assert usable == 375 - 30


def test_usable_minutes_floors_at_zero(hubs):
    """150 minutes gross — deductions exceed the layover, and time never goes negative."""
    lay = layover_at(
        "DOH", "Asia/Qatar", 8, 150, is_entry_point=True, requires_bag_reclaim=False
    )
    usable = usable_minutes(lay, hubs["DOH"], onward_to("BKK"), V0_SCHENGEN_AIRPORTS)
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
