from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from governance_layer import (
    OrderProposal,
    apply_permission,
    assess_native_execution_plan,
    build_actual_net_risk_audit,
    execute_hybrid,
    pending_order_action,
    permission_for,
    record_execution,
)

ROOT = Path(__file__).resolve().parents[2]
FROZEN_RUN = (
    ROOT
    / "0811-0818"
    / "outputs"
    / "paired_governance_effect_validation_v1_1"
    / "runs"
    / "paired_effect_v1_1_A_hybrid_governance_v2_seed_20261023"
)
FROZEN_EVENTS = (
    FROZEN_RUN
    / "aml"
    / "reports"
    / "agents"
    / "native_order_permission_events_retail_population.json"
)
FROZEN_METRICS = FROZEN_RUN / "episode_metrics.json"

# The frozen paired-validation archive is deliberately outside this repository
# (see "Scope" in the README).  These two regression tests replay it when the
# archive is present next to the checkout and are skipped otherwise, so a clone
# without the archive still reports an honest pass.
requires_frozen_run = pytest.mark.skipif(
    not (FROZEN_EVENTS.is_file() and FROZEN_METRICS.is_file()),
    reason=f"frozen paired-validation run not available at {FROZEN_RUN}",
)

NOW = "2025-03-01T09:35:00+00:00"


def proposal(side: str, quantity: int) -> OrderProposal:
    return OrderProposal(
        order_id="v21-order",
        agent_id="retail",
        role="retail",
        instrument="AAPL",
        side=side,
        quantity=quantity,
        order_type="MARKET",
        price=99.9,
        source="test",
        proposed_at=NOW,
    )


def permission(state: str):
    return permission_for(
        run_id="v21-test",
        agent_id="retail",
        role="retail",
        state=state,
        governance_time=NOW,
        reason_code="TEST",
        index=0,
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@requires_frozen_run
def test_seed_20261023_h02_replay_has_no_actual_net_risk_increase() -> None:
    events_path = FROZEN_EVENTS
    metrics_path = FROZEN_METRICS
    event = next(
        row
        for row in json.loads(events_path.read_text(encoding="utf-8"))
        if row.get("event_type") == "order_permission_decision"
        and row.get("governance_state") == "RESTRICTED"
        and row.get("side") == "SELL"
        and row.get("original_quantity") == 23
        and row.get("allowed_quantity") == 15
    )
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics["severe_state_risk_increase_allowed"] == 1
    decision = apply_permission(
        proposal("SELL", 23),
        position=15,
        permission=permission("RESTRICTED"),
        pre_long_position=15,
        pre_short_position=0,
    )
    audited = record_execution(decision, 15)
    assert event["risk_effect"] == "INCREASE"  # Legacy H02 remains untouched.
    assert audited.actual_net_risk_audit_v2 is not None
    assert audited.actual_net_risk_audit_v2.proposed_risk.actual_net_risk_increase == 1
    assert audited.actual_net_risk_audit_v2.authorized_risk.actual_net_risk_increase == 0
    assert audited.actual_net_risk_audit_v2.executed_risk.actual_net_risk_increase == 0


def test_actual_risk_identifies_true_new_long_short_and_zero_crossing_reverse() -> None:
    new_long = build_actual_net_risk_audit(
        proposal("BUY", 4),
        pre_long_position=0,
        pre_short_position=0,
        allowed_quantity=4,
        executed_quantity=4,
    )
    new_short = build_actual_net_risk_audit(
        proposal("SELL", 4),
        pre_long_position=0,
        pre_short_position=0,
        allowed_quantity=4,
        executed_quantity=4,
    )
    crossing = build_actual_net_risk_audit(
        proposal("SELL", 8),
        pre_long_position=5,
        pre_short_position=0,
        allowed_quantity=8,
        executed_quantity=8,
    )
    assert new_long.executed_risk.actual_net_risk_increase == 1
    assert new_short.executed_risk.actual_net_risk_increase == 1
    assert crossing.executed_risk.close_or_reduce_long_quantity == 5
    assert crossing.executed_risk.new_or_increase_short_quantity == 3
    assert crossing.executed_risk.actual_net_risk_increase_quantity == 3


def test_partial_fill_uses_actual_executed_quantity() -> None:
    audit = build_actual_net_risk_audit(
        proposal("BUY", 10),
        pre_long_position=0,
        pre_short_position=0,
        allowed_quantity=8,
        executed_quantity=3,
    )
    assert audit.authorized_risk.new_or_increase_long_quantity == 8
    assert audit.executed_risk.new_or_increase_long_quantity == 3
    assert audit.executed_risk.post_long_position == 3


def test_native_authorization_legs_override_close_first_dual_book_assumption() -> None:
    native = assess_native_execution_plan(
        side="BUY",
        legs=({"quantity": 13, "is_short_cover": False, "is_short": False},),
        pre_long_position=85,
        pre_short_position=7,
        price=100.0,
    )
    audit = build_actual_net_risk_audit(
        proposal("BUY", 13),
        pre_long_position=85,
        pre_short_position=7,
        allowed_quantity=13,
        authorized_legs=(
            {"quantity": 13, "is_short_cover": False, "is_short": False},
        ),
    )
    assert native.new_or_increase_long_quantity == 13
    assert native.close_or_reduce_short_quantity == 0
    assert audit.proposed_risk.new_or_increase_long_quantity == 6
    assert audit.authorized_risk.new_or_increase_long_quantity == 13
    assert audit.authorized_risk.close_or_reduce_short_quantity == 0


def test_restricted_and_isolated_preserve_reduce_cover_and_cancel() -> None:
    for state in ("RESTRICTED", "ISOLATED"):
        controlled = permission(state)
        assert apply_permission(proposal("SELL", 5), 10, controlled).allowed
        assert apply_permission(proposal("BUY", 5), -10, controlled).allowed
        action, decision = pending_order_action(proposal("SELL", 5), 10, controlled)
        assert action == "KEEP"
        assert decision.allowed
        assert controlled.allow_cancel_order


def test_scalar_fallback_returns_a_safe_reduction_after_injected_hybrid_error() -> None:
    def failing_gate(*_args, **_kwargs):
        raise RuntimeError("injected hybrid failure")

    decision = execute_hybrid(
        proposal("SELL", 5),
        position=10,
        permission=permission("ISOLATED"),
        gate=failing_gate,
    )
    assert decision.allowed
    assert decision.allowed_quantity == 5
    assert decision.decision == "SCALAR_FALLBACK_AFTER_HYBRID_ERROR"
    assert decision.actual_net_risk_audit_v2.authorized_risk.actual_net_risk_increase == 0


@requires_frozen_run
def test_frozen_seed_evidence_is_not_written() -> None:
    events_path = FROZEN_EVENTS
    metrics_path = FROZEN_METRICS
    before = (digest(events_path), digest(metrics_path))
    test_seed_20261023_h02_replay_has_no_actual_net_risk_increase()
    assert (digest(events_path), digest(metrics_path)) == before
