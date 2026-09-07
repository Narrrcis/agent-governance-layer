from __future__ import annotations

from governance_layer import (
    OrderProposal,
    StockSimExecutionAuditBridge,
    apply_permission,
    permission_for,
)

NOW = "2025-03-01T09:35:00+00:00"


def order(order_id: str, side: str, quantity: int) -> OrderProposal:
    return OrderProposal(
        order_id=order_id,
        agent_id="retail",
        role="retail",
        instrument="AAPL",
        side=side,
        quantity=quantity,
        order_type="MARKET",
        price=100.0,
        source="test",
        proposed_at=NOW,
    )


def authorize(order_: OrderProposal, long: int, short: int, state: str = "NORMAL"):
    permission = permission_for(
        run_id="execution-audit",
        agent_id="retail",
        role="retail",
        state=state,
        governance_time=NOW,
        reason_code="TEST",
        index=0,
    )
    return apply_permission(
        order_, long - short, permission, pre_long_position=long, pre_short_position=short
    )


def registered(order_: OrderProposal, long: int, short: int):
    bridge = StockSimExecutionAuditBridge()
    bridge.register_authorization(
        order_, authorize(order_, long, short), pre_long_position=long, pre_short_position=short
    )
    return bridge


def test_record_execution_full_fill_uses_actual_positions() -> None:
    proposal = order("full", "BUY", 4)
    bridge = registered(proposal, 0, 0)
    event = bridge.record_execution(
        "full",
        fill_quantity=4,
        pre_long_position=0,
        pre_short_position=0,
        post_long_position=4,
        post_short_position=0,
    )
    decision = bridge.decision_with_execution("full")
    assert event.executed_risk.new_or_increase_long_quantity == 4
    assert decision.actual_net_risk_audit_v2.executed_quantity == 4
    assert decision.actual_net_risk_audit_v2.executed_risk.actual_net_risk_increase == 1


def test_single_and_multiple_partial_fills_are_cumulative_but_individually_exact() -> None:
    proposal = order("partial", "BUY", 8)
    bridge = registered(proposal, 0, 0)
    first = bridge.record_execution(
        "partial",
        fill_quantity=3,
        pre_long_position=0,
        pre_short_position=0,
        post_long_position=3,
        post_short_position=0,
    )
    second = bridge.record_execution(
        "partial",
        fill_quantity=2,
        pre_long_position=3,
        pre_short_position=0,
        post_long_position=5,
        post_short_position=0,
    )
    decision = bridge.decision_with_execution("partial")
    assert (first.cumulative_executed_quantity, second.cumulative_executed_quantity) == (3, 5)
    assert second.executed_risk.pre_long_position == 3
    assert decision.actual_net_risk_audit_v2.authorized_risk.quantity == 8
    assert decision.actual_net_risk_audit_v2.executed_risk.quantity == 5


def test_cancelled_order_without_fill_keeps_execution_unrecorded() -> None:
    proposal = order("cancel", "BUY", 4)
    bridge = registered(proposal, 0, 0)
    bridge.request_cancel("cancel")
    bridge.record_cancel("cancel")
    decision = bridge.decision_with_execution("cancel")
    assert not bridge.events("cancel")
    assert not decision.actual_net_risk_audit_v2.execution_recorded


def test_cancel_execute_race_preserves_the_actual_partial_fill() -> None:
    proposal = order("race", "SELL", 4)
    bridge = registered(proposal, 10, 0)
    bridge.request_cancel("race")
    event = bridge.record_execution(
        "race",
        fill_quantity=2,
        pre_long_position=10,
        pre_short_position=0,
        post_long_position=8,
        post_short_position=0,
    )
    assert event.cancel_execute_race
    assert event.executed_risk.actual_net_risk_increase == 0


def test_zero_crossing_counts_only_actual_reverse_fill_and_not_authorization() -> None:
    proposal = order("cross", "SELL", 8)
    bridge = registered(proposal, 5, 0)
    reduce_event = bridge.record_execution(
        "cross",
        fill_quantity=5,
        pre_long_position=5,
        pre_short_position=0,
        post_long_position=0,
        post_short_position=0,
    )
    reverse_event = bridge.record_execution(
        "cross",
        fill_quantity=3,
        pre_long_position=0,
        pre_short_position=0,
        post_long_position=0,
        post_short_position=3,
    )
    assert reduce_event.executed_risk.actual_net_risk_increase == 0
    assert reverse_event.executed_risk.new_or_increase_short_quantity == 3
    assert reverse_event.executed_risk.actual_net_risk_increase == 1
    decision = bridge.decision_with_execution("cross")
    assert decision.actual_net_risk_audit_v2.executed_risk.quantity == 8


def test_native_stocksim_callback_adapter_uses_trade_quantity_not_allowed_quantity() -> None:
    proposal = order("native", "BUY", 6)
    bridge = StockSimExecutionAuditBridge()
    bridge.register_authorization(
        proposal,
        authorize(proposal, 0, 0),
        pre_long_position=0,
        pre_short_position=0,
        native_order_ids=("native-leg-1", "native-leg-2"),
    )
    event = bridge.record_stocksim_trade_execution(
        {"order_id": "native-leg-1", "quantity": 2, "price": 101.0},
        {"positions": {"AAPL": {"long": 0, "short": 0}}},
        {"positions": {"AAPL": {"long": 2, "short": 0}}},
    )
    second = bridge.record_stocksim_trade_execution(
        {"order_id": "native-leg-2", "quantity": 1, "price": 102.0},
        {"positions": {"AAPL": {"long": 2, "short": 0}}},
        {"positions": {"AAPL": {"long": 3, "short": 0}}},
    )
    decision = bridge.decision_with_execution("native")
    assert event is not None and event.execution_price == 101.0
    assert second is not None
    assert second.cumulative_executed_quantity == 3
    assert event.executed_risk.quantity == 2
    assert decision.actual_net_risk_audit_v2.authorized_risk.quantity == 6
    assert decision.actual_net_risk_audit_v2.executed_risk.quantity == 3


def test_observed_short_leg_can_add_short_without_closing_existing_long() -> None:
    proposal = order("dual-book-short", "SELL", 6)
    bridge = registered(proposal, 8, 0)
    event = bridge.record_execution(
        proposal.order_id,
        fill_quantity=6,
        pre_long_position=8,
        pre_short_position=0,
        post_long_position=8,
        post_short_position=6,
    )
    assert event.executed_risk.close_or_reduce_long_quantity == 0
    assert event.executed_risk.new_or_increase_short_quantity == 6
    assert event.executed_risk.post_net_position == 2
    assert event.executed_risk.gross_exposure_increased
    assert event.realized_opening_excess_quantity == 6
    assert event.realized_opening_exceeds_authorized
    assert event.realized_opening_exceeds_authorized_first_observation


def test_observed_plain_buy_can_add_long_without_covering_existing_short() -> None:
    proposal = order("dual-book-long", "BUY", 4)
    bridge = registered(proposal, 0, 5)
    event = bridge.record_execution(
        proposal.order_id,
        fill_quantity=4,
        pre_long_position=0,
        pre_short_position=5,
        post_long_position=4,
        post_short_position=5,
    )
    assert event.executed_risk.close_or_reduce_short_quantity == 0
    assert event.executed_risk.new_or_increase_long_quantity == 4
    assert event.executed_risk.post_net_position == -1
    assert event.executed_risk.gross_exposure_increased
    assert event.realized_opening_exceeds_authorized


def test_realized_opening_exceed_is_cumulative_but_incident_is_counted_once() -> None:
    proposal = order("opening-skew-partials", "SELL", 8)
    bridge = registered(proposal, 8, 0)
    first = bridge.record_execution(
        proposal.order_id,
        fill_quantity=3,
        pre_long_position=8,
        pre_short_position=0,
        post_long_position=8,
        post_short_position=3,
    )
    second = bridge.record_execution(
        proposal.order_id,
        fill_quantity=2,
        pre_long_position=8,
        pre_short_position=3,
        post_long_position=8,
        post_short_position=5,
    )
    assert first.realized_opening_excess_quantity == 3
    assert second.realized_opening_excess_quantity == 5
    assert first.realized_opening_exceeds_authorized_first_observation
    assert not second.realized_opening_exceeds_authorized_first_observation


def test_execution_time_severe_state_controls_the_opening_skew_flag() -> None:
    proposal = order("opening-skew-severe", "SELL", 5)
    bridge = registered(proposal, 5, 0)
    event = bridge.record_execution(
        proposal.order_id,
        fill_quantity=5,
        pre_long_position=0,
        pre_short_position=0,
        post_long_position=0,
        post_short_position=5,
        governance_state_at_execution="RESTRICTED",
    )
    assert event.authorization_governance_state == "NORMAL"
    assert event.execution_governance_state == "RESTRICTED"
    assert event.realized_opening_exceeds_authorized
    assert event.severe_state_realized_opening_exceeds_authorized
    assert event.severe_state_executed_net_risk_increase
