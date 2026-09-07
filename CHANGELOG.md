# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/).

The package version is independent of the governance policy and audit semantics
versions, which are recorded separately in every artifact. See
[`docs/INTEGRATION.md`](docs/INTEGRATION.md).

## [0.2.0] - 2026-09-07

Renumbered from `2.1.0`. The old number tracked the V2.1 governance policy, not
the distribution, and implied a maturity the package had not earned. `V2.1`
remains the policy and audit semantics version; the package restarts at `0.2.0`
to signal a pre-1.0 API.

### Fixed

Four ways the layer could authorize something it had already refused.

- **The hybrid fallback no longer bypasses authorization.** `execute_hybrid()`
  caught every `Exception` and answered from the scalar fallback. A permission
  issued to one agent, used with another agent's order, raised correctly in the
  gate and was then approved by the fallback. Authorization now runs before the
  adapter call and outside the `try`, and only `GovernanceAdapterError` is
  degraded. Every other exception propagates.
- **Permission validity windows are enforced.** `valid_from` and `valid_until`
  were recorded but never checked, so an hour-old permission still authorized
  orders. The gate now verifies that the evaluation time falls inside the
  window, requires time-zone aware timestamps, and accepts an explicit
  `evaluation_time` for runtimes whose gate runs later than the proposal.
- **Execution audits reconcile exactly.** `finalize_actual_net_risk_audit()`
  rejected an observed delta larger than the claimed fill but accepted a
  smaller one, so a caller could assert an execution of 10 against an observed
  move of 5. The two must now be equal.
- **Decision IDs commit to the permission they label.** The ID was a UUID5 over
  `run_id | index | agent_id`, so changing the state, role, reason, issue time
  or any limit produced the same ID. It is now a SHA-256 over the full
  normalized permission payload plus the policy schema version.

### Added

- `governance_layer.errors` with `GovernanceError`, `PermissionInvalidError`,
  `PermissionExpiredError` and `GovernanceAdapterError`.
- `authorize_permission()` as the explicit, reusable authorization check.
- Numeric validation on `StateObservation`: unit-interval signals must be
  finite and in range, so a `NaN` can no longer compare false against every
  threshold and read as healthy.
- Monotonicity enforcement: `governance_index`, `governance_time` and
  `completed_outcome_count` may not move backwards, so a replayed observation
  cannot rewind a cooldown or re-present spent recovery evidence.
- Property-based tests (Hypothesis) for reduce-only zero-crossing, severe-state
  risk increase, quantity bounds, protected reductions and cumulative fill
  reconciliation.
- `py.typed`, shipped in the wheel and verified in CI. MyPy runs clean and is
  enforced in CI.
- `docs/INTEGRATION.md` describing the call order, time source, exception
  semantics, threading model and audit integrity conditions.
- CI now runs MyPy, a coverage floor, `python -m build`, `twine check`, and a
  smoke test against the installed wheel.

### Security

- **Audit filenames no longer accept arbitrary agent IDs.** Agent IDs were
  interpolated straight into audit paths, so an ID containing `../` or a path
  separator could place artifacts outside the audit directory. IDs are now
  sanitised, with a digest appended whenever sanitisation changed anything so
  distinct agents cannot collide on one file. A containment check backs this up.

### Changed

- `PermissionInvalidError` replaces the bare `ValueError` for identity
  mismatches. It subclasses `ValueError`, so existing handlers keep working.
- Adapters that rely on the scalar fallback must now raise
  `GovernanceAdapterError` rather than any exception.

## [2.1.0] - 2026-08-22

Initial extraction of the governance core from the AML-Sim capstone study:
V2.1 causal state machine, capability permissions, order gate, Actual Net Risk
audit, execution audit bridge and runtime binding.
