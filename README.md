# Agent Governance Layer

[![CI](https://github.com/Narrrcis/agent-governance-layer/actions/workflows/ci.yml/badge.svg)](https://github.com/Narrrcis/agent-governance-layer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Dependencies](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)](pyproject.toml)

A deterministic capability layer that sits between an autonomous agent and a
market venue. The agent decides *what* it wants to do; this layer decides
*whether and how much* of that may actually reach execution — and proves, after
the fact, that what executed matched what was authorized.

Built as the governance core of an AML-Sim multi-agent market research platform,
and extracted here as a standalone, dependency-free package.

> Research software for simulated markets. It does not connect to a broker,
> give investment advice, or replace production risk controls.

---

## The problem

An LLM-driven trading agent will occasionally be confidently wrong, silently
degraded, or fed stale telemetry. Wrapping it in a prompt that says "be careful"
is not a control. What is needed is a layer that is:

- **outside the agent** — the agent cannot argue its way past it, or edit its own permissions;
- **deterministic** — the same inputs always yield the same decision and the same decision ID;
- **auditable** — every proposed, authorized, and executed quantity is recorded and reconcilable;
- **fail-closed, but never fatal** — it may block new risk, and must never block the exit.

## Architecture

```text
 telemetry  ·  lagged outcomes  ·  exposure
                     │
                     ▼
        ┌───────────────────────────┐
        │  GovernanceStateMachine   │   hysteresis · cooldown · staged re-entry
        └───────────────────────────┘
                     │  state
                     ▼
        ┌───────────────────────────┐
        │   CapabilityPermission    │   role envelope × state rule
        └───────────────────────────┘
                     │
 agent proposal ─────┼─────► order gate ─► ALLOW / CLIP / REJECT ─► venue
                     │                                                │
                     ▼                                                ▼
              Actual Net Risk audit  ◄───────────  observed fills, partials,
              (proposed│authorized│executed)        cancels, cancel/execute races
```

The layer never chooses a trading direction. The strategy proposes; governance
only constrains.

## Five failure modes it is built around

These are the cases that make a naive guardrail wrong, and each one is enforced
by tests rather than by convention.

**1. Authorization is not execution.** The gap between what you allow and what
the venue actually fills is where safety layers quietly break. Fill quantity is
derived from the *observed* long/short position delta — never from the quantity
on the request, and never from `allowed_quantity`. A simulator that clamps a
short-cover to the inventory actually held can execute less than requested, or
nothing at all; recording the request as the fill would corrupt every downstream
risk number.

**2. Zero-crossing orders carry two opposite effects.** `SELL 15` against a long
of 10 is 10 units of risk *reduction* plus 5 units of *new short exposure*. A
reduce-only check that looks only at the side lets the second half through. The
gate decomposes each order into closing and opening legs and clips at zero.

**3. A blocked exit is worse than no governance.** Every state — including full
isolation — keeps a protected path for risk-reducing actions. Restriction
removes the ability to add exposure, never the ability to leave.

**4. Recovery can deadlock.** Intervention causes drawdown; drawdown is then read
as evidence that intervention is still needed. Without an explicit exit, an agent
locks into the most restricted state permanently and governance itself amplifies
tail risk. Recovery therefore requires *both* a cooldown *and* newly completed
outcome evidence, and returns through staged re-entry rather than a single jump.

**5. Punishing honest uncertainty is a real hazard.** If a self-reported
low-confidence signal can trigger a hard lockdown, a well-calibrated agent is
penalized precisely for being honest, and the rational adaptation is to
overstate confidence. Confidence-only calibration signals are therefore capped
at `CAUTION`; only material, independently observable risk can reach
`RESTRICTED` or `ISOLATED`.

## Governance states

`NORMAL → CAUTION → RESTRICTED → ISOLATED → STAGED_REENTRY_1 → STAGED_REENTRY_2`

| State | Notional × | New position | Risk increase | Open short | Reduce-only |
|---|---:|:---:|:---:|:---:|:---:|
| `NORMAL` | 1.00 | ✅ | ✅ | role default | — |
| `CAUTION` | 0.60 | ✅ | ✅ | ❌ | — |
| `RESTRICTED` | 0.40 | ❌ | ❌ | ❌ | ✅ |
| `ISOLATED` | 0.25 | ❌ | ❌ | ❌ | ✅ |
| `STAGED_REENTRY_1` | 0.30 | ✅ | ✅ | ❌ | — |
| `STAGED_REENTRY_2` | 0.55 | ✅ | ✅ | market maker only | — |

Limits are a role envelope (retail / institutional / market maker) scaled by the
state multiplier, so the same state means something different for a retail agent
than for a market maker. Two profiles share one state machine: `V21_FULL` grants
soft signals escalation authority, `V21_LEAN` records them without it.

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
    run_id="demo", agent_id="retail-1", role="retail", state="RESTRICTED",
    governance_time=now, reason_code="TELEMETRY_STALE", index=3,
)
proposal = OrderProposal(
    order_id="order-1", agent_id="retail-1", role="retail", instrument="AAPL",
    side="SELL", quantity=15, order_type="MARKET", price=100.0,
    source="example-strategy", proposed_at=now,
)

# Long 10, reduce-only: the close is preserved, the reversal into a new short is not.
decision = apply_permission(proposal, position=10, permission=permission)
print(decision.decision, decision.allowed_quantity)
# CLIPPED_ZERO_CROSSING 10
```

## Invariants

Each invariant is pinned by a named test, so the specification is readable
straight out of `pytest -v`:

| Invariant | Test |
|---|---|
| No look-ahead: inputs are tz-aware and available at or before the governance time | `test_future_data_cutoff_is_rejected` · `test_delayed_outcomes_are_not_visible_early` |
| A risk-reducing order keeps a protected path even when new risk is blocked | `test_isolation_preserves_reduction_and_blocks_new_risk` |
| Reduce-only cannot reverse a position through zero | `test_reduce_only_clips_zero_crossing` |
| Recovery requires both a cooldown and newly completed evidence | `test_recovery_requires_new_evidence_and_cooldown` |
| Recurrent degradation during staged re-entry re-isolates immediately | `test_degradation_during_reentry_reisolates` |
| A permission cannot be replayed against another agent | `test_permission_cannot_be_used_for_another_agent` |
| Confidence-only signals reach at most `CAUTION` | `test_confidence_only_signal_never_exceeds_caution` |
| Executed quantity comes from the observed delta, not the request | `test_the_executed_quantity_comes_from_the_observed_delta` |

## Live execution binding

Register the authorization at submission via `StockSimExecutionAuditBridge`.
Immediately after the simulator's native portfolio mutation in its
`on_trade_execution` callback, call `record_stocksim_trade_execution(trade_data,
before_snapshot, after_snapshot)`. The bridge records each partial fill,
cancellation, and cancel/execute race from the live pre- and post-fill
positions.

Two further properties are asserted rather than assumed: a fill already at the venue
can never be undone by an audit failure, so every raw execution message is
persisted before any derived record, and an audit failure marks the run
ineligible for effect analysis instead of being swallowed; and an ungoverned run
produces no governance audit at all, through an explicit no-op recorder rather
than a partially initialised one.

## How it was validated

Beyond unit tests, the layer was evaluated in a paired simulator study. The
protocol mattered more than any single result, so it is stated here in full:

- **Paired design.** Governance-on and governance-off episodes share `run_id`,
  seed, regime, and shock schedule. The analyzer verifies that each pair's
  `scenario.yaml` SHA-256 matches before it will compute anything.
- **Isolating the intervention.** The frozen agent base coupled the audit bridge
  to the same flag as the governance constraints, so disabling governance would
  also have destroyed the independent audit. A separate flag was added to
  disable *constraints* while keeping the audit path live — otherwise the
  control arm would not have been measurable.
- **Removing a confound.** The deterministic pressure-order schedule was also
  coupled to the governance flag. The control arm keeps the identical schedule
  and releases every order at full size, so the arms differ only in governance.
- **Native outcomes only.** Return and max drawdown are read from the
  simulator's own portfolio time series; fills from its executed-order records;
  authorized quantities from the permission decisions. Nothing is estimated,
  interpolated, or reconstructed.
- **Pre-committed reporting.** No re-runs, no seed swapping, no deleted
  directories; every completed episode enters the analysis, and both benefits
  and costs are reported.

Two methodological findings shaped the design more than any headline number.
Simulator noise across repeated identical-seed runs was measured first, and it
turned out to exceed the effect size of several candidate configurations —
which invalidated a set of small-effect conclusions and forced the paired
design. And governance is a genuine trade-off, not a free win: reducing
drawdown costs return, and there exist individual runs in which intervention
*amplified* the drawdown it was meant to contain. Quantitative results are held
for the accompanying capstone paper.

## Scope

This repository is the reusable governance core. Simulator orchestration, broker
connectivity, vendored runtimes, generated experiment outputs, prompts, and API
logs are deliberately out of scope. To integrate: convert your agent's proposed
action to an `OrderProposal`, call `apply_permission`, and execute only the
returned `allowed_quantity`.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
```

83 tests, no runtime dependencies outside the standard library, CI on Python
3.11–3.13. API is alpha and may change.

## License

[MIT](LICENSE)
