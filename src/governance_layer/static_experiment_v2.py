"""Static paired comparison with corrected execution-retention accounting.

The scenario, seeds, prices, strategist orders, arms, and governance parameters
are imported unchanged from :mod:`governance_layer.static_experiment`; only the
retention *measurement* differs.  Nothing here recomputes or replaces the frozen
V1 result, which remains available from ``run_static_paired_comparison``.
"""

from __future__ import annotations

from random import Random
from typing import Any

from .execution_audit import StockSimExecutionAuditBridge
from .fill_retention import FILL_RETENTION_VERSION, FillRetentionAccumulator
from .static_experiment import (
    ARMS,
    SCENARIOS,
    _controller_for_arm,
    _decision_for_arm,
    _observation,
    _Portfolio,
    _proposal,
    _strategist_order,
)

# Index 9 is cancelled before any fill by scenario construction, so no arm can
# retain it.  It stays in the experiment and leaves the retention denominator.
STRUCTURALLY_UNFILLABLE_INDICES = frozenset({9})


def _single_run(arm: str, seed: int) -> dict[str, Any]:
    random = Random(seed)
    prices = [100.0]
    for _ in range(17):
        prices.append(prices[-1] * (1.0 + random.uniform(-0.012, 0.012)))
    controller, _ = _controller_for_arm(arm)
    portfolio = _Portfolio()
    bridge = StockSimExecutionAuditBridge()
    retention = FillRetentionAccumulator(venue_routed=arm != "shadow")

    for index, price in enumerate(prices):
        state = controller.evaluate(_observation(index)).governance_state
        side, quantity, order_type = _strategist_order(index, price)
        order = _proposal(seed, index, side, quantity, order_type, price)
        decision = _decision_for_arm(arm, order, portfolio, state, index)
        retention.observe_order(
            order.order_id,
            proposed_quantity=order.quantity,
            authorized_quantity=decision.allowed_quantity if decision.allowed else 0,
            venue_eligible=index not in STRUCTURALLY_UNFILLABLE_INDICES,
        )
        if arm == "shadow" or not (decision.allowed and decision.allowed_quantity):
            continue
        if index in STRUCTURALLY_UNFILLABLE_INDICES:
            continue
        bridge.register_authorization(
            order,
            decision,
            pre_long_position=portfolio.long,
            pre_short_position=portfolio.short,
            governance_state=state,
        )
        if index == 2:
            first_fill = min(2, decision.allowed_quantity)
            fills = [first_fill, decision.allowed_quantity - first_fill]
        else:
            fills = [decision.allowed_quantity]
        if index == 10:
            bridge.request_cancel(order.order_id)
        for fill in filter(None, fills):
            before_long, before_short = portfolio.long, portfolio.short
            after_long, after_short = portfolio.execute(order.side, fill, price)
            bridge.record_execution(
                order.order_id,
                fill_quantity=fill,
                execution_price=price,
                pre_long_position=before_long,
                pre_short_position=before_short,
                post_long_position=after_long,
                post_short_position=after_short,
            )
            retention.observe_fill(order.order_id, fill)
    return retention.to_dict()


def _mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)


def run_corrected_retention_comparison(
    seeds: tuple[int, ...] = (20261101, 20261102, 20261103)
) -> dict[str, Any]:
    """Re-measure retention on the unchanged static arms, seeds, and orders."""

    if not seeds:
        raise ValueError("at least one paired seed is required")
    arms: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        rows = [_single_run(arm, seed) for seed in seeds]
        arms[arm] = {
            "venue_routed": rows[0]["venue_routed"],
            "fill_retention_rate": _mean([row["fill_retention_rate"] for row in rows]),
            "filled_order_rate": _mean([row["filled_order_rate"] for row in rows]),
            "authorized_retention_rate": _mean(
                [row["authorized_retention_rate"] for row in rows]
            ),
            "execution_conversion_rate": _mean(
                [row["execution_conversion_rate"] for row in rows]
            ),
            "proposed_quantity": sum(row["proposed_quantity"] for row in rows),
            "authorized_quantity": sum(row["authorized_quantity"] for row in rows),
            "executed_quantity": sum(row["executed_quantity"] for row in rows),
            "eligible_order_count": sum(row["eligible_order_count"] for row in rows),
            "excluded_order_count": sum(row["excluded_order_count"] for row in rows),
            "fill_event_count": sum(row["fill_event_count"] for row in rows),
        }
    return {
        "experiment": "v2_1_static_paired_retention_correction_v1",
        "fill_retention_version": FILL_RETENTION_VERSION,
        "llm_api_used": False,
        "seeds": list(seeds),
        "scenario_coverage": list(SCENARIOS),
        "supersedes_metric": "fill_retention_rate (v1: fill_events / orders_proposed)",
        "arms": arms,
    }
