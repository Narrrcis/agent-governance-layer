from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from governance_layer import (
    AgentGovernanceStateMachine,
    GovernanceState,
    PolicyParameters,
    StateObservation,
    eligible_completed_outcomes,
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
        confidence_calibration=1.0,
        delayed_outcome_count=0,
        completed_outcome_count=completed,
        exposure_ratio=0.05,
        current_risk_limit_scale=1.0,
        material_risk_breach=material_risk_breach,
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
