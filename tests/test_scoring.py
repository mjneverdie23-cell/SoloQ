from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.db import connect
from app.geo import load_entry_rule, load_schengen_airports
from app.layover import city_window, open_hours_minutes, usable_minutes
from app.models import Airport, Hub, Itinerary, Layover, Segment
from app.scoring import (
    STALENESS_DAYS,
    Assessment,
    assess,
    blocked_reasons,
    is_stale,
    stale_reasons,
)

TODAY = date(2026, 8, 5)


@pytest.fixture
def seeded(tmp_path):
    conn = connect(tmp_path / "layover.db")
    from app.seed import load_seed

    load_seed(conn)
    hubs = {r["iata"]: Hub(**dict(r)) for r in conn.execute("SELECT * FROM hub")}
    airports = {r["iata"]: Airport(**dict(r)) for r in conn.execute("SELECT * FROM airport")}
    rules = {iata: load_entry_rule(conn, iata) for iata in hubs}
    schengen = load_schengen_airports(conn)
    conn.close()
    return hubs, airports, rules, schengen


def segment(origin: str, destination: str) -> Segment:
    departure = datetime(2026, 9, 1, 20, 0, tzinfo=ZoneInfo("Asia/Dubai"))
    return Segment(
        carrier="EK",
        flight_number="EK384",
        origin=origin,
        destination=destination,
        departure_local=departure,
        arrival_local=departure + timedelta(hours=6),
        is_international=True,
    )


def itinerary(*, single_ticket: bool = True) -> Itinerary:
    return Itinerary(
        id="test",
        price_eur=500.0,
        is_single_ticket=single_ticket,
        outbound=[segment("OSL", "DXB"), segment("DXB", "BKK")],
        inbound=[],
    )


def layover_at(hub_iata, tz, hour, gross, *, entry=True, bags=False, terminal=False):
    arrival = datetime(2026, 9, 1, hour, 0, tzinfo=ZoneInfo(tz))
    return Layover(
        hub_iata=hub_iata,
        arrival=arrival,
        departure=arrival + timedelta(minutes=gross),
        is_entry_point=entry,
        requires_bag_reclaim=bags,
        requires_terminal_change=terminal,
    )


def assess_case(seeded, lay, hub_iata, *, itin=None, today=TODAY, onward_to="BKK"):
    hubs, airports, rules, schengen = seeded
    onward = segment(hub_iata, onward_to)
    usable = usable_minutes(lay, hubs[hub_iata], onward, schengen)
    start, end = city_window(lay, hubs[hub_iata], onward, schengen)
    return assess(
        lay,
        hubs[hub_iata],
        airports[hub_iata],
        rules[hub_iata],
        itin or itinerary(),
        usable,
        open_hours_minutes(start, end, ZoneInfo(airports[lay.hub_iata].tz_name)),
        today,
    )


# --- §7 staleness, failing closed -------------------------------------------


def test_null_verified_on_is_stale_not_exempt(seeded):
    assert is_stale(None, TODAY) is True


def test_recent_verification_is_not_stale():
    assert is_stale(TODAY - timedelta(days=STALENESS_DAYS - 1), TODAY) is False


def test_verification_expires_at_180_days():
    assert is_stale(TODAY - timedelta(days=STALENESS_DAYS), TODAY) is False
    assert is_stale(TODAY - timedelta(days=STALENESS_DAYS + 1), TODAY) is True


def test_every_seeded_result_is_stale_on_all_three_sources(seeded):
    hubs, airports, rules, _ = seeded
    reasons = stale_reasons(hubs["DXB"], airports["DXB"], rules["DXB"], TODAY)
    assert len(reasons) == 3   # hub, airport and entry rule are all unverified


def test_nothing_in_the_seeded_dataset_is_recommendable(seeded):
    """The honest consequence of an entirely unverified seed. Do not soften."""
    lay = layover_at("DXB", "Asia/Dubai", 8, 720)
    result = assess_case(seeded, lay, "DXB")
    assert result.score > 0            # it scores
    assert result.blocked_reasons == []  # it is not blocked
    assert result.stale_reasons        # but it carries the warning
    assert result.is_recommendable is False


def test_verified_data_becomes_recommendable(seeded):
    """The gate opens once the rows are stamped — it is not permanently shut."""
    hubs, airports, rules, schengen = seeded
    stamped = date(2026, 7, 1)
    lay = layover_at("DXB", "Asia/Dubai", 8, 720)
    onward = segment("DXB", "BKK")
    usable = usable_minutes(lay, hubs["DXB"], onward, schengen)
    start, end = city_window(lay, hubs["DXB"], onward, schengen)
    result = assess(
        lay,
        replace(hubs["DXB"], verified_on=stamped),
        replace(airports["DXB"], verified_on=stamped),
        replace(rules["DXB"], verified_on=stamped),
        itinerary(),
        usable,
        open_hours_minutes(start, end, ZoneInfo(airports[lay.hub_iata].tz_name)),
        TODAY,
    )
    assert result.stale_reasons == []
    assert result.is_recommendable is True


# --- §5.6 hard gates ---------------------------------------------------------


def test_short_layover_gate(seeded):
    """150 minutes gross — the CLAUDE.md fixture."""
    lay = layover_at("DOH", "Asia/Qatar", 8, 150)
    result = assess_case(seeded, lay, "DOH")
    assert result.score == 0
    assert any("shorter than 3 usable hours" in r for r in result.blocked_reasons)


def test_visa_required_gate_ignores_duration(seeded):
    """A visa_required rule scores 0 however long the layover is."""
    hubs, airports, rules, schengen = seeded
    lay = layover_at("DXB", "Asia/Dubai", 8, 1440)
    onward = segment("DXB", "BKK")
    usable = usable_minutes(lay, hubs["DXB"], onward, schengen)
    start, end = city_window(lay, hubs["DXB"], onward, schengen)
    result = assess(
        lay,
        hubs["DXB"],
        airports["DXB"],
        replace(rules["DXB"], entry_type="visa_required"),
        itinerary(),
        usable,
        open_hours_minutes(start, end, ZoneInfo(airports[lay.hub_iata].tz_name)),
        TODAY,
    )
    assert usable > 600           # plenty of time, and it does not help
    assert result.score == 0
    assert any("visa_required" in r for r in result.blocked_reasons)


def test_self_transfer_without_left_luggage_gate(seeded):
    hubs, airports, rules, schengen = seeded
    lay = layover_at("DXB", "Asia/Dubai", 8, 720, bags=True)
    onward = segment("DXB", "BKK")
    usable = usable_minutes(lay, hubs["DXB"], onward, schengen)
    start, end = city_window(lay, hubs["DXB"], onward, schengen)
    result = assess(
        lay,
        replace(hubs["DXB"], has_left_luggage=False),
        airports["DXB"],
        rules["DXB"],
        itinerary(single_ticket=False),
        usable,
        open_hours_minutes(start, end, ZoneInfo(airports[lay.hub_iata].tz_name)),
        TODAY,
    )
    assert result.score == 0
    assert any("left luggage" in r for r in result.blocked_reasons)


def test_gates_are_collected_not_short_circuited(seeded):
    """Both gates failing must produce both reasons.

    Someone told only "too short" goes and finds a longer layover, then hits
    the visa wall nobody showed them.
    """
    hubs, airports, rules, schengen = seeded
    lay = layover_at("DXB", "Asia/Dubai", 8, 150)
    onward = segment("DXB", "BKK")
    usable = usable_minutes(lay, hubs["DXB"], onward, schengen)
    start, end = city_window(lay, hubs["DXB"], onward, schengen)
    result = assess(
        lay,
        hubs["DXB"],
        airports["DXB"],
        replace(rules["DXB"], entry_type="visa_required"),
        itinerary(),
        usable,
        open_hours_minutes(start, end, ZoneInfo(airports[lay.hub_iata].tz_name)),
        TODAY,
    )
    assert len(result.blocked_reasons) == 3
    assert any("visa_required" in r for r in result.blocked_reasons)
    assert any("shorter than 3 usable hours" in r for r in result.blocked_reasons)
    assert any("nothing open" in r for r in result.blocked_reasons)


def test_passport_validity_is_not_a_gate(seeded):
    """We cannot check it, so zeroing a score on it would imply that we had."""
    hubs, airports, rules, schengen = seeded
    lay = layover_at("IST", "Europe/Istanbul", 8, 720)
    onward = segment("IST", "BKK")
    usable = usable_minutes(lay, hubs["IST"], onward, schengen)
    start, end = city_window(lay, hubs["IST"], onward, schengen)
    # Turkey's 150-day rule is the strictest in the set and still gates nothing.
    assert rules["IST"].passport_validity_days == 150
    result = assess(
        lay, hubs["IST"], airports["IST"], rules["IST"], itinerary(),
        usable, open_hours_minutes(start, end, ZoneInfo(airports[lay.hub_iata].tz_name)), TODAY,
    )
    assert result.blocked_reasons == []
    assert result.score > 0


# --- §5.5 score --------------------------------------------------------------


def test_score_is_bounded(seeded):
    for hub_iata, tz in [("IST", "Europe/Istanbul"), ("DOH", "Asia/Qatar"),
                         ("DXB", "Asia/Dubai"), ("AUH", "Asia/Dubai"),
                         ("WAW", "Europe/Warsaw"), ("RIX", "Europe/Riga")]:
        for gross in (150, 480, 720, 1440):
            result = assess_case(seeded, layover_at(hub_iata, tz, 8, gross), hub_iata)
            assert 0 <= result.score <= 100


def test_bag_reclaim_penalty_costs_fifteen(seeded):
    plain = assess_case(seeded, layover_at("DXB", "Asia/Dubai", 8, 720), "DXB")
    # Bag reclaim also costs 30 usable minutes, so isolate the penalty by
    # comparing against the same layover lengthened to compensate.
    bags = assess_case(seeded, layover_at("DXB", "Asia/Dubai", 8, 750, bags=True), "DXB")
    assert plain.score - bags.score == 15


def test_terminal_change_penalty_costs_five(seeded):
    plain = assess_case(seeded, layover_at("DXB", "Asia/Dubai", 8, 720), "DXB")
    moved = assess_case(seeded, layover_at("DXB", "Asia/Dubai", 8, 720, terminal=True), "DXB")
    assert plain.score - moved.score == 5


def test_access_term_favours_the_closer_hub(seeded):
    """DOH is 25 minutes from the centre, IST 60. Same layover, DOH scores higher."""
    hubs, _, _, _ = seeded
    assert hubs["DOH"].transfer_minutes < hubs["IST"].transfer_minutes
    doh = assess_case(seeded, layover_at("DOH", "Asia/Qatar", 8, 720), "DOH")
    ist = assess_case(seeded, layover_at("IST", "Europe/Istanbul", 8, 720), "IST")
    assert doh.score > ist.score


def test_entry_ease_barely_moves_in_v0(seeded):
    """Documented inertness — 20% of the weight spanning two points.

    If this ever fails because the spread widened, the v1 passport matrix has
    landed and the term has started doing its job.
    """
    hubs, airports, rules, schengen = seeded
    lay = layover_at("DXB", "Asia/Dubai", 8, 720)
    onward = segment("DXB", "BKK")
    usable = usable_minutes(lay, hubs["DXB"], onward, schengen)
    start, end = city_window(lay, hubs["DXB"], onward, schengen)
    scores = []
    for entry_type in ("schengen_internal", "visa_free"):
        scores.append(
            assess(
                lay, hubs["DXB"], airports["DXB"],
                replace(rules["DXB"], entry_type=entry_type), itinerary(),
                usable, open_hours_minutes(start, end, ZoneInfo(airports[lay.hub_iata].tz_name)), TODAY,
            ).score
        )
    assert max(scores) - min(scores) <= 2


def test_night_layover_scores_below_the_same_length_by_day(seeded):
    """Open hours are 20% of the score, so clock time shows up in the number."""
    day = assess_case(seeded, layover_at("IST", "Europe/Istanbul", 8, 720), "IST")
    night = assess_case(seeded, layover_at("IST", "Europe/Istanbul", 22, 720), "IST")
    assert night.score < day.score


def test_assessment_is_recommendable_only_when_clean():
    assert Assessment(80, [], []).is_recommendable is True
    assert Assessment(0, ["blocked"], []).is_recommendable is False
    assert Assessment(80, [], ["stale"]).is_recommendable is False


def test_night_layover_is_gated_by_the_band_not_by_duration(seeded):
    """ist_night_2200_1000: 320 usable minutes, so `usable < 180` never fired.

    Duration alone calls this QUICK and it scored 47 under the old proxy gate.
    The band knew better, and gate 2 now reads the band.
    """
    lay = layover_at("IST", "Europe/Istanbul", 22, 720)
    result = assess_case(seeded, lay, "IST")
    assert result.score == 0
    assert result.blocked_reasons == ["nothing open during the usable window"]


def test_a_bed_is_still_a_plan(seeded):
    """OVERNIGHT_NIGHT_ARRIVAL also has no open hours and is deliberately not gated.

    The same 13-hour night at RIX falls either side of the line depending on
    where it goes next. Onward to OSL keeps the 120 buffer and leaves 523
    usable — over the 480 floor, so it is a hotel. Onward to BKK costs the full
    180 (§5.1) and leaves 463, which is under the floor and genuinely blocked.
    Sixty minutes of Schengen exit control is the whole difference.
    """
    from app.layover import band

    lay = layover_at("RIX", "Europe/Riga", 20, 780)
    hubs, airports, _, schengen = seeded

    onward = segment("RIX", "OSL")
    usable = usable_minutes(lay, hubs["RIX"], onward, schengen)
    start, end = city_window(lay, hubs["RIX"], onward, schengen)
    assert usable == 523
    assert open_hours_minutes(start, end, ZoneInfo(airports[lay.hub_iata].tz_name)) == 0
    assert band(usable, open_hours_minutes(start, end, ZoneInfo(airports[lay.hub_iata].tz_name))) == "OVERNIGHT_NIGHT_ARRIVAL"
    assert assess_case(seeded, lay, "RIX", onward_to="OSL").blocked_reasons == []

    leaving = segment("RIX", "BKK")
    assert usable_minutes(lay, hubs["RIX"], leaving, schengen) == 463
    assert assess_case(seeded, lay, "RIX", onward_to="BKK").blocked_reasons == [
        "nothing open during the usable window"
    ]


def test_overnight_band_needs_almost_a_full_day(seeded):
    """A band named "overnight" that a 22-hour layover does not reach.

    Worth writing down before anyone designs a booking flow around the name.
    """
    hubs, _, _, _ = seeded
    for iata, deductions, gross_needed in (("RIX", 257, 1337), ("DXB", 345, 1425)):
        hub = hubs[iata]
        assert (
            hub.disembark_minutes + hub.immigration_minutes
            + hub.transfer_minutes * 2 + hub.recheck_buffer_minutes + 45
        ) == deductions
        assert 1080 + deductions == gross_needed
    assert 1337 == 22 * 60 + 17    # RIX: 22h17m
    assert 1425 == 23 * 60 + 45    # DXB: 23h45m
