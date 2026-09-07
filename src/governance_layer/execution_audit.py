"""Execution-lifecycle bridge for Actual Net Risk V2.1.

The simulator must call :meth:`record_execution` immediately after its native
portfolio update, with the pre- and post-fill inventory snapshots.  In the
AML-Sim/StockSim integration this belongs directly after
``super().on_trade_execution(trade_data)`` in the agent execution callback.
Authorization is intentionally not treated as a fill: one authorized order
can yield zero, one, or several observed fills.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .actual_risk import RiskChange, assess_observed_risk_change
from .models import GateDecision, OrderProposal
from .order_gate import record_execution


@dataclass(frozen=True)
class ExecutionAuditEvent:
    """One actual exchange fill, preserved independently of authorization."""

    order_id: str
    fill_sequence: int
    fill_quantity: int
    execution_price: float
    cumulative_executed_quantity: int
    cancel_requested: bool
    cancel_execute_race: bool
    proposed_risk: RiskChange
    authorized_risk: RiskChange
    executed_risk: RiskChange
    cumulative_executed_risk: RiskChange
    execution_exceeds_authorized: bool
    authorization_governance_state: str
    execution_governance_state: str
    realized_opening_excess_quantity: int
    realized_opening_exceeds_authorized: bool
    realized_opening_exceeds_authorized_first_observation: bool
    severe_state_executed_net_risk_increase: bool
    severe_state_realized_opening_exceeds_authorized: bool
    gross_exposure_limit_at_execution: float | None
    net_exposure_limit_at_execution: float | None
    severe_state_gross_exposure_limit_breach: bool
    severe_state_net_exposure_limit_breach: bool
    severe_state_hard_exposure_limit_breach: bool

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for name in (
            "proposed_risk",
            "authorized_risk",
            "executed_risk",
            "cumulative_executed_risk",
        ):
            result[name] = getattr(self, name).to_dict()
        return result


@dataclass
class _TrackedOrder:
    order: OrderProposal
    decision: GateDecision
    pre_long_position: int
    pre_short_position: int
    governance_state: str
    cumulative_executed_quantity: int = 0
    cancel_requested: bool = False
    cancelled: bool = False
    realized_opening_exceed_observed: bool = False
    events: list[ExecutionAuditEvent] = field(default_factory=list)
    latest_decision: GateDecision | None = None


class StockSimExecutionAuditBridge:
    """Associate native exchange callbacks with governance authorizations.

    Register the governance decision when the order is submitted.  For every
    actual fill, call :meth:`record_execution` after the execution engine has
    updated its portfolio.  The bridge validates the supplied post-fill
    inventory, making it impossible to accidentally audit ``allowed_quantity``
    as a fill or to use a stale position for a multi-partial execution.
    """

    def __init__(self) -> None:
        self._orders: dict[str, _TrackedOrder] = {}
        self._native_to_logical: dict[str, str] = {}

    def register_authorization(
        self,
        order: OrderProposal,
        decision: GateDecision,
        *,
        pre_long_position: int,
        pre_short_position: int,
        governance_state: str = "NORMAL",
        native_order_ids: tuple[str, ...] = (),
    ) -> None:
        if order.order_id in self._orders:
            raise ValueError(f"order already registered: {order.order_id}")
        if decision.actual_net_risk_audit_v2 is None:
            raise ValueError("authorization decision has no Actual Net Risk audit")
        audit = decision.actual_net_risk_audit_v2
        if audit.requested_quantity != order.quantity or audit.side != order.side:
            raise ValueError("authorization audit does not match the submitted order")
        self._orders[order.order_id] = _TrackedOrder(
            order=order,
            decision=decision,
            pre_long_position=pre_long_position,
            pre_short_position=pre_short_position,
            governance_state=governance_state,
        )
        self.bind_native_order_ids(order.order_id, native_order_ids)

    def bind_native_order_ids(
        self, logical_order_id: str, native_order_ids: tuple[str, ...]
    ) -> None:
        """Bind split native close/open legs to one authorization audit."""

        self._order(logical_order_id)
        for native_order_id in native_order_ids:
            if native_order_id in self._native_to_logical:
                raise ValueError(f"native order already bound: {native_order_id}")
            self._native_to_logical[native_order_id] = logical_order_id

    def request_cancel(self, order_id: str) -> None:
        self._order(order_id).cancel_requested = True

    def record_cancel(self, order_id: str) -> None:
        logical_order_id = self._native_to_logical.get(order_id, order_id)
        tracked = self._order(logical_order_id)
        tracked.cancel_requested = True
        tracked.cancelled = True

    def record_execution(
        self,
        order_id: str,
        *,
        fill_quantity: int,
        execution_price: float | None = None,
        pre_long_position: int,
        pre_short_position: int,
        post_long_position: int,
        post_short_position: int,
        governance_state_at_execution: str | None = None,
        gross_exposure_limit_at_execution: float | None = None,
        net_exposure_limit_at_execution: float | None = None,
    ) -> ExecutionAuditEvent:
        """Record one observed fill after native portfolio mutation.

        This calls the public order-gate ``record_execution`` function using
        cumulative actual quantity; the event's ``executed_risk`` is the exact
        individual fill effect from the live pre-fill snapshot.
        """

        if fill_quantity <= 0:
            raise ValueError("fill quantity must be positive")
        logical_order_id = self._native_to_logical.get(order_id, order_id)
        tracked = self._order(logical_order_id)
        price = tracked.order.price if execution_price is None else execution_price
        fill_risk = assess_observed_risk_change(
            side=tracked.order.side,
            pre_long_position=pre_long_position,
            pre_short_position=pre_short_position,
            post_long_position=post_long_position,
            post_short_position=post_short_position,
            price=price,
        )
        if fill_risk.quantity != fill_quantity:
            raise ValueError("observed position delta does not equal the actual fill quantity")
        cumulative = tracked.cumulative_executed_quantity + fill_quantity
        cumulative_decision = record_execution(
            tracked.decision,
            cumulative,
        )
        cumulative_audit = cumulative_decision.actual_net_risk_audit_v2
        assert cumulative_audit is not None and cumulative_audit.executed_risk is not None
        prior_changes = [event.executed_risk for event in tracked.events]
        close_long = sum(change.close_or_reduce_long_quantity for change in prior_changes)
        close_short = sum(change.close_or_reduce_short_quantity for change in prior_changes)
        add_long = sum(change.new_or_increase_long_quantity for change in prior_changes)
        add_short = sum(change.new_or_increase_short_quantity for change in prior_changes)
        close_long += fill_risk.close_or_reduce_long_quantity
        close_short += fill_risk.close_or_reduce_short_quantity
        add_long += fill_risk.new_or_increase_long_quantity
        add_short += fill_risk.new_or_increase_short_quantity
        attributed_post_long = tracked.pre_long_position - close_long + add_long
        attributed_post_short = tracked.pre_short_position - close_short + add_short
        cumulative_risk = assess_observed_risk_change(
            side=tracked.order.side,
            pre_long_position=tracked.pre_long_position,
            pre_short_position=tracked.pre_short_position,
            post_long_position=attributed_post_long,
            post_short_position=attributed_post_short,
            price=price,
        )
        cumulative_audit = replace(cumulative_audit, executed_risk=cumulative_risk)
        cumulative_decision = replace(
            cumulative_decision, actual_net_risk_audit_v2=cumulative_audit
        )
        tracked.cumulative_executed_quantity = cumulative
        tracked.latest_decision = cumulative_decision
        execution_state = governance_state_at_execution or tracked.governance_state
        authorized_opening = (
            cumulative_audit.authorized_risk.new_or_increase_long_quantity
            + cumulative_audit.authorized_risk.new_or_increase_short_quantity
        )
        realized_opening = (
            cumulative_risk.new_or_increase_long_quantity
            + cumulative_risk.new_or_increase_short_quantity
        )
        opening_excess = max(0, realized_opening - authorized_opening)
        opening_exceeds = opening_excess > 0
        first_opening_exceed = (
            opening_exceeds and not tracked.realized_opening_exceed_observed
        )
        # A fill that reaches the venue after authorization is classified using
        # the state at execution.  Falling back to the authorization state keeps
        # direct callers compatible, while the live adapter always supplies the
        # current state.
        # A fill that reaches the venue after a cancellation remains visible as
        # a race rather than being silently discarded.
        severe_state = execution_state in {"RESTRICTED", "ISOLATED"}
        severe_gross_breach = bool(
            severe_state
            and gross_exposure_limit_at_execution is not None
            and fill_risk.post_gross_exposure
            > gross_exposure_limit_at_execution + 1e-9
        )
        severe_net_breach = bool(
            severe_state
            and net_exposure_limit_at_execution is not None
            and fill_risk.post_net_directional_exposure
            > net_exposure_limit_at_execution + 1e-9
        )
        event = ExecutionAuditEvent(
            order_id=logical_order_id,
            fill_sequence=len(tracked.events) + 1,
            fill_quantity=fill_quantity,
            execution_price=price,
            cumulative_executed_quantity=cumulative,
            cancel_requested=tracked.cancel_requested,
            cancel_execute_race=tracked.cancel_requested or tracked.cancelled,
            proposed_risk=cumulative_audit.proposed_risk,
            authorized_risk=cumulative_audit.authorized_risk,
            executed_risk=fill_risk,
            cumulative_executed_risk=cumulative_risk,
            execution_exceeds_authorized=cumulative_audit.execution_exceeds_authorized,
            authorization_governance_state=tracked.governance_state,
            execution_governance_state=execution_state,
            realized_opening_excess_quantity=opening_excess,
            realized_opening_exceeds_authorized=opening_exceeds,
            realized_opening_exceeds_authorized_first_observation=first_opening_exceed,
            severe_state_executed_net_risk_increase=(
                severe_state and fill_risk.actual_net_risk_increase == 1
            ),
            severe_state_realized_opening_exceeds_authorized=(
                severe_state and opening_exceeds
            ),
            gross_exposure_limit_at_execution=gross_exposure_limit_at_execution,
            net_exposure_limit_at_execution=net_exposure_limit_at_execution,
            severe_state_gross_exposure_limit_breach=severe_gross_breach,
            severe_state_net_exposure_limit_breach=severe_net_breach,
            severe_state_hard_exposure_limit_breach=(
                severe_gross_breach or severe_net_breach
            ),
        )
        tracked.realized_opening_exceed_observed |= opening_exceeds
        tracked.events.append(event)
        return event

    def record_stocksim_trade_execution(
        self,
        trade_data: dict[str, Any],
        before_snapshot: dict[str, Any],
        after_snapshot: dict[str, Any],
        governance_state_at_execution: str | None = None,
        gross_exposure_limit_at_execution: float | None = None,
        net_exposure_limit_at_execution: float | None = None,
    ) -> ExecutionAuditEvent | None:
        """Adapter for native ``on_trade_execution`` callback payloads.

        Snapshot positions use ``positions[instrument][long|short]``.  Keeping
        this narrow adapter at the integration boundary avoids dependencies on
        an experiment-local AML-Sim wrapper.
        """

        order_id = str(trade_data["order_id"])
        tracked = self._order(order_id)
        instrument = tracked.order.instrument
        before = before_snapshot["positions"][instrument]
        after = after_snapshot["positions"][instrument]
        observed = assess_observed_risk_change(
            side=tracked.order.side,
            pre_long_position=int(before.get("long", 0)),
            pre_short_position=int(before.get("short", 0)),
            post_long_position=int(after.get("long", 0)),
            post_short_position=int(after.get("short", 0)),
            price=float(trade_data.get("price", tracked.order.price)),
        )
        if observed.quantity == 0:
            return None
        return self.record_execution(
            order_id,
            fill_quantity=observed.quantity,
            execution_price=float(trade_data.get("price", tracked.order.price)),
            pre_long_position=int(before.get("long", 0)),
            pre_short_position=int(before.get("short", 0)),
            post_long_position=int(after.get("long", 0)),
            post_short_position=int(after.get("short", 0)),
            governance_state_at_execution=governance_state_at_execution,
            gross_exposure_limit_at_execution=gross_exposure_limit_at_execution,
            net_exposure_limit_at_execution=net_exposure_limit_at_execution,
        )

    def decision_with_execution(self, order_id: str) -> GateDecision:
        """Return authorization amended only with cumulative observed fills."""

        tracked = self._order(order_id)
        if tracked.cumulative_executed_quantity == 0:
            return tracked.decision
        assert tracked.latest_decision is not None
        return tracked.latest_decision

    def events(self, order_id: str) -> tuple[ExecutionAuditEvent, ...]:
        return tuple(self._order(order_id).events)

    def _order(self, order_id: str) -> _TrackedOrder:
        order_id = self._native_to_logical.get(order_id, order_id)
        try:
            return self._orders[order_id]
        except KeyError as exc:
            raise ValueError(f"unknown execution audit order: {order_id}") from exc
