# Integration contract

What a runtime must guarantee for this layer to mean anything. Everything here
is enforced in code or asserted in tests; nothing is a recommendation.

## Call order

```text
1. permission_for(...)                 issue the authorization
2. apply_permission(order, ...)        authorize, clip or refuse
   or execute_hybrid(order, ...)       same, plus scalar sizing
3. submit only decision.allowed_quantity to the venue
4. bridge.register(...)                bind the authorization to the order id
5. record_stocksim_trade_execution()   after every observed fill
```

Step 3 is the contract. Submitting `order.quantity` rather than
`decision.allowed_quantity` silently discards the entire layer, and nothing
downstream can detect it.

Step 5 must run *after* the venue has already mutated its own positions, and
must be given the pre- and post-fill snapshots. A fill is derived from the
observed long/short delta, never from the requested quantity and never from
`allowed_quantity`.

## Time

| Question | Answer |
| --- | --- |
| What time is a permission checked against? | The `evaluation_time` argument if given, otherwise `order.proposed_at`. |
| When should a caller pass `evaluation_time`? | Whenever the gate runs materially later than the proposal, so the window is checked at the moment the authorization is actually consumed. |
| Are naive timestamps accepted? | No. Every timestamp must be time-zone aware; a naive one raises `PermissionInvalidError` rather than being assumed to be UTC. |
| How long is a permission valid? | 60 seconds from `governance_time`, recorded explicitly as `valid_from` and `valid_until`. |
| May an observation move backwards? | No. `governance_index`, `governance_time` and `completed_outcome_count` must not decrease, otherwise a replay could rewind a cooldown or re-present spent evidence. |

There is no look-ahead: `data_cutoff_timestamp` and
`latest_result_available_timestamp` must be at or before `governance_time`.

## Exceptions

The layer distinguishes two failure kinds, and they must be handled in opposite
ways.

| Exception | Meaning | Correct handling |
| --- | --- | --- |
| `PermissionInvalidError` | Malformed permission, or one issued to another agent. | Fail the order closed. Never degrade. |
| `PermissionExpiredError` | Evaluation time is outside the validity window. | Re-issue a permission, then retry. Never degrade. |
| `GovernanceAdapterError` | A downstream adapter failed on an already-authorized request. | The only exception `execute_hybrid` degrades into the scalar fallback. |
| Anything else | A defect. | Propagates. It must not be absorbed. |

`PermissionInvalidError` subclasses `ValueError`, so callers that already catch
`ValueError` keep working.

An adapter that wants the scalar fallback must raise `GovernanceAdapterError`
explicitly. This is deliberate: the fallback used to be reachable by any
exception, which meant an identity-mismatch refusal could be answered from the
fallback and the order allowed.

## Threading

**One agent, one process, one event loop.** The bridge and the recorder hold
unsynchronised dictionaries, dedup sets and cumulative counters. They are safe
under the runtime they were built for, where the simulator spawns a separate
process per agent and delivers order and execution callbacks on that process's
single asyncio event loop.

They are **not** safe if you call them from multiple threads. If your runtime
dispatches execution callbacks across a thread pool, either give each agent its
own recorder confined to one thread, or wrap every recorder call in your own
lock. Do not share one recorder across threads and assume the counters are
correct: fill dedup and cumulative quantities would both be racy.

## Audit integrity

A run is eligible for effect analysis only if all of the following hold. The
recorder tracks this itself and reports it in `audit_integrity.json`.

- Every raw execution message was persisted before any derived record. A fill
  that reached the venue is never unwound by an audit failure.
- No order was registered twice.
- Every fill was attributable to a registered authorization, or the run was not
  in strict mode.
- The observed position delta equals the claimed executed quantity, exactly.
  A smaller observed delta is as much an accounting break as a larger one.

An ungoverned run produces no governance audit at all, through an explicit
no-op recorder rather than a partially initialised one. Absence of an audit
means "not governed", never "governed and clean".

## Identifiers and versions

Three versions travel independently and must not be conflated:

| Version | Meaning |
| --- | --- |
| Package version (`0.2.0`) | This distribution. |
| `POLICY_SCHEMA_VERSION` | The state-to-capability mapping. |
| `ACTUAL_NET_RISK_VERSION` | The audit's accounting semantics. |
| `AUDIT_BINDING_VERSION` | The runtime binding's artifact format. |

`governance_decision_id` is a SHA-256 over the entire permission payload plus
the policy schema version, run and index. Two permissions differing in state,
role, reason, window or any limit therefore have different IDs, so the ID can
be used to detect a substituted authorization rather than merely to label one.

## Agent identifiers

Agent IDs reach the filesystem through audit artifact names. They are sanitised
before use, and a digest is appended whenever sanitisation changed anything, so
two distinct agents can never collapse onto one audit file. An ID that is
already a safe filename keeps its readable name.
