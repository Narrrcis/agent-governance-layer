"""Unit tests for the simulator-agnostic parts of the runtime audit binding."""

from __future__ import annotations

import json

import pytest

from governance_layer import (
    ExecutionAuditRecorder,
    NullExecutionAuditRecorder,
    apply_permission,
    build_execution_audit_recorder,
    fill_quantity_from_delta,
    message_fill_key,
    permission_for,
    positions_from_snapshot,
    recorder_from_env,
)
from governance_layer.models import OrderProposal


def snapshot(long_position: int, short_position: int, instrument: str = "AAPL") -> dict:
    return {"positions": {instrument: {"long": long_position, "short": short_position}}}


def proposal(order_id: str = "order-1", side: str = "BUY", quantity: int = 10) -> OrderProposal:
    return OrderProposal(
        order_id=order_id,
        agent_id="retail_population",
        role="retail",
        instrument="AAPL",
        side=side,
        quantity=quantity,
        order_type="LIMIT",
        price=100.0,
        source="test",
        proposed_at="2026-11-01T09:30:00+00:00",
    )


def authorize(order: OrderProposal, position: int = 0, state: str = "NORMAL"):
    permission = permission_for(
        run_id="test_run",
        agent_id=order.agent_id,
        role=order.role,
        state=state,
        governance_time=order.proposed_at,
        reason_code="TEST",
        index=0,
    )
    return apply_permission(
        order,
        position,
        permission,
        pre_long_position=max(0, position),
        pre_short_position=max(0, -position),
    )


def recorder(tmp_path, **kwargs) -> ExecutionAuditRecorder:
    return ExecutionAuditRecorder(
        run_id="test_run",
        agent_id="retail_population",
        mechanism="hybrid_staged_v3",
        audit_dir=tmp_path,
        **kwargs,
    )


def test_positions_are_read_from_the_native_snapshot_shape():
    assert positions_from_snapshot(snapshot(4, 3), "AAPL") == (4, 3)
    with pytest.raises(KeyError):
        positions_from_snapshot(snapshot(4, 3), "MSFT")


def test_a_sequence_stamped_message_gets_a_real_fill_identity():
    key, synthetic = message_fill_key({"order_id": "n1", "seq": 12})
    assert key == "n1|seq=12" and synthetic is False


def test_a_message_without_any_identity_falls_back_to_its_content():
    key, synthetic = message_fill_key({"order_id": "n1", "quantity": 3, "price": 100.0})
    assert synthetic is True and key.startswith("n1|content=")


@pytest.mark.parametrize(
    "side,pre,post,expected",
    [
        ("BUY", (0, 0), (7, 0), 7),
        ("BUY", (0, 7), (0, 0), 7),
        ("SELL", (7, 0), (0, 0), 7),
        ("SELL", (0, 2), (0, 9), 7),
        ("SELL", (4, 0), (4, 0), 0),
    ],
)
def test_the_executed_quantity_comes_from_the_observed_delta(side, pre, post, expected):
    assert fill_quantity_from_delta(side, pre[0], pre[1], post[0], post[1]) == expected


def test_a_delta_inconsistent_with_the_order_side_is_rejected():
    with pytest.raises(ValueError):
        fill_quantity_from_delta("SELL", 0, 0, 5, 0)


def test_a_governed_recorder_requires_a_run_and_an_agent(tmp_path):
    with pytest.raises(ValueError):
        ExecutionAuditRecorder(
            run_id="", agent_id="a", mechanism="m", audit_dir=tmp_path
        )
    with pytest.raises(ValueError):
        ExecutionAuditRecorder(
            run_id="r", agent_id="", mechanism="m", audit_dir=tmp_path
        )


def test_the_factory_returns_an_explicit_no_op_when_audit_is_off(tmp_path):
    built = build_execution_audit_recorder(
        enabled=False, run_id="r", agent_id="a", mechanism="m", audit_dir=tmp_path
    )
    assert isinstance(built, NullExecutionAuditRecorder)
    assert built.governed is False
    assert built.record_fill({}, {}, {}) is None
    assert built.close().eligible_for_effect_analysis is False


def test_the_factory_refuses_to_audit_an_anonymous_run(tmp_path):
    built = build_execution_audit_recorder(
        enabled=True, run_id="", agent_id="a", mechanism="m", audit_dir=tmp_path
    )
    assert isinstance(built, NullExecutionAuditRecorder)
    assert built.reason == "EXECUTION_AUDIT_MISCONFIGURED"


def test_the_environment_factory_reads_the_launcher_variables(tmp_path):
    built = recorder_from_env(
        "retail_population",
        env={
            "GOVERNANCE_EXECUTION_AUDIT": "1",
            "GOVERNANCE_RUN_ID": "run_x",
            "NATIVE_GOVERNANCE_MECHANISM": "hybrid_staged_v3",
            "GOVERNANCE_EXECUTION_AUDIT_DIR": str(tmp_path),
        },
    )
    assert isinstance(built, ExecutionAuditRecorder)
    assert built.run_id == "run_x" and built.mechanism == "hybrid_staged_v3"


def test_a_fill_is_audited_against_the_native_positions(tmp_path):
    audit = recorder(tmp_path)
    order = proposal()
    audit.register_order(
        order,
        authorize(order),
        pre_long_position=0,
        pre_short_position=0,
        governance_state="NORMAL",
        native_order_ids=("native-1",),
    )
    event = audit.record_fill(
        {"order_id": "native-1", "quantity": 10, "price": 100.0, "seq": 1},
        snapshot(0, 0),
        snapshot(10, 0),
    )
    assert event is not None
    assert event.fill_quantity == 10
    assert event.order_id == order.order_id
    report = audit.close()
    assert report.executed_quantity_total == 10
    assert report.eligible_for_effect_analysis is True


def test_a_fill_on_an_unauthorised_order_is_kept_but_leaves_the_audit_incomplete(tmp_path):
    audit = recorder(tmp_path)
    assert audit.record_fill(
        {"order_id": "stray", "quantity": 3, "price": 100.0, "seq": 1},
        snapshot(0, 0),
        snapshot(3, 0),
    ) is None
    report = audit.close()
    assert report.unattributed_fill_count == 1
    assert report.audit_valid is True
    assert report.audit_complete is False
    assert report.eligible_for_effect_analysis is False
    raw = (tmp_path / "execution_events_raw_retail_population.jsonl").read_text()
    assert "stray" in raw


def test_strict_mode_treats_an_unauthorised_fill_as_audit_invalid(tmp_path):
    audit = recorder(tmp_path, strict_on_unattributed=True)
    audit.record_fill(
        {"order_id": "stray", "quantity": 3, "price": 100.0, "seq": 1},
        snapshot(0, 0),
        snapshot(3, 0),
    )
    assert audit.audit_valid is False


def test_severe_state_execution_is_flagged_on_the_event(tmp_path):
    audit = recorder(tmp_path)
    order = proposal(order_id="order-severe", side="BUY", quantity=1)
    audit.register_order(
        order,
        authorize(order),
        pre_long_position=0,
        pre_short_position=0,
        governance_state="RESTRICTED",
        native_order_ids=("native-severe",),
    )
    event = audit.record_fill(
        {"order_id": "native-severe", "quantity": 1, "price": 100.0, "seq": 1},
        snapshot(0, 0),
        snapshot(1, 0),
    )
    assert event is not None
    assert event.severe_state_executed_net_risk_increase is True
    assert audit.close().severe_state_executed_net_risk_increase == 1


def test_report_counts_realized_opening_exceed_by_event_order_and_severe_state(
    tmp_path,
):
    audit = recorder(tmp_path)
    order = proposal(order_id="order-opening-skew", side="SELL", quantity=5)
    audit.register_order(
        order,
        authorize(order, position=5),
        pre_long_position=5,
        pre_short_position=0,
        governance_state="NORMAL",
        native_order_ids=("native-opening-skew",),
    )
    event = audit.record_fill(
        {
            "order_id": "native-opening-skew",
            "quantity": 5,
            "price": 100.0,
            "seq": 1,
        },
        snapshot(0, 0),
        snapshot(0, 5),
        governance_state_at_execution="ISOLATED",
        gross_exposure_limit_at_execution=400.0,
        net_exposure_limit_at_execution=400.0,
    )
    assert event is not None
    assert event.realized_opening_exceeds_authorized
    report = audit.close()
    assert report.realized_opening_exceeds_authorized == 1
    assert report.realized_opening_exceeds_authorized_order_count == 1
    assert report.severe_state_realized_opening_exceeds_authorized == 1
    assert report.severe_state_gross_exposure_limit_breach == 1
    assert report.severe_state_net_exposure_limit_breach == 1
    assert report.severe_state_hard_exposure_limit_breach == 1


def test_closing_twice_is_stable_and_hashes_the_written_artifacts(tmp_path):
    audit = recorder(tmp_path)
    order = proposal()
    audit.register_order(
        order,
        authorize(order),
        pre_long_position=0,
        pre_short_position=0,
        governance_state="NORMAL",
        native_order_ids=("native-1",),
    )
    audit.record_fill(
        {"order_id": "native-1", "quantity": 4, "price": 100.0, "seq": 1},
        snapshot(0, 0),
        snapshot(4, 0),
    )
    first = audit.close()
    second = audit.close()
    assert first.to_dict() == second.to_dict()
    stored = json.loads(
        (tmp_path / "audit_integrity_retail_population.json").read_text(encoding="utf-8")
    )
    assert stored["artifact_sha256"] == first.artifact_sha256


def test_registering_the_same_order_twice_marks_the_audit_invalid(tmp_path):
    audit = recorder(tmp_path)
    order = proposal()
    decision = authorize(order)
    audit.register_order(
        order, decision, pre_long_position=0, pre_short_position=0,
        governance_state="NORMAL",
    )
    audit.register_order(
        order, decision, pre_long_position=0, pre_short_position=0,
        governance_state="NORMAL",
    )
    assert audit.audit_valid is False
    assert audit.close().eligible_for_effect_analysis is False
