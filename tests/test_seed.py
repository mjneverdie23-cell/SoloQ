import pytest

from app.db import connect
from app.seed import load_seed

# `transfer_note` is `str | None` in the Hub dataclass (SPEC.md §4).
NULLABLE_HUB_COLUMNS = {"transfer_note"}
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
        assert hub["is_schengen"] in (0, 1)
        assert hub["has_left_luggage"] in (0, 1)
        assert 0.0 <= hub["activity_density"] <= 1.0


def test_transfer_note_survives_the_load(conn):
    note = conn.execute("SELECT transfer_note FROM hub WHERE iata = 'DXB'").fetchone()[0]
    assert "Friday" in note


def test_seeding_twice_is_idempotent(conn):
    load_seed(conn)
    assert len(rows(conn, "hub")) == 6
    assert len(rows(conn, "entry_rule")) == 5


def test_db_path_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LAYOVER_DB", str(tmp_path / "from-env.db"))
    conn = connect()
    load_seed(conn)
    conn.close()
    assert (tmp_path / "from-env.db").exists()
