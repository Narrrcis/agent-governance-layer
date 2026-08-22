"""Causal, hysteretic Governance state machine with recurrent-degradation recovery."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return parsed


def eligible_completed_outcomes(
    history: list[dict[str, Any]], governance_time: str, delay_seconds: int
) -> list[dict[str, Any]]:
    """Return only completed outcomes that were available at the decision time."""
    now = parse_timestamp(governance_time)
    delay = timedelta(seconds=int(delay_seconds))
    return [
        row
        for row in history
        if row.get("outcome_status", "completed") == "completed"
        and parse_timestamp(str(row["interval_end"])) <= now
        and parse_timestamp(str(row["result_available_timestamp"])) + delay <= now
    ]


class GovernanceState(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    RESTRICTED = "RESTRICTED"
    ISOLATED = "ISOLATED"
    STAGED_REENTRY_1 = "STAGED_REENTRY_1"
    STAGED_REENTRY_2 = "STAGED_REENTRY_2"


@dataclass(frozen=True)
class PolicyParameters:
    reliability_caution_threshold: float
    reliability_restricted_threshold: float
    calibration_caution_threshold: float
    calibration_restricted_threshold: float
    exposure_caution_threshold: float
    exposure_restricted_threshold: float
    delayed_outcomes_caution_threshold: int
    delayed_outcomes_restricted_threshold: int
    persistent_telemetry_loss_intervals: int
    material_risk_confirmation_signals: int
    cooldown_intervals: int
    minimum_new_completed_outcomes: int
    caution_equal_score_shrink: float
    caution_action_scale_cap: float
    caution_risk_limit_cap: float
    restricted_score_multiplier: float
    restricted_action_scale_cap: float
    restricted_risk_limit_cap: float
    isolated_action_scale: float
    isolated_risk_limit_scale: float
    reentry_stage_1_action_scale_cap: float
    reentry_stage_1_risk_limit_cap: float
    reentry_stage_2_action_scale_cap: float
    reentry_stage_2_risk_limit_cap: float

    @classmethod
    def defaults(cls) -> PolicyParameters:
        """Return the preregistered baseline policy shipped with the package."""
        return cls(
            reliability_caution_threshold=0.985,
            reliability_restricted_threshold=0.965,
            calibration_caution_threshold=0.65,
            calibration_restricted_threshold=0.45,
            exposure_caution_threshold=0.65,
            exposure_restricted_threshold=0.85,
            delayed_outcomes_caution_threshold=1,
            delayed_outcomes_restricted_threshold=2,
            persistent_telemetry_loss_intervals=3,
            material_risk_confirmation_signals=2,
            cooldown_intervals=2,
            minimum_new_completed_outcomes=2,
            caution_equal_score_shrink=0.50,
            caution_action_scale_cap=0.85,
            caution_risk_limit_cap=0.90,
            restricted_score_multiplier=0.50,
            restricted_action_scale_cap=0.60,
            restricted_risk_limit_cap=0.65,
            isolated_action_scale=0.00,
            isolated_risk_limit_scale=0.25,
            reentry_stage_1_action_scale_cap=0.35,
            reentry_stage_1_risk_limit_cap=0.50,
            reentry_stage_2_action_scale_cap=0.65,
            reentry_stage_2_risk_limit_cap=0.75,
        )

    @classmethod
    def from_preregistration(cls, path: Path) -> PolicyParameters:
        with path.open(newline="", encoding="utf-8") as handle:
            values = {row["parameter"]: row["value"] for row in csv.DictReader(handle)}
        float_fields = {
            "reliability_caution_threshold",
            "reliability_restricted_threshold",
            "calibration_caution_threshold",
            "calibration_restricted_threshold",
            "exposure_caution_threshold",
            "exposure_restricted_threshold",
            "caution_equal_score_shrink",
            "caution_action_scale_cap",
            "caution_risk_limit_cap",
            "restricted_score_multiplier",
            "restricted_action_scale_cap",
            "restricted_risk_limit_cap",
            "isolated_action_scale",
            "isolated_risk_limit_scale",
            "reentry_stage_1_action_scale_cap",
            "reentry_stage_1_risk_limit_cap",
            "reentry_stage_2_action_scale_cap",
            "reentry_stage_2_risk_limit_cap",
        }
        int_fields = {
            "delayed_outcomes_caution_threshold",
            "delayed_outcomes_restricted_threshold",
            "persistent_telemetry_loss_intervals",
            "material_risk_confirmation_signals",
            "cooldown_intervals",
            "minimum_new_completed_outcomes",
        }
        missing = (float_fields | int_fields) - set(values)
        if missing:
            raise ValueError(f"Missing preregistered parameters: {sorted(missing)}")
        parsed: dict[str, Any] = {name: float(values[name]) for name in float_fields}
        parsed.update({name: int(values[name]) for name in int_fields})
        return cls(**parsed)


@dataclass(frozen=True)
class StateObservation:
    governance_index: int
    governance_time: str
    data_cutoff_timestamp: str
    latest_result_available_timestamp: str
    telemetry_missing: bool
    telemetry_age_intervals: int
    lagged_reliability: float
    confidence: float
    confidence_calibration: float
    delayed_outcome_count: int
    completed_outcome_count: int
    exposure_ratio: float
    current_risk_limit_scale: float
    material_risk_breach: bool = False
    risk_limit_violation: bool = False
    material_risk_signal_count: int = 0

    def validate(self) -> StateObservation:
        now = parse_timestamp(self.governance_time)
        if parse_timestamp(self.data_cutoff_timestamp) > now:
            raise ValueError("future state cutoff is not allowed")
        if (
            self.latest_result_available_timestamp
            and parse_timestamp(self.latest_result_available_timestamp) > now
        ):
            raise ValueError("future result is not allowed")
        counts = (
            self.telemetry_age_intervals,
            self.delayed_outcome_count,
            self.completed_outcome_count,
            self.material_risk_signal_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("age, outcome, and signal counts must be non-negative")
        return self


@dataclass(frozen=True)
class StateDecision:
    previous_state: str
    governance_state: str
    changed: bool
    transition_direction: str
    reason_code: str
    trigger_codes: str
    missing_streak: int
    fresh_streak: int
    new_completed_outcomes_since_transition: int
    cooldown_remaining: int
    state_transition_count: int
    direct_state_reversal_count: int
    recovery_epoch: int
    evidence_floor_completed_count: int
    old_evidence_rejected: bool
    unnecessary_intervention_candidate: bool

    @property
    def reentry_stage(self) -> int:
        if self.governance_state == GovernanceState.STAGED_REENTRY_1.value:
            return 1
        if self.governance_state == GovernanceState.STAGED_REENTRY_2.value:
            return 2
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "reentry_stage": self.reentry_stage}


class AgentGovernanceStateMachine:
    """Deterministic per-Agent policy using contemporaneously visible evidence only."""

    _SEVERITY = {
        GovernanceState.NORMAL: 0,
        GovernanceState.CAUTION: 1,
        GovernanceState.RESTRICTED: 2,
        GovernanceState.STAGED_REENTRY_2: 1,
        GovernanceState.STAGED_REENTRY_1: 2,
        GovernanceState.ISOLATED: 3,
    }

    def __init__(self, agent_id: str, parameters: PolicyParameters) -> None:
        self.agent_id = agent_id
        self.parameters = parameters
        self.state = GovernanceState.NORMAL
        self.last_transition_index = 0
        self.evidence_at_transition = 0
        self.recovery_evidence_floor = 0
        self.missing_streak = 0
        self.fresh_streak = 0
        self.transition_count = 0
        self.reversal_count = 0
        self.last_transition_direction = ""
        self.ever_isolated = False
        self.recovery_epoch = 0

    def _trigger_codes(self, observation: StateObservation) -> list[str]:
        p = self.parameters
        triggers: list[str] = []
        if observation.telemetry_missing:
            triggers.append(f"TELEMETRY_MISSING_{self.missing_streak}")
        if observation.telemetry_age_intervals >= 2:
            triggers.append("TELEMETRY_STALE_SEVERE")
        elif observation.telemetry_age_intervals >= 1:
            triggers.append("TELEMETRY_STALE")
        if observation.lagged_reliability <= p.reliability_restricted_threshold:
            triggers.append("LAGGED_RELIABILITY_SEVERE")
        elif observation.lagged_reliability <= p.reliability_caution_threshold:
            triggers.append("LAGGED_RELIABILITY_LOW")
        if observation.confidence_calibration <= p.calibration_restricted_threshold:
            triggers.append("CONFIDENCE_MISCALIBRATED_SEVERE")
        elif observation.confidence_calibration <= p.calibration_caution_threshold:
            triggers.append("CONFIDENCE_MISCALIBRATED")
        if observation.delayed_outcome_count >= p.delayed_outcomes_restricted_threshold:
            triggers.append("OUTCOMES_DELAYED_SEVERE")
        elif observation.delayed_outcome_count >= p.delayed_outcomes_caution_threshold:
            triggers.append("OUTCOMES_DELAYED")
        if observation.exposure_ratio >= p.exposure_restricted_threshold:
            triggers.append("EXPOSURE_SEVERE")
        elif observation.exposure_ratio >= p.exposure_caution_threshold:
            triggers.append("EXPOSURE_ELEVATED")
        if observation.material_risk_breach:
            triggers.append("MATERIAL_RISK_BREACH")
        if observation.risk_limit_violation or observation.current_risk_limit_scale < 0.25:
            triggers.append("RISK_LIMIT_VIOLATION")
        if observation.material_risk_signal_count:
            triggers.append(f"MATERIAL_RISK_SIGNAL_COUNT_{observation.material_risk_signal_count}")
        return triggers

    def _material_override(self, observation: StateObservation) -> bool:
        return bool(
            observation.material_risk_breach
            or observation.risk_limit_violation
            or observation.current_risk_limit_scale < 0.25
            or observation.material_risk_signal_count
            >= self.parameters.material_risk_confirmation_signals
        )

    def _required_severity(self, triggers: list[str]) -> int:
        if self.missing_streak >= self.parameters.persistent_telemetry_loss_intervals:
            return 3
        if any(code.endswith("SEVERE") for code in triggers):
            return 2
        if triggers:
            return 1
        return 0

    def _cooldown_remaining(self, index: int) -> int:
        return max(0, self.parameters.cooldown_intervals - (index - self.last_transition_index))

    def _transition(
        self,
        target: GovernanceState,
        observation: StateObservation,
        *,
        reset_recovery: bool = False,
    ) -> tuple[bool, str]:
        previous_severity = self._SEVERITY[self.state]
        target_severity = self._SEVERITY[target]
        direction = (
            "ESCALATE"
            if target_severity > previous_severity
            else "DEESCALATE"
            if target_severity < previous_severity
            else "LATERAL"
        )
        if self.last_transition_direction and direction in {"ESCALATE", "DEESCALATE"}:
            if direction != self.last_transition_direction:
                self.reversal_count += 1
        if direction in {"ESCALATE", "DEESCALATE"}:
            self.last_transition_direction = direction
        self.state = target
        self.last_transition_index = observation.governance_index
        self.evidence_at_transition = observation.completed_outcome_count
        self.transition_count += 1
        if target == GovernanceState.ISOLATED:
            if reset_recovery:
                self.recovery_epoch += 1
                self.recovery_evidence_floor = observation.completed_outcome_count
            self.ever_isolated = True
        return True, direction

    def evaluate(self, observation: StateObservation) -> StateDecision:
        observation.validate()
        previous_state = self.state
        if observation.telemetry_missing:
            self.missing_streak += 1
            self.fresh_streak = 0
        else:
            self.missing_streak = 0
            self.fresh_streak += 1
        triggers = self._trigger_codes(observation)
        required_severity = self._required_severity(triggers)
        material_override = self._material_override(observation)
        changed = False
        direction = "HOLD"
        reason_code = "HOLD_NORMAL_NO_TRIGGER"

        staged = self.state in {
            GovernanceState.STAGED_REENTRY_1,
            GovernanceState.STAGED_REENTRY_2,
        }
        emergency = material_override or required_severity == 3
        if self.state != GovernanceState.ISOLATED and emergency:
            re_isolation = staged or self.ever_isolated
            if material_override:
                suffix = "MATERIAL_RISK"
            else:
                suffix = "PERSISTENT_TELEMETRY_LOSS"
            reason_code = f"{'REISOLATE' if re_isolation else 'EMERGENCY_ISOLATE'}_{suffix}"
            changed, direction = self._transition(
                GovernanceState.ISOLATED, observation, reset_recovery=True
            )
        elif staged and observation.telemetry_missing:
            reason_code = "REISOLATE_TELEMETRY_LOSS_DURING_REENTRY"
            changed, direction = self._transition(
                GovernanceState.ISOLATED, observation, reset_recovery=True
            )
        elif required_severity > self._SEVERITY[self.state]:
            target = (
                GovernanceState.RESTRICTED if required_severity == 2 else GovernanceState.CAUTION
            )
            reason_code = f"ESCALATE_{target.value}_{triggers[0]}"
            changed, direction = self._transition(target, observation)
        elif required_severity > 0:
            reason_code = (
                "HOLD_ISOLATED_ACTIVE_RISK"
                if self.state == GovernanceState.ISOLATED
                else f"HOLD_{self.state.value}_HYSTERESIS_{triggers[0]}"
            )
        else:
            new_evidence = observation.completed_outcome_count - self.evidence_at_transition
            evidence_ready = new_evidence >= self.parameters.minimum_new_completed_outcomes
            cooldown_ready = self._cooldown_remaining(observation.governance_index) == 0
            if self.state == GovernanceState.ISOLATED:
                if evidence_ready and cooldown_ready:
                    reason_code = "ENTER_STAGED_REENTRY_1_NEW_EVIDENCE"
                    changed, direction = self._transition(
                        GovernanceState.STAGED_REENTRY_1, observation
                    )
                elif not cooldown_ready:
                    reason_code = "HOLD_ISOLATED_COOLDOWN"
                else:
                    reason_code = "HOLD_ISOLATED_NEW_EVIDENCE_REQUIRED"
            elif self.state == GovernanceState.STAGED_REENTRY_1:
                if evidence_ready and cooldown_ready:
                    reason_code = "ADVANCE_STAGED_REENTRY_2_NEW_EVIDENCE"
                    changed, direction = self._transition(
                        GovernanceState.STAGED_REENTRY_2, observation
                    )
                else:
                    reason_code = (
                        "HOLD_STAGED_REENTRY_1_COOLDOWN"
                        if not cooldown_ready
                        else "HOLD_STAGED_REENTRY_1_NEW_EVIDENCE_REQUIRED"
                    )
            elif self.state == GovernanceState.STAGED_REENTRY_2:
                if evidence_ready and cooldown_ready:
                    reason_code = "RETURN_NORMAL_AFTER_STAGED_REENTRY"
                    changed, direction = self._transition(GovernanceState.NORMAL, observation)
                else:
                    reason_code = (
                        "HOLD_STAGED_REENTRY_2_COOLDOWN"
                        if not cooldown_ready
                        else "HOLD_STAGED_REENTRY_2_NEW_EVIDENCE_REQUIRED"
                    )
            elif self.state == GovernanceState.RESTRICTED:
                if evidence_ready and cooldown_ready:
                    reason_code = "DEESCALATE_RESTRICTED_TO_CAUTION"
                    changed, direction = self._transition(GovernanceState.CAUTION, observation)
                else:
                    reason_code = "HOLD_RESTRICTED_HYSTERESIS"
            elif self.state == GovernanceState.CAUTION:
                if evidence_ready and cooldown_ready:
                    reason_code = "RETURN_NORMAL_FROM_CAUTION"
                    changed, direction = self._transition(GovernanceState.NORMAL, observation)
                else:
                    reason_code = "HOLD_CAUTION_HYSTERESIS"

        new_evidence = observation.completed_outcome_count - self.evidence_at_transition
        old_evidence_rejected = (
            self.ever_isolated
            and observation.completed_outcome_count - self.recovery_evidence_floor
            < self.parameters.minimum_new_completed_outcomes
        )
        return StateDecision(
            previous_state=previous_state.value,
            governance_state=self.state.value,
            changed=changed,
            transition_direction=direction,
            reason_code=reason_code,
            trigger_codes="|".join(triggers),
            missing_streak=self.missing_streak,
            fresh_streak=self.fresh_streak,
            new_completed_outcomes_since_transition=max(0, new_evidence),
            cooldown_remaining=self._cooldown_remaining(observation.governance_index),
            state_transition_count=self.transition_count,
            direct_state_reversal_count=self.reversal_count,
            recovery_epoch=self.recovery_epoch,
            evidence_floor_completed_count=self.recovery_evidence_floor,
            old_evidence_rejected=old_evidence_rejected,
            unnecessary_intervention_candidate=(
                changed
                and direction == "ESCALATE"
                and not material_override
                and not observation.telemetry_missing
                and previous_state == GovernanceState.NORMAL
            ),
        )

    def control_caps(self) -> tuple[float, float]:
        p = self.parameters
        if self.state == GovernanceState.NORMAL:
            return 1.25, 1.0
        if self.state == GovernanceState.CAUTION:
            return p.caution_action_scale_cap, p.caution_risk_limit_cap
        if self.state == GovernanceState.RESTRICTED:
            return p.restricted_action_scale_cap, p.restricted_risk_limit_cap
        if self.state == GovernanceState.ISOLATED:
            return p.isolated_action_scale, p.isolated_risk_limit_scale
        if self.state == GovernanceState.STAGED_REENTRY_1:
            return p.reentry_stage_1_action_scale_cap, p.reentry_stage_1_risk_limit_cap
        return p.reentry_stage_2_action_scale_cap, p.reentry_stage_2_risk_limit_cap


def confidence_calibration(confidence: float, lagged_reliability: float) -> float:
    quality = max(0.0, min(1.0, lagged_reliability))
    return max(0.0, min(1.0, 1.0 - abs(float(confidence) - quality)))
