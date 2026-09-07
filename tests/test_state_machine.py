from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from governance_layer import (
    V21_FULL,
    V21_LEAN,
    AgentGovernanceStateMachine,
    GovernanceState,
    PolicyParameters,
    StateObservation,
    eligible_completed_outcomes,
    parameters_for_role,
)

START = datetime(2025, 3, 1, 9, 30, tzinfo=UTC)


def moment(index: int) -> str:
    return (START + timedelta(minutes=index)).isoformat()


def observation(
    index: int,
    *,
    missing: bool = False,
    completed: int = 0,
    reliability: float = 1.0,
    material_risk_breach: bool = False,
    calibration: float = 1.0,
    exposure_ratio: float = 0.05,
    material_risk_signal_count: int = 0,
) -> StateObservation:
    return StateObservation(
        governance_index=index,
        governance_time=moment(index),
        data_cutoff_timestamp=moment(index),
        latest_result_available_timestamp=moment(index) if completed else "",
        telemetry_missing=missing,
        telemetry_age_intervals=1 if missing else 0,
        lagged_reliability=reliability,
        confidence=1.0,
        confidence_calibration=calibration,
        delayed_outcome_count=0,
        completed_outcome_count=completed,
        exposure_ratio=exposure_ratio,
        current_risk_limit_scale=1.0,
        material_risk_breach=material_risk_breach,
        material_risk_signal_count=material_risk_signal_count,
    )


def machine() -> AgentGovernanceStateMachine:
    return AgentGovernanceStateMachine("agent-1", PolicyParameters.defaults())


def isolate(subject: AgentGovernanceStateMachine) -> None:
    subject.evaluate(observation(0))
    subject.evaluate(observation(1, missing=True))
    subject.evaluate(observation(2, missing=True))
    subject.evaluate(observation(3, missing=True))


def test_persistent_telemetry_loss_isolates() -> None:
    subject = machine()
    isolate(subject)
    assert subject.state is GovernanceState.ISOLATED


def test_recovery_requires_new_evidence_and_cooldown() -> None:
    subject = machine()
    isolate(subject)
    held = subject.evaluate(observation(4, completed=1))
    recovered = subject.evaluate(observation(5, completed=2))
    assert held.governance_state == "ISOLATED"
    assert recovered.governance_state == "STAGED_REENTRY_1"


def test_degradation_during_reentry_reisolates() -> None:
    subject = machine()
    isolate(subject)
    subject.evaluate(observation(4, completed=1))
    subject.evaluate(observation(5, completed=2))
    decision = subject.evaluate(observation(6, missing=True, completed=2))
    assert decision.governance_state == "ISOLATED"
    assert decision.reason_code == "REISOLATE_TELEMETRY_LOSS_DURING_REENTRY"


def test_material_risk_breach_immediately_isolates() -> None:
    decision = machine().evaluate(observation(0, material_risk_breach=True))
    assert decision.governance_state == "ISOLATED"
    assert decision.reason_code == "EMERGENCY_ISOLATE_MATERIAL_RISK"


def test_future_data_cutoff_is_rejected() -> None:
    current = observation(2)
    with pytest.raises(ValueError, match="future state cutoff"):
        StateObservation(**{**current.__dict__, "data_cutoff_timestamp": moment(3)}).validate()


def test_delayed_outcomes_are_not_visible_early() -> None:
    history = [
        {
            "interval_end": moment(1),
            "result_available_timestamp": moment(1),
            "outcome_status": "completed",
        },
        {
            "interval_end": moment(2),
            "result_available_timestamp": moment(4),
            "outcome_status": "completed",
        },
    ]
    visible = eligible_completed_outcomes(history, moment(3), delay_seconds=0)
    assert visible == history[:1]


def test_role_parameter_interface_preserves_current_thresholds() -> None:
    baseline = PolicyParameters.defaults()
    assert parameters_for_role(baseline, "retail") == baseline


def test_confidence_only_signal_never_exceeds_caution() -> None:
    decision = machine().evaluate(observation(0, calibration=0.10))
    assert decision.governance_state == "CAUTION"
    assert decision.calibration_signal_active
    assert decision.calibration_trigger_codes == "CONFIDENCE_MISCALIBRATED_SEVERE"


def test_calibration_ready_false_cannot_hide_an_active_calibration_signal() -> None:
    raw = observation(0, calibration=0.50)
    decision = machine().evaluate(
        StateObservation(
            **{
                **raw.__dict__,
                "calibration_brier": 0.34,
                "calibration_ece": 0.19,
                "calibration_ready": False,
            }
        )
    )
    assert decision.governance_state == "CAUTION"
    assert decision.calibration_signal_active
    assert not decision.calibration_control_suppressed
    assert not decision.calibration_ready


def test_independent_material_risk_still_immediately_isolates() -> None:
    decision = machine().evaluate(observation(0, calibration=0.10, material_risk_breach=True))
    assert decision.governance_state == "ISOLATED"
    assert decision.calibration_signal_active


def test_hard_blocker_keeps_isolation_when_recovery_conditions_otherwise_hold() -> None:
    subject = machine()
    isolate(subject)
    decision = subject.evaluate(observation(5, completed=2, exposure_ratio=0.90))
    assert decision.governance_state == "ISOLATED"
    assert decision.reason_code == "HOLD_ISOLATED_HARD_BLOCKER_EXPOSURE_SEVERE"


def test_residual_confidence_warning_recovers_directly_to_caution() -> None:
    subject = machine()
    isolate(subject)
    decision = subject.evaluate(
        StateObservation(
            **{
                **observation(5, completed=2, calibration=0.50).__dict__,
                "calibration_brier": 0.31,
                "calibration_ece": 0.18,
                "calibration_ready": True,
            }
        )
    )
    assert decision.governance_state == "CAUTION"
    assert decision.reason_code == "RECOVER_TO_CAUTION_SOFT_WARNING"
    assert decision.calibration_signal_active
    assert decision.calibration_control_suppressed
    assert decision.calibration_suppression_reason == "RECOVERY_SOFT_CONFIDENCE_ONLY"
    assert decision.calibration_brier == 0.31
    assert decision.calibration_ece == 0.18
    assert decision.calibration_ready


def test_stage_two_soft_warning_cannot_transiently_return_normal() -> None:
    subject = machine()
    isolate(subject)
    assert subject.evaluate(observation(5, completed=2)).governance_state == "STAGED_REENTRY_1"
    assert subject.evaluate(observation(7, completed=4)).governance_state == "STAGED_REENTRY_2"
    decision = subject.evaluate(observation(8, completed=4, calibration=0.50))
    assert decision.previous_state == "STAGED_REENTRY_2"
    assert decision.governance_state == "CAUTION"
    assert decision.governance_state != "NORMAL"


def test_second_degradation_reisolates_and_rejects_old_evidence() -> None:
    subject = machine()
    isolate(subject)
    assert subject.evaluate(observation(5, completed=2)).governance_state == "STAGED_REENTRY_1"
    decision = subject.evaluate(observation(6, missing=True, completed=2))
    assert decision.governance_state == "ISOLATED"
    assert decision.recovery_epoch == 2
    assert decision.evidence_floor_completed_count == 2
    assert decision.old_evidence_rejected


def test_lean_records_confidence_only_signal_without_escalating_beyond_caution() -> None:
    subject = AgentGovernanceStateMachine("agent-lean", PolicyParameters.defaults(), V21_LEAN)
    decision = subject.evaluate(observation(0, calibration=0.10))
    assert decision.governance_state == "NORMAL"
    assert decision.soft_signal_active
    assert decision.soft_signal_recorded_only
    assert decision.calibration_signal_active
    full = AgentGovernanceStateMachine("agent-full", PolicyParameters.defaults(), V21_FULL)
    assert full.evaluate(observation(0, calibration=0.10)).governance_state == "CAUTION"


def test_lean_hard_risk_still_restricts_or_isolates() -> None:
    subject = AgentGovernanceStateMachine("agent-lean", PolicyParameters.defaults(), V21_LEAN)
    restricted = subject.evaluate(observation(0, exposure_ratio=0.90))
    isolated = subject.evaluate(observation(1, material_risk_breach=True))
    assert restricted.governance_state == "RESTRICTED"
    assert isolated.governance_state == "ISOLATED"


def test_full_and_lean_share_the_same_state_machine_with_explicit_profiles() -> None:
    full = AgentGovernanceStateMachine("full", PolicyParameters.defaults(), V21_FULL)
    lean = AgentGovernanceStateMachine("lean", PolicyParameters.defaults(), V21_LEAN)
    assert type(full) is type(lean)
    assert full.profile.name == "hybrid_v2_1_full"
    assert lean.profile.name == "hybrid_v2_1_lean"
