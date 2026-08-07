"""SPEC.md §10 step 6: every fixture's `expected` block matches computed output.

The expected values in fixtures/itineraries/*.json were derived by hand from
§5's arithmetic, not read back from this code. That is what makes them a
regression suite rather than a snapshot of whatever the code happened to do.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.comparison import comparison_of
from app.db import connect
from app.fares import FixtureSource, fare_source, is_demo_mode, itinerary_of
from app.geo import load_entry_rule, load_schengen_airports
from app.layover import band, city_window, layovers_of, open_hours_minutes, usable_minutes
from app.models import Airport, Hub
from app.scoring import assess
from app.seed import load_seed

TODAY = date(2026, 8, 5)
FIXTURES = FixtureSource().load_all()
FIXTURE_IDS = [f["id"] for f in FIXTURES]


@pytest.fixture(scope="module")
def reference(tmp_path_factory):
    conn = connect(tmp_path_factory.mktemp("db") / "layover.db")
    load_seed(conn)
    hubs = {r["iata"]: Hub(**dict(r)) for r in conn.execute("SELECT * FROM hub")}
    airports = {r["iata"]: Airport(**dict(r)) for r in conn.execute("SELECT * FROM airport")}
    rules = {iata: load_entry_rule(conn, iata) for iata in hubs}
    schengen = load_schengen_airports(conn)
    conn.close()
    return hubs, airports, rules, schengen


def computed(fixture, reference):
    """Run one fixture all the way through §5, returning what it produced."""
    hubs, airports, rules, schengen = reference
    itin = itinerary_of(fixture)
    layovers = layovers_of(itin.outbound, itin, hubs, schengen)
    assert len(layovers) == 1, "each fixture holds exactly one layover (§8.2)"
    lay = layovers[0]

    hub = hubs[lay.hub_iata]
    onward = itin.outbound[-1]
    start, end = city_window(lay, hub, onward, schengen)
    usable = usable_minutes(lay, hub, onward, schengen)
    open_hours = open_hours_minutes(start, end, ZoneInfo(airports[lay.hub_iata].tz_name))
    result = assess(
        lay, hub, airports[lay.hub_iata], rules[lay.hub_iata], itin,
        usable, open_hours, TODAY,
    )
    return {
        "hub_iata": lay.hub_iata,
        "gross_minutes": lay.gross_minutes,
        "usable_minutes": usable,
        "open_hours_minutes": open_hours,
        "window_start_local": start.isoformat(),
        "window_end_local": end.isoformat(),
        "band": band(usable, open_hours),
        "score": result.score,
        "blocked_reasons": result.blocked_reasons,
    }


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_fixture_matches_its_expected_block(fixture, reference):
    assert computed(fixture, reference) == fixture["expected"]


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_fixture_carries_a_baseline_and_a_plan_cost(fixture):
    """Step 7's Comparison reads both; §9's activity rows are step 10."""
    assert fixture["baseline_price_eur"] > 0
    assert fixture["layover_plan_cost_eur"] >= 0
    # extra_hours divides by this; a missing or zero value would surface as a
    # nonsense euros_per_extra_hour rather than a failing test.
    assert fixture["baseline_duration_minutes"] > 0


def test_eight_fixtures_exist():
    assert len(FIXTURES) == 8


def test_terminal_change_follows_the_rule_not_the_author(reference):
    """§5.7 applied mechanically: self-transfer at DXB, IST or AUH, else False."""
    hubs, _, _, schengen = reference
    for fixture in FIXTURES:
        itin = itinerary_of(fixture)
        lay = layovers_of(itin.outbound, itin, hubs, schengen)[0]
        expected = not itin.is_single_ticket and lay.hub_iata in {"DXB", "IST", "AUH"}
        assert lay.requires_terminal_change is expected, fixture["id"]
        assert lay.requires_bag_reclaim is (not itin.is_single_ticket), fixture["id"]


def test_self_transfer_fixture_takes_both_penalties(reference):
    """Bag reclaim and terminal change co-occur; -20 is the realistic worst case."""
    fixture = next(f for f in FIXTURES if f["id"] == "dxb_selftransfer_bags")
    clean = next(f for f in FIXTURES if f["id"] == "dxb_12h_halfday")
    hubs, _, _, schengen = reference
    itin = itinerary_of(fixture)
    lay = layovers_of(itin.outbound, itin, hubs, schengen)[0]
    assert lay.requires_bag_reclaim and lay.requires_terminal_change
    # Same hub and same gross layover, so the gap is the 30 lost minutes plus -20.
    assert clean["expected"]["usable_minutes"] - fixture["expected"]["usable_minutes"] == 30


def test_twenty_two_hours_would_not_have_reached_overnight(reference):
    """Why the fixture is named 23h. RIX deducts 257 minutes."""
    hubs, _, _, _ = reference
    rix = hubs["RIX"]
    deductions = rix.disembark_minutes + rix.immigration_minutes \
        + rix.transfer_minutes * 2 + rix.recheck_buffer_minutes + 45
    assert deductions == 257
    assert 22 * 60 - deductions == 1063     # FULL_DAY, 17 minutes short
    assert 23 * 60 - deductions >= 1080     # OVERNIGHT


def test_window_endpoints_are_asserted_not_just_durations():
    """The SAFETY_MARGIN mutation is invisible to duration-only assertions."""
    for fixture in FIXTURES:
        assert "window_start_local" in fixture["expected"], fixture["id"]
        assert "window_end_local" in fixture["expected"], fixture["id"]


def test_fixture_source_is_the_default(monkeypatch):
    monkeypatch.delenv("FARE_SOURCE", raising=False)
    assert isinstance(fare_source(), FixtureSource)
    assert is_demo_mode() is True


def test_amadeus_source_is_not_built_yet(monkeypatch):
    monkeypatch.setenv("FARE_SOURCE", "amadeus")
    assert is_demo_mode() is False
    with pytest.raises(NotImplementedError):
        fare_source()


def test_search_returns_parsed_itineraries_not_raw_json():
    """§8.1: the scoring layer must not be able to tell which source it got."""
    depart = datetime.fromisoformat(
        next(f for f in FIXTURES if f["id"] == "dxb_12h_halfday")["outbound"][0][
            "departure_local"
        ]
    ).date()
    found = FixtureSource().search("OSL", "BKK", depart)
    assert found and all(hasattr(i, "is_single_ticket") for i in found)
    assert all(s.departure_local.tzinfo is not None for i in found for s in i.outbound)


# --- step 7: the comparison -------------------------------------------------


def comparison_for(fixture):
    return comparison_of(
        itinerary_of(fixture),
        baseline_price_eur=fixture["baseline_price_eur"],
        baseline_duration_minutes=fixture["baseline_duration_minutes"],
        layover_plan_cost_eur=fixture["layover_plan_cost_eur"],
    )


def test_negative_saving(reference):
    """SPEC.md §10 step 7 and the CLAUDE.md fixture.

    ist_negative_saving saves €20 of fare against a €55 plan. The itinerary is
    honestly worse and the number has to say so.
    """
    fixture = next(f for f in FIXTURES if f["id"] == "ist_negative_saving")
    comparison = comparison_for(fixture)
    assert comparison.fare_saving_eur == 20.0
    assert comparison.net_saving_eur == -35.0
    assert comparison.is_worse_than_flying_direct is True
    assert comparison.euros_per_extra_hour < 0


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_comparison_arithmetic_holds(fixture):
    comparison = comparison_for(fixture)
    assert comparison.fare_saving_eur == (
        fixture["baseline_price_eur"] - fixture["price_eur"]
    )
    assert comparison.net_saving_eur == (
        comparison.fare_saving_eur - fixture["layover_plan_cost_eur"]
    )
    assert comparison.is_worse_than_flying_direct is (comparison.net_saving_eur < 0)


@pytest.mark.parametrize("fixture", FIXTURES, ids=FIXTURE_IDS)
def test_the_long_way_round_is_always_longer(fixture):
    """Every fixture is a cheap slow routing, so extra_hours is positive."""
    comparison = comparison_for(fixture)
    assert comparison.extra_hours > 0
    assert comparison.euros_per_extra_hour is not None


def test_extra_hours_is_none_per_hour_when_not_slower():
    """The rate is meaningless when the cheap routing is not actually slower."""
    from app.comparison import Comparison

    same = Comparison(400.0, 500.0, 20.0, 900, 900)
    assert same.extra_hours == 0
    assert same.euros_per_extra_hour is None
