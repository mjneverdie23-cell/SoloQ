"""Fare-versus-plan comparison — SPEC.md §4 and §10 step 7.

`net_saving_eur` is the number the whole product exists to display. When it is
negative the itinerary is honestly worse, and we say so rather than hiding it.
"""

from dataclasses import dataclass

from app.models import Itinerary, Segment


def duration_minutes(segments: list[Segment]) -> int:
    """First departure to last arrival, across however many legs."""
    return int(
        (segments[-1].arrival_local - segments[0].departure_local).total_seconds() // 60
    )


@dataclass(frozen=True)
class Comparison:
    """The three headline figures are derived, never stored (hard rule 7)."""

    itinerary_price_eur: float
    baseline_price_eur: float
    layover_plan_cost_eur: float
    itinerary_duration_minutes: int
    baseline_duration_minutes: int

    @property
    def fare_saving_eur(self) -> float:
        return self.baseline_price_eur - self.itinerary_price_eur

    @property
    def net_saving_eur(self) -> float:
        return self.fare_saving_eur - self.layover_plan_cost_eur

    @property
    def extra_hours(self) -> float:
        """How much longer the cheap routing takes than the fast one."""
        return (self.itinerary_duration_minutes - self.baseline_duration_minutes) / 60

    @property
    def is_worse_than_flying_direct(self) -> bool:
        """Surfaced plainly in the UI, not buried (SPEC.md §4)."""
        return self.net_saving_eur < 0

    @property
    def euros_per_extra_hour(self) -> float | None:
        """What the traveller is paid per hour of extra travel, after the plan.

        None when the itinerary is not actually slower, since the rate is
        meaningless then.
        """
        if self.extra_hours <= 0:
            return None
        return self.net_saving_eur / self.extra_hours


def comparison_of(
    itin: Itinerary,
    *,
    baseline_price_eur: float,
    baseline_duration_minutes: int,
    layover_plan_cost_eur: float,
) -> Comparison:
    return Comparison(
        itinerary_price_eur=itin.price_eur,
        baseline_price_eur=baseline_price_eur,
        layover_plan_cost_eur=layover_plan_cost_eur,
        itinerary_duration_minutes=duration_minutes(itin.outbound),
        baseline_duration_minutes=baseline_duration_minutes,
    )
