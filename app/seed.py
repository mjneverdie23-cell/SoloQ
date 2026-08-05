"""Load hubs.seed.json into SQLite. Idempotent — safe to re-run."""

import json
import sqlite3
from pathlib import Path

from app.db import connect

SEED_PATH = Path(__file__).resolve().parent.parent / "hubs.seed.json"

HUB_COLUMNS = (
    "iata",
    "city",
    "country_iso2",
    "is_schengen",
    "transfer_minutes",
    "transfer_cost_eur",
    "transfer_mode",
    "transfer_note",
    "disembark_minutes",
    "immigration_minutes",
    "exit_control_minutes",
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
    _insert(conn, "hub", HUB_COLUMNS, seed["hubs"])
    _insert(conn, "entry_rule", ENTRY_RULE_COLUMNS, seed["entry_rules"])
    conn.commit()


if __name__ == "__main__":
    conn = connect()
    load_seed(conn)
    hubs = conn.execute("SELECT count(*) FROM hub").fetchone()[0]
    rules = conn.execute("SELECT count(*) FROM entry_rule").fetchone()[0]
    conn.close()
    print(f"seeded {hubs} hubs, {rules} entry rules")
