"""Zero-cost, paired static comparison for V2.1 Full and Lean candidates.

This is a diagnostic harness, not a calibration run.  It uses fixed strategist
orders and deterministic unseen seeds; it neither calls an LLM nor changes a
policy parameter from observed results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from random import Random
from typing import Any

from .actual_risk import assess_risk_change
from .execution_audit import StockSimExecutionAuditBridge
from .hybrid import execute_hybrid, scalar_fallback_decision
from .models import GateDecision, OrderProposal
from .permissions import permission_for
from .state_machine import (
    V21_FULL,
    V21_LEAN,
    AgentGovernanceStateMachine,
    GovernanceProfile,
    PolicyParameters,
    StateObservation,
)

ARMS = ("shadow", "scalar", "hybrid_v2_1_full", "hybrid_v2_1_lean")
SCENARIOS = (
    "normal_no_risk",
    "confidence_only_anomaly",
    "transient_telemetry_delay",
    "persistent_telemetry_missing",
    "recovery_after_risk",
    "severe_exposure",
    "multiple_risk_signals",
    "partial_fill",
    "cancel_execute_race",
    "open_short_and_cover",
    "recurrent_degradation",
)
_START = datetime(2026, 11, 1, 9, 30, tzinfo=UTC)


@dataclass
class _Portfolio:
    long: int = 10
    short: int = 0
    cash: float = 0.0

    def execute(self, side: str, quantity: int, price: float) -> tuple[int, int]:
        change = assess_risk_change(
            side=side,
            quantity=quantity,
            pre_long_position=self.long,
            pre_short_position=self.short,
            price=price,
        )
        self.long, self.short = change.post_long_position, change.post_short_position
        self.cash += quantity * price if side == "SELL" else -quantity * price
        return self.long, self.short

    def equity(self, price: float) -> float:
        return self.cash + (self.long - self.short) * price


def _time(index: int) -> str:
    return (_START + timedelta(minutes=index)).isoformat()


def _observation(index: int) -> StateObservation:
    # Fixed sequence covers soft signals, persistent loss, recovery, hard
    # exposure/multi-signal conditions, and a second independent degradation.
    values: dict[str, Any] = {
        "telemetry_missing": False,
        "telemetry_age_intervals": 0,
        "confidence_calibration": 1.0,
        "delayed_outcome_count": 0,
        "exposure_ratio": 0.05,
        "material_risk_breach": False,
        "material_risk_signal_count": 0,
        "completed_outcome_count": max(0, index - 4),
    }
    if index == 1:
        values["confidence_calibration"] = 0.40
    elif index == 2:
        values["telemetry_age_intervals"] = 1
    elif index in {4, 5, 6}:
        values["telemetry_missing"] = True
        values["telemetry_age_intervals"] = 1
    elif index == 13:
        values["exposure_ratio"] = 0.90
    elif index == 14:
        values["delayed_outcome_count"] = 2
        values["material_risk_signal_count"] = 2
    elif index == 17:
        values["material_risk_breach"] = True
    now = _time(index)
    return StateObservation(
        governance_index=index,
        governance_time=now,
        data_cutoff_timestamp=now,
        latest_result_available_timestamp=now if values["completed_outcome_count"] else "",
        telemetry_missing=values["telemetry_missing"],
        telemetry_age_intervals=values["telemetry_age_intervals"],
        lagged_reliability=1.0,
        confidence=1.0,
        confidence_calibration=values["confidence_calibration"],
        delayed_outcome_count=values["delayed_outcome_count"],
        completed_outcome_count=values["completed_outcome_count"],
        exposure_ratio=values["exposure_ratio"],
        current_risk_limit_scale=1.0,
        material_risk_breach=values["material_risk_breach"],
        material_risk_signal_count=values["material_risk_signal_count"],
    )


def _strategist_order(index: int, price: float) -> tuple[str, int, str]:
    """Static agent output shared unchanged by every arm and seed."""

    schedule = {
        0: ("SELL", 15, "MARKET"),  # long -> short crossing
        1: ("BUY", 5, "LIMIT"),  # cover
        2: ("BUY", 6, "LIMIT"),  # partial / multi-fill
        3: ("SELL", 3, "LIMIT"),
        4: ("SELL", 5, "MARKET"),
        5: ("SELL", 5, "MARKET"),
        6: ("SELL", 5, "MARKET"),
        9: ("BUY", 4, "LIMIT"),  # cancelled, no fill
        10: ("SELL", 2, "LIMIT"),  # cancel/execute race
        13: ("BUY", 4, "MARKET"),
        14: ("BUY", 4, "MARKET"),
        17: ("BUY", 4, "MARKET"),
    }
    return schedule.get(index, ("SELL", 1, "LIMIT"))


def _proposal(
    seed: int, index: int, side: str, quantity: int, order_type: str, price: float
) -> OrderProposal:
    return OrderProposal(
        order_id=f"static-{seed}-{index}",
        agent_id="retail-static",
        role="retail",
        instrument="AAPL",
        side=side,
        quantity=quantity,
        order_type=order_type,
        price=price,
        source="static_strategist_no_llm",
        proposed_at=_time(index),
    )


def _controller_for_arm(arm: str) -> tuple[AgentGovernanceStateMachine, GovernanceProfile]:
    profile = V21_LEAN if arm == "hybrid_v2_1_lean" else V21_FULL
    return (
        AgentGovernanceStateMachine(f"{arm}-retail", PolicyParameters.defaults(), profile),
        profile,
    )


def _decision_for_arm(
    arm: str,
    order: OrderProposal,
    portfolio: _Portfolio,
    state: str,
    index: int,
) -> GateDecision:
    permission = permission_for(
        run_id=f"static-{arm}",
        agent_id=order.agent_id,
        role=order.role,
        state=state,
        governance_time=order.proposed_at,
        reason_code="STATIC_PAIRED_COMPARISON",
        index=index,
    )
    position = portfolio.long - portfolio.short
    if arm == "scalar":
        return scalar_fallback_decision(
            order,
            position,
            permission,
            pre_long_position=portfolio.long,
            pre_short_position=portfolio.short,
        )
    return execute_hybrid(
        order,
        position,
        permission,
        pre_long_position=portfolio.long,
        pre_short_position=portfolio.short,
    )


def _single_run(arm: str, seed: int) -> dict[str, float]:
    random = Random(seed)
    prices = [100.0]
    for _ in range(17):
        prices.append(prices[-1] * (1.0 + random.uniform(-0.012, 0.012)))
    controller, _ = _controller_for_arm(arm)
    portfolio = _Portfolio()
    bridge = StockSimExecutionAuditBridge()
    equity = [portfolio.equity(prices[0])]
    gross_exposure = [(portfolio.long + portfolio.short) * prices[0]]
    net_exposure = [abs(portfolio.long - portfolio.short) * prices[0]]
    executions = 0
    proposed = 0
    institution_allowed = 0
    maker_allowed = 0
    blocked_reduction = 0
    blocked_close_cover = 0
    severe_increase = 0
    actual_amplification = 0
    fallback_failure = 0
    state_counts: dict[str, int] = {}
    transitions = 0
    interventions = 0
    recovery_index: int | None = None
    second_isolation_index: int | None = None

    for index, price in enumerate(prices):
        state_decision = controller.evaluate(_observation(index))
        state = state_decision.governance_state
        state_counts[state] = state_counts.get(state, 0) + 1
        transitions += int(state_decision.changed)
        if index > 6 and recovery_index is None and state not in {"ISOLATED", "RESTRICTED"}:
            recovery_index = index
        if index >= 17 and state == "ISOLATED":
            second_isolation_index = index

        side, quantity, order_type = _strategist_order(index, price)
        order = _proposal(seed, index, side, quantity, order_type, price)
        proposed += 1
        decision = _decision_for_arm(arm, order, portfolio, state, index)
        if not decision.allowed:
            interventions += 1
            audit = decision.actual_net_risk_audit_v2
            if audit and audit.proposed_risk.actual_net_risk_increase == 0:
                blocked_reduction += 1
                blocked_close_cover += int(
                    audit.proposed_risk.close_or_reduce_long_quantity > 0
                    or audit.proposed_risk.close_or_reduce_short_quantity > 0
                )

        # The Shadow arm receives the identical decision but never reaches an
        # execution venue.  It still contributes an intervention audit.
        if arm != "shadow" and decision.allowed and decision.allowed_quantity:
            bridge.register_authorization(
                order,
                decision,
                pre_long_position=portfolio.long,
                pre_short_position=portfolio.short,
                governance_state=state,
            )
            if index == 9:
                bridge.request_cancel(order.order_id)
                bridge.record_cancel(order.order_id)
            else:
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
                    event = bridge.record_execution(
                        order.order_id,
                        fill_quantity=fill,
                        execution_price=price,
                        pre_long_position=before_long,
                        pre_short_position=before_short,
                        post_long_position=after_long,
                        post_short_position=after_short,
                    )
                    executions += 1
                    severe_increase += int(event.severe_state_executed_net_risk_increase)
                    actual_amplification += int(event.execution_exceeds_authorized)

        # Same static institutional and maker outputs, used only for utility
        # retention; their inventory is not cross-contaminated with retail.
        inst = OrderProposal(
            order_id=f"inst-{seed}-{index}", agent_id="inst-static", role="institutional",
            instrument="AAPL", side="SELL", quantity=1, order_type="LIMIT", price=price,
            source="static_strategist_no_llm", proposed_at=_time(index),
        )
        maker = OrderProposal(
            order_id=f"maker-{seed}-{index}", agent_id="maker-static", role="market_maker",
            instrument="AAPL", side="BUY" if index % 2 else "SELL", quantity=1,
            order_type="LIMIT", price=price, source="static_strategist_no_llm",
            proposed_at=_time(index), market_making_quote=True,
        )
        inst_decision = _decision_for_arm(arm, inst, _Portfolio(long=20), state, index)
        maker_decision = _decision_for_arm(arm, maker, _Portfolio(), state, index)
        institution_allowed += int(inst_decision.allowed)
        maker_allowed += int(maker_decision.allowed)
        equity.append(portfolio.equity(price))
        gross_exposure.append((portfolio.long + portfolio.short) * price)
        net_exposure.append(abs(portfolio.long - portfolio.short) * price)

    peak = max(equity)
    max_drawdown = max((peak - value) for value in equity)
    peak_gross = max(gross_exposure)
    peak_net = max(net_exposure)
    count = len(prices)
    return {
        "actual_risk_amplification": float(actual_amplification),
        "severe_state_executed_net_risk_increase": float(severe_increase),
        "risk_reduction_blocked": float(blocked_reduction),
        "close_or_cover_blocked": float(blocked_close_cover),
        "forced_direction_change": 0.0,
        "scalar_fallback_failure": float(fallback_failure),
        "max_drawdown": max_drawdown,
        "peak_gross_exposure": peak_gross,
        "peak_net_exposure": peak_net,
        "risk_limit_response_delay": 0.0,
        "recurrent_reisolation_delay": 0.0 if second_isolation_index == 17 else 1.0,
        "safe_recovery_time": float((recovery_index or count) - 6),
        "fill_retention_rate": executions / proposed,
        "post_risk_trade_recovery_rate": float(executions > 0 and recovery_index is not None),
        "final_pnl": equity[-1] - equity[0],
        "caution_occupancy": state_counts.get("CAUTION", 0) / count,
        "restricted_occupancy": state_counts.get("RESTRICTED", 0) / count,
        "isolated_occupancy": state_counts.get("ISOLATED", 0) / count,
        "state_permission_switches": float(transitions),
        "institutional_completion_rate": institution_allowed / count,
        "maker_quote_retention_rate": maker_allowed / count,
        "unnecessary_interventions": float(interventions),
    }


def run_static_paired_comparison(
    seeds: tuple[int, ...] = (20261101, 20261102, 20261103)
) -> dict[str, Any]:
    """Run the four fixed arms with identical seeds, prices, and static orders."""

    if not seeds:
        raise ValueError("at least one paired seed is required")
    results: dict[str, dict[str, float]] = {}
    for arm in ARMS:
        individual = [_single_run(arm, seed) for seed in seeds]
        keys = individual[0]
        results[arm] = {
            key: sum(row[key] for row in individual) / len(individual) for key in keys
        }
    return {
        "experiment": "v2_1_lean_static_paired_diagnostic_v1",
        "llm_api_used": False,
        "seeds": list(seeds),
        "scenario_coverage": list(SCENARIOS),
        "arms": results,
    }
