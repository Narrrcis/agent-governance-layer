"""Stable Hybrid Governance V2.1 execution composition.

Capability permission is authoritative.  Scalar sizing is applied only after a
permission decision and never turns a reduce-only action into new risk.  An
adapter failure falls back to the existing scalar path with severe-state
reduce-only protection.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from .actual_risk import build_actual_net_risk_audit
from .errors import GovernanceAdapterError
from .models import CapabilityPermission, GateDecision, OrderProposal
from .order_gate import apply_permission, assess_order, authorize_permission
from .permissions import SCALAR_ACTION_CAP

Gate = Callable[..., GateDecision]


def _decision(
    order: OrderProposal,
    position: int,
    *,
    allowed: bool,
    allowed_quantity: int,
    decision: str,
    reason: str,
    pre_long_position: int,
    pre_short_position: int,
) -> GateDecision:
    assessed_order = replace(order, quantity=max(1, allowed_quantity))
    return GateDecision(
        allowed=allowed,
        original_quantity=order.quantity,
        allowed_quantity=allowed_quantity,
        decision=decision,
        reason=reason,
        assessment=assess_order(assessed_order, position),
        actual_net_risk_audit_v2=build_actual_net_risk_audit(
            order,
            pre_long_position=pre_long_position,
            pre_short_position=pre_short_position,
            allowed_quantity=allowed_quantity,
        ),
    )


def scalar_fallback_decision(
    order: OrderProposal,
    position: int,
    permission: CapabilityPermission,
    *,
    error: Exception | None = None,
    pre_long_position: int | None = None,
    pre_short_position: int | None = None,
) -> GateDecision:
    """Return a scalar fallback decision which is reduce-only in severe states."""

    if pre_long_position is None:
        pre_long_position = max(0, position)
        pre_short_position = max(0, -position)
    assert pre_short_position is not None
    severe = permission.governance_state in {"RESTRICTED", "ISOLATED"}
    if severe:
        safe_quantity = (
            min(order.quantity, pre_long_position)
            if order.side == "SELL"
            else min(order.quantity, pre_short_position)
        )
    else:
        safe_quantity = int(order.quantity * SCALAR_ACTION_CAP[permission.governance_state])
    return _decision(
        order,
        position,
        allowed=safe_quantity > 0,
        allowed_quantity=safe_quantity,
        decision="SCALAR_FALLBACK_AFTER_HYBRID_ERROR",
        reason=type(error).__name__ if error is not None else "SCALAR_FALLBACK",
        pre_long_position=pre_long_position,
        pre_short_position=pre_short_position,
    )


def execute_hybrid(
    order: OrderProposal,
    position: int,
    permission: CapabilityPermission,
    *,
    orders_this_interval: int = 0,
    pre_long_position: int | None = None,
    pre_short_position: int | None = None,
    evaluation_time: str | None = None,
    gate: Gate = apply_permission,
) -> GateDecision:
    """Combine capability authorization with existing scalar sizing and fallback.

    Authorization is verified before the adapter call and is never degraded.
    An adapter that wants the scalar fallback must raise
    :class:`~governance_layer.errors.GovernanceAdapterError`; every other
    exception propagates.
    """

    if (pre_long_position is None) != (pre_short_position is None):
        raise ValueError("provide both pre_long_position and pre_short_position")
    if pre_long_position is None:
        pre_long_position = max(0, position)
        pre_short_position = max(0, -position)
    assert pre_short_position is not None
    if pre_long_position < 0 or pre_short_position < 0:
        raise ValueError("long and short positions must be non-negative")
    if pre_long_position - pre_short_position != position:
        raise ValueError("long/short positions do not match signed position")

    # Authorization happens outside the try block. If the permission is
    # malformed, expired, or issued to another agent, this raises and the order
    # fails closed. Only an adapter failure below may reach the scalar
    # fallback, and only for a request that is already authorized.
    authorize_permission(order, permission, evaluation_time=evaluation_time)

    try:
        decision = gate(
            order,
            position,
            permission,
            orders_this_interval=orders_this_interval,
            pre_long_position=pre_long_position,
            pre_short_position=pre_short_position,
        )
    except GovernanceAdapterError as exc:
        return scalar_fallback_decision(
            order,
            position,
            permission,
            error=exc,
            pre_long_position=pre_long_position,
            pre_short_position=pre_short_position,
        )
    if not decision.allowed or decision.actual_net_risk_audit_v2 is None:
        return decision
    # A fully clipped zero-crossing order is a reduction even when the legacy
    # proposal label says INCREASE.  Only actual new exposure is scalar-sized.
    if decision.actual_net_risk_audit_v2.authorized_risk.actual_net_risk_increase == 0:
        return decision
    quantity = int(decision.allowed_quantity * SCALAR_ACTION_CAP[permission.governance_state])
    if quantity <= 0:
        return _decision(
            order,
            position,
            allowed=False,
            allowed_quantity=0,
            decision="REJECTED",
            reason="HYBRID_ACTION_SCALE_ZERO",
            pre_long_position=pre_long_position,
            pre_short_position=pre_short_position,
        )
    if quantity == decision.allowed_quantity:
        return decision
    return _decision(
        order,
        position,
        allowed=True,
        allowed_quantity=quantity,
        decision="CLIPPED_ACTION_SCALE",
        reason="HYBRID_ACTION_SCALE",
        pre_long_position=pre_long_position,
        pre_short_position=pre_short_position,
    )
