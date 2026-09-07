"""Input validation for observations and audit paths.

These cover inputs that are not hostile in the normal case but are also not
verified by the caller: a NaN that would read as healthy against every
threshold, a replayed observation that would rewind a cooldown, and an agent ID
that is not a safe path component.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from governance_layer import (
    AgentGovernanceStateMachine,
    PolicyParameters,
    StateObservation,
)
from governance_layer.runtime_binding import (
    ExecutionAuditRecorder,
    filesystem_agent_slug,
)

NOW = "2026-11-01T09:30:00+00:00"
LATER = "2026-11-01T09:31:00+00:00"


def observation(**overrides) -> StateObservation:
    base = dict(
        governance_index=1,
        governance_time=NOW,
        data_cutoff_timestamp=NOW,
        latest_result_available_timestamp=NOW,
        telemetry_missing=False,
        telemetry_age_intervals=0,
        lagged_reliability=1.0,
        confidence=0.9,
        confidence_calibration=1.0,
        delayed_outcome_count=0,
        completed_outcome_count=5,
        exposure_ratio=0.1,
        current_risk_limit_scale=1.0,
    )
    base.update(overrides)
    return StateObservation(**base)


def machine() -> AgentGovernanceStateMachine:
    return AgentGovernanceStateMachine("agent-1", PolicyParameters.defaults())


# --- numeric bounds ---------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["lagged_reliability", "confidence", "confidence_calibration"],
)
def test_a_nan_signal_is_rejected_instead_of_reading_as_healthy(field: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        observation(**{field: float("nan")}).validate()


@pytest.mark.parametrize(
    "field",
    ["lagged_reliability", "confidence", "confidence_calibration"],
)
def test_a_unit_interval_signal_outside_its_range_is_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="between zero and one"):
        observation(**{field: 1.5}).validate()


def test_infinite_exposure_ratio_is_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        observation(exposure_ratio=float("inf")).validate()


def test_exposure_ratio_above_one_is_allowed_because_breaching_a_limit_is_real() -> None:
    assert observation(exposure_ratio=1.4).validate().exposure_ratio == 1.4


def test_negative_exposure_ratio_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        observation(exposure_ratio=-0.1).validate()


def test_negative_governance_index_is_rejected() -> None:
    with pytest.raises(ValueError, match="governance index"):
        observation(governance_index=-1).validate()


# --- monotonicity -----------------------------------------------------------


def test_a_replayed_older_index_is_rejected() -> None:
    m = machine()
    m.evaluate(observation(governance_index=5, governance_time=LATER))
    with pytest.raises(ValueError, match="index moved backwards"):
        m.evaluate(observation(governance_index=4, governance_time=LATER))


def test_a_rewound_governance_time_is_rejected() -> None:
    m = machine()
    m.evaluate(observation(governance_index=5, governance_time=LATER))
    with pytest.raises(ValueError, match="time moved backwards"):
        m.evaluate(observation(governance_index=6, governance_time=NOW))


def test_shrinking_completed_evidence_is_rejected() -> None:
    m = machine()
    m.evaluate(observation(governance_index=5, completed_outcome_count=10))
    with pytest.raises(ValueError, match="outcome count moved backwards"):
        m.evaluate(
            observation(
                governance_index=6,
                governance_time=LATER,
                completed_outcome_count=9,
            )
        )


def test_a_monotonic_sequence_is_accepted() -> None:
    m = machine()
    first = observation(governance_index=5, completed_outcome_count=10)
    m.evaluate(first)
    m.evaluate(replace(first, governance_index=6, governance_time=LATER))


# --- audit path safety ------------------------------------------------------


def test_a_traversing_agent_id_cannot_escape_the_audit_directory(tmp_path) -> None:
    recorder = ExecutionAuditRecorder(
        run_id="run-1",
        agent_id="../../escape",
        mechanism="test",
        audit_dir=tmp_path / "audit",
    )
    path = recorder._path("execution_audit.jsonl")
    assert path.resolve().parent == (tmp_path / "audit").resolve()
    assert ".." not in path.name


def test_a_normal_agent_id_keeps_its_readable_filename(tmp_path) -> None:
    recorder = ExecutionAuditRecorder(
        run_id="run-1",
        agent_id="retail-1",
        mechanism="test",
        audit_dir=tmp_path / "audit",
    )
    assert recorder._path("execution_audit.jsonl").name == "execution_audit_retail-1.jsonl"


def test_two_distinct_unsafe_ids_do_not_collide_on_one_audit_file() -> None:
    assert filesystem_agent_slug("a/b") != filesystem_agent_slug("a:b")
