"""web/landing.html quotes the DXB worked example. Keep it honest.

Every figure on that page is hard-coded HTML. If the scoring logic moves and
nobody updates the page, the page starts lying to strangers on the internet —
which is worse than a failing test.
"""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "web" / "landing.html").read_text()
FIXTURE = json.loads((ROOT / "fixtures" / "itineraries" / "dxb_12h_halfday.json").read_text())
EXPECTED = FIXTURE["expected"]


def local_hhmm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M")


def ledger_row(label: str) -> int:
    """The signed minutes in the row whose first cell is `label`."""
    match = re.search(
        rf"<td>{re.escape(label)}</td><td class=\"num\">(−?-?\d+)</td>", PAGE
    )
    assert match, f"no ledger row labelled {label!r}"
    return int(match.group(1).replace("−", "-"))


def money(label: str) -> float:
    match = re.search(
        rf"<dt>{re.escape(label)}</dt><dd>\+?€([\d.]+)</dd>", PAGE
    )
    assert match, f"no money figure labelled {label!r}"
    return float(match.group(1))


def strip_columns() -> list[int]:
    match = re.search(r"grid-template-columns:\s*([0-9fr\s]+);", PAGE)
    assert match, "the strip has no proportional grid"
    return [int(n) for n in re.findall(r"(\d+)fr", match.group(1))]


# --- hard rule 8: this is a standalone document, not an artifact fragment -----


@pytest.mark.parametrize(
    "needle",
    [
        "<!DOCTYPE html>",
        '<html lang="en">',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="description"',
        '<meta property="og:title"',
    ],
)
def test_page_is_a_standalone_document(needle):
    assert needle in PAGE


def test_no_unreachable_theme_selectors():
    """Nothing sets data-theme outside an artifact host, so such rules are dead."""
    assert "data-theme" not in PAGE


def test_stylesheet_braces_balance():
    css = PAGE.split("<style>")[1].split("</style>")[0]
    assert css.count("{") == css.count("}")


# --- the figures ------------------------------------------------------------


def test_strip_is_proportional_to_the_real_minutes():
    """The bar is sized in fr units equal to minutes, so it cannot flatter."""
    columns = strip_columns()
    assert sum(columns) == EXPECTED["gross_minutes"]
    assert max(columns) == EXPECTED["usable_minutes"]


def test_ledger_gross_and_total_match_the_fixture():
    assert ledger_row("Layover, gate to gate") == EXPECTED["gross_minutes"]
    assert ledger_row("Actually yours") == EXPECTED["usable_minutes"]


def test_ledger_deductions_sum_to_the_difference():
    deductions = [
        ledger_row("Disembark"),
        ledger_row("Immigration"),
        ledger_row("Transfer, both ways"),
        ledger_row("Check-in buffer"),
        ledger_row("Safety margin"),
    ]
    assert all(d < 0 for d in deductions)
    assert EXPECTED["gross_minutes"] + sum(deductions) == EXPECTED["usable_minutes"]


def test_window_times_match_the_fixture():
    start = local_hhmm(EXPECTED["window_start_local"])
    end = local_hhmm(EXPECTED["window_end_local"])
    assert f"Leave the airport {start} → be back by {end}" in PAGE


def test_headline_hours_match_usable_minutes():
    hours, minutes = divmod(EXPECTED["usable_minutes"], 60)
    assert f"{hours}h {minutes:02d}m yours" in PAGE


def test_money_figures_are_the_fixture_arithmetic():
    fare_saving = FIXTURE["baseline_price_eur"] - FIXTURE["price_eur"]
    assert money("Fare saved") == fare_saving
    assert money("Net") == fare_saving - FIXTURE["layover_plan_cost_eur"]


def test_plan_cost_is_broken_out_never_folded():
    """A single €13.25 is not legible; three lines are."""
    parts = money("Metro") + money("Admissions") + money("One meal")
    assert parts == FIXTURE["layover_plan_cost_eur"]


def test_estimated_prices_render_with_a_tilde():
    """§9: a guess displayed as an exact figure wears precision it has not earned."""
    import json as _json

    seed = _json.loads((ROOT / "hubs.seed.json").read_text())
    named = {"Dubai Museum, Al Fahidi Fort", "Abra across Dubai Creek",
             "Spice Souk, Deira", "Gold Souk, Deira"}
    for row in seed["activities"]:
        if row["hub_iata"] != "DXB" or row["name"] not in named or row["cost_eur"] == 0:
            continue
        rendered = f"~€{row['cost_eur']:.2f}" if row["confidence"] == "estimated" \
            else f"€{row['cost_eur']:.2f}"
        assert rendered in PAGE, f"{row['name']} should render as {rendered}"


def test_footer_says_the_meal_leans_against_the_layover():
    """Counting it in full is what makes the number defensible, not just honest."""
    assert "counted in full even though you would" in PAGE


def test_footer_names_the_placeholder_fares():
    for value in (FIXTURE["price_eur"], FIXTURE["baseline_price_eur"]):
        assert f"€{value:.0f}" in PAGE


def test_demo_banner_is_present():
    """§8.3: fixture fares must never travel unlabelled."""
    assert "Demo data — these fares are not real" in PAGE


def test_plan_names_the_generated_stops():
    """The stops are the §9 fill's actual output for this window, not a pick.

    tests/test_plan.py pins the same four in the same order; if the generator
    or the curated rows change, that test fails and this page is stale.
    """
    assert "TODO(step" not in PAGE
    for stop in ("Abra across Dubai Creek", "Spice Souk, Deira",
                 "Dubai Museum, Al Fahidi Fort", "Gold Souk, Deira"):
        assert stop in PAGE, stop
    assert "221 minutes allocated of 375" in PAGE
