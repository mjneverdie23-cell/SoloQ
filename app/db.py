"""SQLite connection and schema for the seeded reference data (SPEC.md §4)."""

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "layover.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hub (
    iata                    TEXT PRIMARY KEY,
    city                    TEXT    NOT NULL,
    country_iso2            TEXT    NOT NULL,
    is_schengen             INTEGER NOT NULL,
    transfer_minutes        INTEGER NOT NULL,
    transfer_cost_eur       REAL    NOT NULL,
    transfer_mode           TEXT    NOT NULL,
    transfer_note           TEXT,
    disembark_minutes       INTEGER NOT NULL,
    immigration_minutes     INTEGER NOT NULL,
    recheck_buffer_minutes  INTEGER NOT NULL,
    has_left_luggage        INTEGER NOT NULL,
    activity_density        REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS entry_rule (
    passport_scope          TEXT    NOT NULL,
    country_iso2            TEXT    NOT NULL,
    entry_type              TEXT    NOT NULL,
    max_stay_days           INTEGER,
    passport_validity_days  INTEGER NOT NULL,
    notes                   TEXT    NOT NULL,
    verified_on             TEXT,
    PRIMARY KEY (passport_scope, country_iso2)
);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the database, creating the schema if it is not there yet."""
    conn = sqlite3.connect(path or os.environ.get("LAYOVER_DB", DEFAULT_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
