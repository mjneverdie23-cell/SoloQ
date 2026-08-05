"""Airport reference lookups."""

import sqlite3


def load_schengen_airports(conn: sqlite3.Connection) -> frozenset[str]:
    """The Schengen airports of the seeded dataset.

    Callers pass the result into the scoring functions rather than having those
    functions reach into the database themselves (SPEC.md §5.1).
    """
    rows = conn.execute("SELECT iata FROM airport WHERE is_schengen = 1")
    return frozenset(row["iata"] for row in rows)
