from __future__ import annotations

from governance_layer import OrderProposal, pending_native_order_action, permission_for

NOW = "2026-11-01T09:30:00+00:00"


def order(side: str, quantity: int) -> OrderProposal:
    return OrderProposal(
        order_id="pending-native",
        agent_id="market_maker",
        role="market_maker",
        instrument="AAPL",
        side=side,
        quantity=quantity,
        order_type="LIMIT",
        price=100.0,
        source="skew-matrix",
        proposed_at=NOW,
        market_making_quote=True,
    )


def permission(state: str):
    return permission_for(
        run_id="pending-guard",
        agent_id="market_maker",
        role="market_maker",
        state=state,
        governance_time=NOW,
        reason_code="SKEW_MATRIX",
        index=0,
    )


def test_native_open_short_masked_by_long_book_is_cancelled_in_severe_state() -> None:
    decision = pending_native_order_action(
        order("SELL", 70),
        pre_long_position=95,
        pre_short_position=0,
        permission=permission("RESTRICTED"),
        is_short=True,
    )
    assert decision.action == "CANCEL"
    assert decision.actual_if_executed.new_or_increase_short_quantity == 70
    assert decision.reason == "SEVERE_STATE_NATIVE_OPENING_NOT_PERMITTED"


def test_native_open_long_masked_by_short_book_is_cancelled_in_severe_state() -> None:
    decision = pending_native_order_action(
        order("BUY", 70),
        pre_long_position=0,
        pre_short_position=95,
        permission=permission("ISOLATED"),
    )
    assert decision.action == "CANCEL"
    assert decision.actual_if_executed.new_or_increase_long_quantity == 70


def test_same_native_opening_is_retained_in_normal_within_hard_limits() -> None:
    decision = pending_native_order_action(
        order("SELL", 70),
        pre_long_position=25,
        pre_short_position=70,
        permission=permission("NORMAL"),
        is_short=True,
    )
    assert decision.action == "KEEP"
    assert not decision.gross_limit_breach_if_executed
    assert not decision.net_limit_breach_if_executed


def test_close_cover_and_clamped_close_are_never_blocked_in_severe_state() -> None:
    close = pending_native_order_action(
        order("SELL", 20),
        pre_long_position=20,
        pre_short_position=0,
        permission=permission("ISOLATED"),
    )
    cover = pending_native_order_action(
        order("BUY", 20),
        pre_long_position=0,
        pre_short_position=5,
        permission=permission("ISOLATED"),
        is_short_cover=True,
    )
    absent = pending_native_order_action(
        order("SELL", 20),
        pre_long_position=0,
        pre_short_position=95,
        permission=permission("ISOLATED"),
    )
    assert close.action == cover.action == absent.action == "KEEP"
    assert cover.actual_if_executed.quantity == 5
    assert absent.actual_if_executed.quantity == 0
