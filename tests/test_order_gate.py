from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from governance_layer import (
    OrderProposal,
    apply_permission,
    assess_order,
    pending_order_action,
    permission_for,
)

NOW = datetime(2025, 3, 1, 9, 30, tzinfo=UTC).isoformat()


def order(
    side: str,
    quantity: int,
    *,
    order_type: str = "MARKET",
    quote: bool = False,
    role: str = "retail",
    agent_id: str = "agent-1",
) -> OrderProposal:
    return OrderProposal(
        order_id="order-1",
        agent_id=agent_id,
        role=role,
        instrument="AAPL",
        side=side,
        quantity=quantity,
        order_type=order_type,
        price=100.0,
        source="test",
        proposed_at=NOW,
        market_making_quote=quote,
    )


def permission(state: str, role: str = "retail"):
    return permission_for(
        run_id="test",
        agent_id="agent-1",
        role=role,
        state=state,
        governance_time=NOW,
        reason_code="TEST",
        index=0,
    )


def test_classifies_long_and_short_risk_effects() -> None:
    assert assess_order(order("SELL", 4), 10).order_intent == "REDUCE_LONG"
    assert assess_order(order("SELL", 5), -8).order_intent == "INCREASE_SHORT"
    assert assess_order(order("BUY", 5), -8).order_intent == "REDUCE_SHORT"


def test_reduce_only_clips_zero_crossing() -> None:
    decision = apply_permission(order("SELL", 15), 10, permission("ISOLATED"))
    assert decision.allowed
    assert decision.allowed_quantity == 10
    assert decision.decision == "CLIPPED_ZERO_CROSSING"
    assert decision.assessment.projected_post_position == 0


def test_isolation_preserves_reduction_and_blocks_new_risk() -> None:
    isolated = permission("ISOLATED")
    assert apply_permission(order("SELL", 5), 10, isolated).allowed
    assert not apply_permission(order("BUY", 5), 10, isolated).allowed


def test_notional_limit_clips_order() -> None:
    limited = replace(
        permission("NORMAL"),
        max_order_notional=300.0,
        max_position_notional=300.0,
        max_gross_exposure=300.0,
        max_net_exposure=300.0,
    )
    decision = apply_permission(order("BUY", 10), 0, limited)
    assert decision.allowed_quantity == 3
    assert decision.decision == "CLIPPED_LIMIT"


def test_pending_risk_increase_is_cancelled_after_tightening() -> None:
    action, decision = pending_order_action(
        order("BUY", 10, order_type="LIMIT"), 2, permission("ISOLATED")
    )
    assert action == "CANCEL"
    assert not decision.allowed


def test_pending_reduction_is_kept_after_tightening() -> None:
    action, decision = pending_order_action(
        order("SELL", 5, order_type="LIMIT"), 10, permission("ISOLATED")
    )
    assert action == "KEEP"
    assert decision.allowed


def test_permission_cannot_be_used_for_another_agent() -> None:
    with pytest.raises(ValueError, match="permission agent"):
        apply_permission(order("BUY", 1, agent_id="agent-2"), 0, permission("NORMAL"))


def test_market_maker_quotes_remain_bilateral_in_normal_state() -> None:
    normal = permission("NORMAL", "market_maker")
    assert apply_permission(order("BUY", 3, quote=True, role="market_maker"), 5, normal).allowed
    assert apply_permission(order("SELL", 3, quote=True, role="market_maker"), 5, normal).allowed
