"""Corrected fill-retention accounting for post-V2.1 experiments.

The V1 diagnostic reported ``fill_retention_rate = fill_events / orders_proposed``.
That ratio mixes three different populations:

* its numerator counts *fill events*, so one order filled in two partials scores
  twice as high as the same order filled once;
* its denominator counts every strategist order, including orders the scenario
  cancels before any fill by construction, which no arm can ever retain;
* an arm that by definition never routes to a venue has a structurally empty
  numerator against a non-empty denominator, which reads as "0% retention"
  when the correct statement is "not defined for this arm".

The corrected quantity is retained *executed quantity* against the *venue-eligible
proposed quantity*, with an explicit ``None`` for arms that do not execute.  The
historical V1 numbers are not recomputed; this module is only used by new runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FILL_RETENTION_VERSION = "fill_retention_v2"


@dataclass
class FillRetentionAccumulator:
    """Order-level ledger behind the corrected retention rates."""

    venue_routed: bool = True
    proposed_quantity: int = 0
    authorized_quantity: int = 0
    executed_quantity: int = 0
    eligible_order_count: int = 0
    excluded_order_count: int = 0
    filled_order_count: int = 0
    fill_event_count: int = 0
    _filled_orders: set[str] = field(default_factory=set)

    def observe_order(
        self,
        order_id: str,
        *,
        proposed_quantity: int,
        authorized_quantity: int,
        venue_eligible: bool = True,
    ) -> None:
        """Record one strategist order and whether it could fill at all."""

        if proposed_quantity <= 0:
            raise ValueError("a proposed order must have positive quantity")
        if not venue_eligible:
            self.excluded_order_count += 1
            return
        self.eligible_order_count += 1
        self.proposed_quantity += proposed_quantity
        self.authorized_quantity += min(authorized_quantity, proposed_quantity)

    def observe_fill(self, order_id: str, fill_quantity: int) -> None:
        """Record one actual fill; several partials on one order stay one order."""

        if fill_quantity <= 0:
            raise ValueError("a fill must have positive quantity")
        self.executed_quantity += fill_quantity
        self.fill_event_count += 1
        if order_id not in self._filled_orders:
            self._filled_orders.add(order_id)
            self.filled_order_count += 1

    # -- rates -------------------------------------------------------------

    @property
    def fill_retention_rate(self) -> float | None:
        """Executed quantity over venue-eligible proposed quantity.

        ``None`` for an arm whose orders never reach an execution venue: the
        quantity is undefined there, not zero.
        """

        if not self.venue_routed:
            return None
        if self.proposed_quantity == 0:
            return None
        return self.executed_quantity / self.proposed_quantity

    @property
    def filled_order_rate(self) -> float | None:
        if not self.venue_routed:
            return None
        if self.eligible_order_count == 0:
            return None
        return self.filled_order_count / self.eligible_order_count

    @property
    def authorized_retention_rate(self) -> float | None:
        """Authorized over proposed quantity: defined for every arm, including shadow.

        This is the counterfactual an observe-only arm can legitimately report,
        because it depends on the decision alone and not on execution.
        """

        if self.proposed_quantity == 0:
            return None
        return self.authorized_quantity / self.proposed_quantity

    @property
    def execution_conversion_rate(self) -> float | None:
        """Executed over authorized quantity: venue behaviour, not governance."""

        if not self.venue_routed or self.authorized_quantity == 0:
            return None
        return self.executed_quantity / self.authorized_quantity

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_retention_version": FILL_RETENTION_VERSION,
            "venue_routed": self.venue_routed,
            "proposed_quantity": self.proposed_quantity,
            "authorized_quantity": self.authorized_quantity,
            "executed_quantity": self.executed_quantity,
            "eligible_order_count": self.eligible_order_count,
            "excluded_order_count": self.excluded_order_count,
            "filled_order_count": self.filled_order_count,
            "fill_event_count": self.fill_event_count,
            "fill_retention_rate": self.fill_retention_rate,
            "filled_order_rate": self.filled_order_rate,
            "authorized_retention_rate": self.authorized_retention_rate,
            "execution_conversion_rate": self.execution_conversion_rate,
        }
