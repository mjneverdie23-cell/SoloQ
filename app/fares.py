"""Fare sources — SPEC.md §8.1.

The scoring layer must not be able to tell which source it got, so both return
parsed `Itinerary` objects rather than anything provider-shaped.
"""

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from app.models import Itinerary, Segment

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "itineraries"
DEFAULT_SOURCE = "fixture"


class FareSource(Protocol):
    def search(self, origin: str, dest: str, depart: date) -> list[Itinerary]: ...


def itinerary_of(fixture: dict) -> Itinerary:
    return Itinerary(
        id=fixture["id"],
        price_eur=fixture["price_eur"],
        is_single_ticket=fixture["is_single_ticket"],
        outbound=[_segment(s) for s in fixture["outbound"]],
        inbound=[_segment(s) for s in fixture["inbound"]],
    )


class FixtureSource:
    """Hand-written itineraries, committed. No network, no quota."""

    def __init__(self, directory: Path = FIXTURE_DIR):
        self.directory = directory

    def load_all(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.directory.glob("*.json"))]

    def search(self, origin: str, dest: str, depart: date) -> list[Itinerary]:
        return [
            itinerary_of(fixture)
            for fixture in self.load_all()
            if fixture["outbound"][0]["origin"] == origin
            and fixture["outbound"][-1]["destination"] == dest
            and datetime.fromisoformat(
                fixture["outbound"][0]["departure_local"]
            ).date() == depart
        ]


def selected_source_name() -> str:
    return os.environ.get("FARE_SOURCE", DEFAULT_SOURCE)


def is_demo_mode() -> bool:
    """§8.3: fixture fares are not real and every page must say so."""
    return selected_source_name() == "fixture"


def fare_source() -> FareSource:
    name = selected_source_name()
    if name == "fixture":
        return FixtureSource()
    if name == "amadeus":
        raise NotImplementedError("AmadeusSource is step 9")
    raise ValueError(f"unknown FARE_SOURCE {name!r}")


def _segment(raw: dict) -> Segment:
    return Segment(
        carrier=raw["carrier"],
        flight_number=raw["flight_number"],
        origin=raw["origin"],
        destination=raw["destination"],
        departure_local=datetime.fromisoformat(raw["departure_local"]),
        arrival_local=datetime.fromisoformat(raw["arrival_local"]),
        is_international=raw["is_international"],
    )
