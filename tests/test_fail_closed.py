"""Fail-closed regression tests for the four authorization bypasses.

Each test in this module reproduces a way the layer could be made to authorize
something it had already decided to refuse. They are kept together because they
share a single property: no degraded path, no missing check and no ambiguous
identifier may ever turn a refusal into an approval.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from governance_layer import (
    GovernanceAdapterError,
    OrderProposal,
    PermissionExpiredError,
    PermissionInvalidError,
    apply_permission,
    execute_hybrid,
    finalize_actual_net_risk_audit,
    permission_for,
    record_execution,
)

NOW = "2026-11-01T09:30:00+00:00"


def order(
    side: str,
    quantity: int,
    *,
    agent_id: str = "alice",
    proposed_at: str = NOW,
) -> OrderProposal:
    return OrderProposal(
        order_id="order-1",
        agent_id=agent_id,
        role="retail",
        instrument="AAPL",
        side=side,
        quantity=quantity,
        order_type="MARKET",
        price=100.0,
        source="test",
        proposed_at=proposed_at,
    )


def permission(state: str = "NORMAL", *, agent_id: str = "alice", at: str = NOW):
    return permission_for(
        run_id="run-1",
        agent_id=agent_id,
        role="retail",
        state=state,
        governance_time=at,
        reason_code="TEST",
        index=0,
    )


# --- 1. The hybrid fallback must not become an authorization bypass ----------


def test_hybrid_does_not_fall_back_when_the_permission_belongs_to_another_agent() -> None:
    """A permission issued to alice must never authorize an order from mallory.

    The gate raises for the identity mismatch. Before the fix the hybrid path
    caught that refusal and answered from the scalar fallback instead, which
    allowed the order.
    """

    with pytest.raises(PermissionInvalidError, match="permission agent"):
        execute_hybrid(
            order("SELL", 10, agent_id="mallory"),
            position=10,
            permission=permission("NORMAL", agent_id="alice"),
        )


def test_hybrid_does_not_absorb_an_unexpected_exception() -> None:
    """Only a declared adapter failure may degrade to the scalar path."""

    def broken_gate(*_args, **_kwargs):
        raise RuntimeError("programming error")

    with pytest.raises(RuntimeError, match="programming error"):
        execute_hybrid(
            order("SELL", 5),
            position=10,
            permission=permission("ISOLATED"),
            gate=broken_gate,
        )


def test_hybrid_still_degrades_on_a_declared_adapter_failure() -> None:
    """The fallback remains available for the failure mode it was built for."""

    def failing_adapter(*_args, **_kwargs):
        raise GovernanceAdapterError("venue adapter unavailable")

    decision = execute_hybrid(
        order("SELL", 5),
        position=10,
        permission=permission("ISOLATED"),
        gate=failing_adapter,
    )
    assert decision.decision == "SCALAR_FALLBACK_AFTER_HYBRID_ERROR"
    assert decision.allowed_quantity == 5


def test_hybrid_rejects_inconsistent_position_inputs() -> None:
    with pytest.raises(ValueError, match="do not match signed position"):
        execute_hybrid(
            order("SELL", 5),
            position=10,
            permission=permission("NORMAL"),
            pre_long_position=3,
            pre_short_position=0,
        )


# --- 2. The validity window must actually be enforced -----------------------


def test_an_expired_permission_cannot_authorize_an_order() -> None:
    issued = datetime.fromisoformat(NOW)
    late = (issued + timedelta(minutes=59)).isoformat()
    with pytest.raises(PermissionExpiredError, match="expired"):
        apply_permission(
            order("BUY", 1, proposed_at=late),
            0,
            permission("NORMAL", at=NOW),
        )


def test_a_permission_is_not_valid_before_its_window_opens() -> None:
    issued = datetime.fromisoformat(NOW)
    early = (issued - timedelta(seconds=1)).isoformat()
    with pytest.raises(PermissionExpiredError, match="not yet valid"):
        apply_permission(
            order("BUY", 1, proposed_at=early),
            0,
            permission("NORMAL", at=NOW),
        )


def test_the_caller_may_supply_the_gate_time_explicitly() -> None:
    issued = datetime.fromisoformat(NOW)
    late = (issued + timedelta(minutes=59)).isoformat()
    # The proposal is inside the window, but the gate runs long afterwards.
    with pytest.raises(PermissionExpiredError):
        apply_permission(
            order("BUY", 1),
            0,
            permission("NORMAL", at=NOW),
            evaluation_time=late,
        )


def test_a_naive_timestamp_is_rejected_rather_than_assumed_utc() -> None:
    with pytest.raises(PermissionInvalidError, match="time-zone aware"):
        apply_permission(
            order("BUY", 1, proposed_at="2026-11-01T09:30:00"),
            0,
            permission("NORMAL", at=NOW),
        )


def test_a_permission_inside_its_window_still_works() -> None:
    issued = datetime.fromisoformat(NOW)
    inside = (issued + timedelta(seconds=30)).isoformat()
    decision = apply_permission(
        order("BUY", 1, proposed_at=inside),
        0,
        permission("NORMAL", at=NOW),
    )
    assert decision.allowed


# --- 3. Executed quantity must reconcile with the observed delta ------------


def test_a_claimed_fill_larger_than_the_observed_delta_is_rejected() -> None:
    decision = apply_permission(order("BUY", 10), 0, permission("NORMAL"))
    with pytest.raises(ValueError, match="does not match executed quantity"):
        record_execution(
            decision,
            10,
            post_long_position=5,
            post_short_position=0,
        )


def test_a_matching_fill_is_accepted() -> None:
    decision = apply_permission(order("BUY", 10), 0, permission("NORMAL"))
    audited = record_execution(
        decision,
        5,
        post_long_position=5,
        post_short_position=0,
    )
    assert audited.actual_net_risk_audit_v2 is not None
    assert audited.actual_net_risk_audit_v2.executed_risk.quantity == 5
    assert audited.actual_net_risk_audit_v2.executed_quantity == 5


def test_a_zero_crossing_fill_reconciles_across_both_legs() -> None:
    decision = apply_permission(order("SELL", 15), 10, permission("NORMAL"))
    audit = decision.actual_net_risk_audit_v2
    assert audit is not None
    finalized = finalize_actual_net_risk_audit(
        audit,
        15,
        post_long_position=0,
        post_short_position=5,
    )
    assert finalized.executed_risk.close_or_reduce_long_quantity == 10
    assert finalized.executed_risk.new_or_increase_short_quantity == 5


# --- 4. A decision ID must commit to the permission it labels ---------------


def test_a_different_state_yields_a_different_decision_id() -> None:
    assert (
        permission("NORMAL").governance_decision_id
        != permission("ISOLATED").governance_decision_id
    )


def test_a_different_issue_time_yields_a_different_decision_id() -> None:
    later = (datetime.fromisoformat(NOW) + timedelta(seconds=1)).isoformat()
    assert permission("NORMAL").governance_decision_id != (
        permission("NORMAL", at=later).governance_decision_id
    )


def test_a_different_role_yields_a_different_decision_id() -> None:
    retail = permission_for(
        run_id="run-1",
        agent_id="alice",
        role="retail",
        state="NORMAL",
        governance_time=NOW,
        reason_code="TEST",
        index=0,
    )
    institutional = permission_for(
        run_id="run-1",
        agent_id="alice",
        role="institutional",
        state="NORMAL",
        governance_time=NOW,
        reason_code="TEST",
        index=0,
    )
    assert retail.governance_decision_id != institutional.governance_decision_id


def test_a_different_reason_code_yields_a_different_decision_id() -> None:
    other = permission_for(
        run_id="run-1",
        agent_id="alice",
        role="retail",
        state="NORMAL",
        governance_time=NOW,
        reason_code="OTHER",
        index=0,
    )
    assert permission("NORMAL").governance_decision_id != other.governance_decision_id


def test_the_decision_id_is_deterministic_for_identical_inputs() -> None:
    assert permission("NORMAL").governance_decision_id == (
        permission("NORMAL").governance_decision_id
    )
