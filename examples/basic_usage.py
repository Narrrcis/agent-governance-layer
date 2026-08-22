"""Minimal state-machine and order-gate integration example."""

from datetime import UTC, datetime, timedelta

from governance_layer import (
    AgentGovernanceStateMachine,
    OrderProposal,
    PolicyParameters,
    StateObservation,
    apply_permission,
    permission_for,
)


def timestamp(index: int) -> str:
    start = datetime(2025, 3, 1, 9, 30, tzinfo=UTC)
    return (start + timedelta(minutes=index)).isoformat()


machine = AgentGovernanceStateMachine("retail-1", PolicyParameters.defaults())

for index in range(3):
    observation = StateObservation(
        governance_index=index,
        governance_time=timestamp(index),
        data_cutoff_timestamp=timestamp(index),
        latest_result_available_timestamp="",
        telemetry_missing=True,
        telemetry_age_intervals=index + 1,
        lagged_reliability=0.99,
        confidence=0.8,
        confidence_calibration=0.8,
        delayed_outcome_count=0,
        completed_outcome_count=0,
        exposure_ratio=0.2,
        current_risk_limit_scale=1.0,
    )
    state_decision = machine.evaluate(observation)
    print(index, state_decision.governance_state, state_decision.reason_code)

permission = permission_for(
    run_id="demo",
    agent_id="retail-1",
    role="retail",
    state=machine.state.value,
    governance_time=timestamp(2),
    reason_code=state_decision.reason_code,
    index=2,
)
proposal = OrderProposal(
    order_id="demo-order",
    agent_id="retail-1",
    role="retail",
    instrument="AAPL",
    side="SELL",
    quantity=15,
    order_type="MARKET",
    price=100.0,
    source="demo-strategy",
    proposed_at=timestamp(2),
)
gate = apply_permission(proposal, position=10, permission=permission)
print(gate.to_dict())
