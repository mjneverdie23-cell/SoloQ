import sqlite3

import pytest

from app.db import connect
from app.seed import load_seed

# `transfer_note` is `str | None` in the Hub dataclass (SPEC.md §4).
NULLABLE_HUB_COLUMNS = {"transfer_note", "verified_on"}
# `max_stay_days` is `int | None`; `verified_on` is null across the whole seed
# by design — see test_every_entry_rule_is_unverified.
NULLABLE_ENTRY_RULE_COLUMNS = {"max_stay_days", "verified_on"}


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path / "layover.db")
    load_seed(conn)
    yield conn
    conn.close()


def rows(conn, table):
    return conn.execute(f"SELECT * FROM {table}").fetchall()


def test_seventeen_airports_load(conn):
    # OSL, the six hubs, the ten destinations.
    assert len(rows(conn, "airport")) == 17


def test_only_osl_waw_rix_are_schengen(conn):
    schengen = {a["iata"] for a in rows(conn, "airport") if a["is_schengen"]}
    assert schengen == {"OSL", "WAW", "RIX"}


def test_every_airport_is_unverified(conn):
    # Schengen membership changes — Croatia 2023, Bulgaria and Romania 2025 —
    # so it is exactly the class of fact SPEC.md §7 exists for.
    assert [a["verified_on"] for a in rows(conn, "airport")] == [None] * 17


def test_every_hub_has_an_airport(conn):
    airports = {a["iata"] for a in rows(conn, "airport")}
    assert {hub["iata"] for hub in rows(conn, "hub")} <= airports


def test_foreign_keys_are_on_for_every_connection(tmp_path):
    # The pragma is per-connection, not per-database: a second connection would
    # silently lose it if connect() were not the only way one gets opened.
    for _ in range(2):
        other = connect(tmp_path / "layover.db")
        assert other.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        other.close()


def test_hub_without_an_airport_is_rejected(conn):
    # A complete DXB row renamed, so the foreign key is the only thing wrong
    # with it — a partial row would trip NOT NULL first and pass for free.
    orphan = dict(conn.execute("SELECT * FROM hub WHERE iata = 'DXB'").fetchone())
    orphan["iata"] = "ZZZ"
    columns = ", ".join(orphan)
    placeholders = ", ".join("?" * len(orphan))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(f"INSERT INTO hub ({columns}) VALUES ({placeholders})", list(orphan.values()))


def test_six_hubs_load(conn):
    assert len(rows(conn, "hub")) == 6


def test_five_entry_rules_load(conn):
    # Six hubs, five rules: DXB and AUH are both AE (SPEC.md §6).
    assert len(rows(conn, "entry_rule")) == 5


def test_hub_non_nullable_fields_populated(conn):
    for hub in rows(conn, "hub"):
        for column in hub.keys():
            if column not in NULLABLE_HUB_COLUMNS:
                assert hub[column] is not None, f"{hub['iata']}.{column} is NULL"


def test_entry_rule_non_nullable_fields_populated(conn):
    for rule in rows(conn, "entry_rule"):
        for column in rule.keys():
            if column not in NULLABLE_ENTRY_RULE_COLUMNS:
                assert rule[column] is not None, f"{rule['country_iso2']}.{column} is NULL"


def test_every_entry_rule_is_unverified(conn):
    # SPEC.md §7: none of this may drive user-facing output until each row is
    # checked against an official source and `verified_on` stamped. Asserting
    # it keeps the unverified state a visible fact rather than a silent one.
    assert [rule["verified_on"] for rule in rows(conn, "entry_rule")] == [None] * 5


def test_durations_that_must_not_be_zero(conn):
    # NOT NULL does not catch a zero, and a silent zero in any of these
    # corrupts every downstream usable_minutes calculation (SPEC.md §5.1).
    for hub in rows(conn, "hub"):
        for column in (
            "transfer_minutes",
            "disembark_minutes",
            "immigration_minutes",
            "recheck_buffer_minutes",
        ):
            assert hub[column] > 0, f"{hub['iata']}.{column} is {hub[column]}"


def test_schengen_hubs_carry_the_shorter_buffer(conn):
    # The 180/120 split is what encodes exit control (SPEC.md §5.1), so it is
    # the only place that difference survives now the field is gone.
    buffers = {hub["iata"]: hub["recheck_buffer_minutes"] for hub in rows(conn, "hub")}
    assert buffers["WAW"] == 120
    assert buffers["RIX"] == 120
    assert all(buffers[iata] == 180 for iata in ("IST", "DOH", "DXB", "AUH"))


def test_hub_flags_and_density_in_range(conn):
    for hub in rows(conn, "hub"):
        assert hub["has_left_luggage"] in (0, 1)
        assert 0.0 <= hub["activity_density"] <= 1.0
    for airport in rows(conn, "airport"):
        assert airport["is_schengen"] in (0, 1)


def test_transfer_note_survives_the_load(conn):
    note = conn.execute("SELECT transfer_note FROM hub WHERE iata = 'DXB'").fetchone()[0]
    assert "Friday" in note


def test_seeding_twice_is_idempotent(conn):
    load_seed(conn)
    assert len(rows(conn, "airport")) == 17
    assert len(rows(conn, "hub")) == 6
    assert len(rows(conn, "entry_rule")) == 5


def test_db_path_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LAYOVER_DB", str(tmp_path / "from-env.db"))
    conn = connect()
    load_seed(conn)
    conn.close()
    assert (tmp_path / "from-env.db").exists()


def test_every_airport_has_a_real_iana_zone(conn):
    """§5.4 needs a zone, not an offset. A fixed offset cannot do DST."""
    from zoneinfo import ZoneInfo

    for airport in rows(conn, "airport"):
        assert "/" in airport["tz_name"], airport["iata"]
        ZoneInfo(airport["tz_name"])   # raises if it is not a real zone


def test_missing_entry_rule_says_which_hub(conn):
    from app.geo import load_entry_rule

    conn.execute("DELETE FROM entry_rule WHERE country_iso2 = 'AE'")
    with pytest.raises(LookupError, match="no NORDIC entry rule for hub DXB"):
        load_entry_rule(conn, "DXB")


def test_each_hub_carries_its_own_cost_figures(conn):
    """Per-hub values, pinned individually.

    A regex insert once stacked all six meal costs into IST's row. JSON keeps
    the last duplicate, so IST silently read 8.00 instead of its own figure and
    every other hub was still correct — a wrong value, not a missing one, which
    the non-null sweep cannot see. These assertions can.
    """
    costs = {
        h["iata"]: (h["meal_cost_eur"], h["overnight_cost_eur"])
        for h in rows(conn, "hub")
    }
    assert costs == {
        "IST": (7.00, 25.00),
        "DOH": (6.00, 40.00),
        "DXB": (8.00, 35.00),
        "AUH": (8.00, 30.00),
        "WAW": (7.00, 22.00),
        "RIX": (8.00, 25.00),
    }


def test_seed_json_has_no_duplicate_keys():
    """json.loads silently keeps the last duplicate, so parse strictly instead."""
    import json
    from pathlib import Path

    def reject_duplicates(pairs):
        seen = [k for k, _ in pairs]
        assert len(seen) == len(set(seen)), f"duplicate keys: {seen}"
        return dict(pairs)

    path = Path(__file__).resolve().parent.parent / "hubs.seed.json"
    json.loads(path.read_text(), object_pairs_hook=reject_duplicates)
