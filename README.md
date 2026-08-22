# Agent Governance Layer

A small, deterministic safety layer for autonomous agents that can propose
financial-market actions. It turns observable reliability and risk signals into
governance states, maps those states to explicit capabilities, and checks every
order before execution.

The package is simulator-oriented research software. It does **not** connect to
a broker, provide investment advice, or replace production risk controls.

## What it provides

- A causal state machine with hysteresis and staged re-entry:
  `NORMAL -> CAUTION -> RESTRICTED -> ISOLATED -> STAGED_REENTRY_1 -> STAGED_REENTRY_2`.
- Role-specific capability envelopes for retail, institutional, and market-making agents.
- An order-intent classifier that distinguishes risk increases, reductions, closures,
  short openings, and zero-crossing reversals.
- A fail-closed order gate that rejects or clips proposals while preserving
  risk-reducing actions.
- Deterministic decision IDs and serializable audit records.
- No runtime dependencies outside the Python standard library.

## Architecture

```text
telemetry + lagged outcomes + exposure
                  |
                  v
        GovernanceStateMachine
                  |
                  v
          CapabilityPermission
                  |
agent proposal -> order gate -> allow / clip / reject -> execution adapter
```

The governance layer never chooses a trading direction. The strategy proposes an
action; governance only constrains whether and how that action may be executed.

## Quick start

```bash
python -m pip install -e ".[dev]"
python examples/basic_usage.py
pytest
```

```python
from datetime import datetime, timezone

from governance_layer import OrderProposal, apply_permission, permission_for

now = datetime.now(timezone.utc).isoformat()
permission = permission_for(
    run_id="demo",
    agent_id="retail-1",
    role="retail",
    state="RESTRICTED",
    governance_time=now,
    reason_code="TELEMETRY_STALE",
    index=3,
)
proposal = OrderProposal(
    order_id="order-1",
    agent_id="retail-1",
    role="retail",
    instrument="AAPL",
    side="SELL",
    quantity=15,
    order_type="MARKET",
    price=100.0,
    source="example-strategy",
    proposed_at=now,
)

# A long position of 10 shares may be closed, but the proposal may not reverse
# into a new short while the account is reduce-only.
decision = apply_permission(proposal, position=10, permission=permission)
print(decision.decision, decision.allowed_quantity)
# CLIPPED_ZERO_CROSSING 10
```

See [`examples/basic_usage.py`](examples/basic_usage.py) for a state transition
and order-gating example.

## Governance invariants

1. Inputs must be time-zone-aware and available at or before the governance time.
2. A risk-reducing order keeps a protected path even when new risk is blocked.
3. Reduce-only mode cannot reverse a position through zero.
4. Recovery requires both a cooldown and new completed evidence.
5. Recurrent degradation during staged re-entry immediately re-isolates the agent.
6. Strategy or LLM output cannot modify its own capability permission.

## Repository scope

This repository contains the reusable governance core extracted from an AML-Sim
capstone study. Simulator orchestration, broker connectivity, generated experiment
outputs, private prompts, and API response logs are intentionally out of scope.
Integrate it by converting your agent's proposed action to `OrderProposal`, calling
`apply_permission`, and executing only the returned allowed quantity.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
```

Python 3.11 or newer is required. The API is at an alpha stage and may change.

## License

No open-source license has been selected yet. Add a license before inviting
external reuse or contributions.

