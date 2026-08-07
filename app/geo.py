"""Airport and entry-rule reference lookups."""

import sqlite3

from app.models import EntryRule

# v0 has exactly one passport scope (SPEC.md §2), so it is pinned rather than
# selected. A UI selector is a v1 concern.
PASSPORT_SCOPE = "NORDIC"


def load_schengen_airports(conn: sqlite3.Connection) -> frozenset[str]:
    """The Schengen airports of the seeded dataset.

    Callers pass the result into the scoring functions rather than having those
    functions reach into the database themselves (SPEC.md §5.1).
    """
    rows = conn.execute("SELECT iata FROM airport WHERE is_schengen = 1")
    return frozenset(row["iata"] for row in rows)


def load_entry_rule(conn: sqlite3.Connection, hub_iata: str) -> EntryRule:
    """The entry rule that applies at a hub: hub.iata -> country -> rule."""
    row = conn.execute(
        """
        SELECT r.* FROM entry_rule r
        JOIN airport a ON a.country_iso2 = r.country_iso2
        WHERE a.iata = ? AND r.passport_scope = ?
        """,
        (hub_iata, PASSPORT_SCOPE),
    ).fetchone()
    if row is None:
        raise LookupError(f"no {PASSPORT_SCOPE} entry rule for hub {hub_iata}")
    return EntryRule(**dict(row))
