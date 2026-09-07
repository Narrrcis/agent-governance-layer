"""Property-based tests for the invariants that must hold at every input.

The example tests elsewhere pin specific scenarios. These generate arbitrary
positions, sides and quantities and assert the properties that the README
states without qualification, which is where an example suite is weakest: it
proves the cases someone thought of.
"""

from __future__ import annotations

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from governance_layer import (
    OrderProposal,
    apply_permission,
    permission_for,
    record_execution,
)

NOW = "2026-11-01T09:30:00+00:00"
SEVERE = ("RESTRICTED", "ISOLATED")
ALL_STATES = (
    "NORMAL",
    "CAUTION",
    "RESTRICTED",
    "ISOLATED",
    "STAGED_REENTRY_1",
    "STAGED_REENTRY_2",
)

positions = st.integers(min_value=-50, max_value=50)
quantities = st.integers(min_value=1, max_value=100)
sides = st.sampled_from(["BUY", "SELL"])


def proposal(side: str, quantity: int) -> OrderProposal:
    return OrderProposal(
        order_id="p",
        agent_id="agent-1",
        role="retail",
        instrument="AAPL",
        side=side,
        quantity=quantity,
        order_type="MARKET",
        price=10.0,
        source="property-test",
        proposed_at=NOW,
    )


def permission(state: str):
    return permission_for(
        run_id="run-1",
        agent_id="agent-1",
        role="retail",
        state=state,
        governance_time=NOW,
        reason_code="TEST",
        index=0,
    )


@given(position=positions, quantity=quantities, side=sides, state=st.sampled_from(ALL_STATES))
@settings(max_examples=400)
def test_an_allowed_order_never_reverses_a_position_through_zero_under_reduce_only(
    position: int, quantity: int, side: str, state: str
) -> None:
    perm = permission(state)
    assume(perm.reduce_only)
    decision = apply_permission(proposal(side, quantity), position, perm)
    if not decision.allowed:
        return
    delta = decision.allowed_quantity if side == "BUY" else -decision.allowed_quantity
    projected = position + delta
    # Reduce-only may close all the way to zero, but never past it, in either
    # direction. Closing a short lands on zero from below, so the property is
    # "the sign never flips", not "the sign is preserved".
    assert position * projected >= 0, f"{position} -> {projected} crossed zero"
    assert abs(projected) <= abs(position)


@given(position=positions, quantity=quantities, side=sides, state=st.sampled_from(SEVERE))
@settings(max_examples=400)
def test_a_severe_state_never_authorizes_a_net_risk_increase(
    position: int, quantity: int, side: str, state: str
) -> None:
    decision = apply_permission(proposal(side, quantity), position, permission(state))
    audit = decision.actual_net_risk_audit_v2
    assert audit is not None
    assert audit.authorized_risk.actual_net_risk_increase == 0


@given(position=positions, quantity=quantities, side=sides, state=st.sampled_from(ALL_STATES))
@settings(max_examples=400)
def test_the_authorized_quantity_never_exceeds_the_requested_quantity(
    position: int, quantity: int, side: str, state: str
) -> None:
    decision = apply_permission(proposal(side, quantity), position, permission(state))
    assert 0 <= decision.allowed_quantity <= quantity
    assert decision.original_quantity == quantity


@given(data=st.data(), position=st.integers(min_value=1, max_value=50))
@settings(max_examples=300)
def test_a_pure_reduction_is_never_blocked_outright(data, position: int) -> None:
    """A sell no larger than the long position always keeps a path.

    The bound matters: a sell *larger* than the position is a zero-crossing
    order carrying new short exposure, and states that forbid new risk are
    correct to clip or refuse its opening leg.
    """

    quantity = data.draw(st.integers(min_value=1, max_value=position))
    for state in ALL_STATES:
        decision = apply_permission(proposal("SELL", quantity), position, permission(state))
        assert decision.allowed, f"{state} blocked a pure reduction"
        assert decision.allowed_quantity == quantity


@given(
    fills=st.lists(st.integers(min_value=1, max_value=10), min_size=1, max_size=6),
)
@settings(max_examples=200)
def test_cumulative_partial_fills_reconcile_with_the_observed_book(fills: list[int]) -> None:
    """Every partial fill must reconcile against the observed long position."""

    total = sum(fills)
    decision = apply_permission(proposal("BUY", total), 0, permission("NORMAL"))
    assume(decision.allowed_quantity == total)

    filled = 0
    for fill in fills:
        filled += fill
        audited = record_execution(
            decision,
            filled,
            post_long_position=filled,
            post_short_position=0,
        )
        audit = audited.actual_net_risk_audit_v2
        assert audit is not None
        assert audit.executed_quantity == filled
        assert audit.executed_risk.quantity == filled
        assert audit.executed_risk.post_long_position == filled
