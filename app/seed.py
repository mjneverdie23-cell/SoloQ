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


def load_seed(conn: sqlite3.Connection, seed_path: Path = SEED_PATH) -> None:
    seed = json.loads(seed_path.read_text())
    # Airports first: hub.iata is a foreign key into them.
    _insert(conn, "airport", AIRPORT_COLUMNS, seed["airports"])
    _insert(conn, "hub", HUB_COLUMNS, seed["hubs"])
    _insert(conn, "entry_rule", ENTRY_RULE_COLUMNS, seed["entry_rules"])
    conn.commit()


if __name__ == "__main__":
    conn = connect()
    load_seed(conn)
    counts = [
        conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("airport", "hub", "entry_rule")
    ]
    conn.close()
    print("seeded {} airports, {} hubs, {} entry rules".format(*counts))
