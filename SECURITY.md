# Security policy

## Scope

This is research software for simulated markets. It does not connect to a
broker, hold credentials, or move money. Please read the threat model below
before deciding whether something is a vulnerability here.

## Threat model

The layer's job is to keep an autonomous agent inside a capability envelope the
agent cannot widen. It assumes:

- **The agent is untrusted.** Strategy or LLM output may be wrong, degraded, or
  actively trying to act outside its envelope. It must not be able to modify
  its own permission, replay another agent's permission, or reuse an expired
  one.
- **The venue is independent.** Fills, partial fills, clamped short covers and
  cancel/execute races are observed facts, not things the layer controls. What
  executed is derived from observed position deltas, never from what was
  requested or authorized.
- **The runtime is trusted to call the layer correctly.** A caller that submits
  `order.quantity` instead of `decision.allowed_quantity` bypasses everything,
  and no check inside this package can detect that. See
  [`docs/INTEGRATION.md`](docs/INTEGRATION.md).
- **Agent IDs may not be safe path components.** They reach the filesystem
  through audit artifact names and are sanitised before use.

Out of scope: the simulator, broker connectivity, and anything that depends on
the caller honouring the integration contract.

## What counts as a vulnerability

Anything that lets an order reach a venue with more risk than the governance
state authorized. Concretely:

- A path where a refusal becomes an approval, including via a fallback,
  degraded mode, or swallowed exception.
- A permission usable by an agent it was not issued to, or outside its validity
  window.
- An execution audit that reconciles when the observed and claimed quantities
  disagree.
- A reduce-only path that reverses a position through zero.
- An audit artifact written outside its audit directory.
- A risk-reducing order blocked outright. A layer that blocks the exit is more
  dangerous than no layer, so this is treated as a safety defect, not a
  usability one.

## Reporting

Open a [private security advisory](https://github.com/Narrrcis/agent-governance-layer/security/advisories/new)
on this repository. Please include a minimal reproduction: the permission, the
order, the position, and what was authorized versus what should have been.

Because this is a research project maintained by one person, there is no
guaranteed response time. Reports that demonstrate an authorization bypass will
be prioritised over everything else.

## Supported versions

Only the latest version on `main` receives fixes. The API is pre-1.0 and may
change.
