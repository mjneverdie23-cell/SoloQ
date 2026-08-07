"""Load hubs.seed.json into SQLite. Idempotent — safe to re-run."""

import json
import sqlite3
from pathlib import Path

from app.db import connect

SEED_PATH = Path(__file__).resolve().parent.parent / "hubs.seed.json"

AIRPORT_COLUMNS = (
    "iata",
    "city",
    "country_iso2",
    "is_schengen",
    "tz_name",
    "verified_on",
)

HUB_COLUMNS = (
    "iata",
    "transfer_minutes",
    "transfer_cost_eur",
    "transfer_mode",
    "transfer_note",
    "disembark_minutes",
    "immigration_minutes",
    "recheck_buffer_minutes",
    "has_left_luggage",
    "activity_density",
    "meal_cost_eur",
    "overnight_cost_eur",
    "verified_on",
)

ACTIVITY_COLUMNS = (
    "hub_iata",
    "name",
    "lat",
    "lon",
    "interest",
    "minutes_needed",
    "cost_eur",
    "opens_local",
    "closes_local",
    "transfer_minutes_from_centre",
    "verified_on",
)

ENTRY_RULE_COLUMNS = (
    "passport_scope",
    "country_iso2",
    "entry_type",
    "max_stay_days",
    "passport_validity_days",
    "notes",
    "verified_on",
)


def _insert(conn: sqlite3.Connection, table: str, columns: tuple[str, ...], rows: list[dict]) -> None:
    sql = "INSERT OR REPLACE INTO {} ({}) VALUES ({})".format(
        table, ", ".join(columns), ", ".join("?" * len(columns))
    )
    # A column missing from the seed lands as NULL and trips the NOT NULL
    # constraint, which is the loud failure we want.
    conn.executemany(sql, [[row.get(column) for column in columns] for row in rows])


def reject_duplicate_keys(pairs: list[tuple]) -> dict:
    """`json.loads` keeps the last duplicate and says nothing about it.

    A regex insert once wrote six meal costs into one hub row; the file parsed
    cleanly and the hub silently took the last of them. Guarding the load path
    rather than only the test means a corrupt seed cannot reach runtime.
    """
    keys = [key for key, _ in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"duplicate keys in seed: {duplicates}")
    return dict(pairs)


def load_seed(conn: sqlite3.Connection, seed_path: Path = SEED_PATH) -> None:
    seed = json.loads(seed_path.read_text(), object_pairs_hook=reject_duplicate_keys)
    # Airports first: hub.iata is a foreign key into them.
    _insert(conn, "airport", AIRPORT_COLUMNS, seed["airports"])
    _insert(conn, "hub", HUB_COLUMNS, seed["hubs"])
    _insert(conn, "entry_rule", ENTRY_RULE_COLUMNS, seed["entry_rules"])
    _insert(conn, "activity", ACTIVITY_COLUMNS, seed["activities"])
    conn.commit()


if __name__ == "__main__":
    conn = connect()
    load_seed(conn)
    counts = [
        conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("airport", "hub", "entry_rule", "activity")
    ]
    conn.close()
    print("seeded {} airports, {} hubs, {} entry rules, {} activities".format(*counts))
