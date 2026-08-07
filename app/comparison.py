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
    usable_minutes: int

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
    def dead_hours(self) -> float:
        """Extra hours that are not the product.

        A day in Riga is the thing being sold, not a penalty. Only the hours
        with nothing in them — queuing, transferring, sitting airside — are
        cost, so the usable window comes back out.
        """
        return self.extra_hours - self.usable_minutes / 60

    @property
    def euros_per_extra_hour(self) -> float | None:
        """What the traveller is paid per hour of extra travel, after the plan.

        Treats every extra hour as cost, which overstates it. Kept alongside
        euros_per_dead_hour rather than replaced, because it is the number a
        sceptic reaches for first.
        """
        if self.extra_hours <= 0:
            return None
        return self.net_saving_eur / self.extra_hours

    @property
    def euros_per_dead_hour(self) -> float | None:
        """The honest rate: net saving against the hours that buy nothing.

        None when there are no dead hours. That cannot happen with the current
        deductions, but a future hub with a very short transfer could approach
        it, and dividing by near-zero produces a spectacular number with
        nothing behind it.
        """
        if self.dead_hours <= 0:
            return None
        return self.net_saving_eur / self.dead_hours


def comparison_of(
    itin: Itinerary,
    *,
    baseline_price_eur: float,
    baseline_duration_minutes: int,
    layover_plan_cost_eur: float,
    usable_minutes: int,
) -> Comparison:
    return Comparison(
        itinerary_price_eur=itin.price_eur,
        baseline_price_eur=baseline_price_eur,
        layover_plan_cost_eur=layover_plan_cost_eur,
        itinerary_duration_minutes=duration_minutes(itin.outbound),
        baseline_duration_minutes=baseline_duration_minutes,
        usable_minutes=usable_minutes,
    )
