"""Order-intent classifier and capability permission gate."""

from __future__ import annotations

from dataclasses import replace

from .actual_risk import build_actual_net_risk_audit, finalize_actual_net_risk_audit
from .errors import PermissionExpiredError, PermissionInvalidError
from .models import (
    CapabilityPermission,
    GateDecision,
    IntentAssessment,
    OrderIntent,
    OrderProposal,
    RiskEffect,
    parse_governance_time,
)


def authorize_permission(
    order: OrderProposal,
    permission: CapabilityPermission,
    *,
    evaluation_time: str | None = None,
) -> None:
    """Check that this permission may authorize this order, at this time.

    Raises on failure and returns ``None`` on success, so that no caller can
    mistake a falsy return value for authorization. This runs before any gating
    logic and is never absorbed by a fallback path.

    The evaluation time defaults to the order's ``proposed_at``. A runtime whose
    gate runs materially later than the proposal should pass its own clock
    reading explicitly, so that the window is checked against the moment the
    authorization is actually consumed.
    """

    order.validate()
    permission.validate()
    if order.agent_id != permission.agent_id:
        raise PermissionInvalidError("permission agent does not match order agent")
    evaluation = parse_governance_time(
        order.proposed_at if evaluation_time is None else evaluation_time,
        "evaluation time",
    )
    valid_from, valid_until = permission.window()
    if evaluation < valid_from:
        raise PermissionExpiredError(
            f"permission is not yet valid: {evaluation.isoformat()} < {valid_from.isoformat()}"
        )
    if evaluation > valid_until:
        raise PermissionExpiredError(
            f"permission has expired: {evaluation.isoformat()} > {valid_until.isoformat()}"
        )


def assess_order(order: OrderProposal, position: int) -> IntentAssessment:
    order.validate()
    delta = order.quantity if order.side == "BUY" else -order.quantity
    projected = position + delta
    crosses = position != 0 and projected != 0 and (position > 0) != (projected > 0)
    closing = min(abs(position), order.quantity) if position * delta < 0 else 0
    opening = max(0, order.quantity - closing) if position * delta <= 0 else order.quantity

    if order.market_making_quote:
        intent = OrderIntent.MARKET_MAKING_QUOTE
    elif position == 0:
        intent = OrderIntent.OPEN_LONG if order.side == "BUY" else OrderIntent.OPEN_SHORT
    elif position > 0:
        if order.side == "BUY":
            intent = OrderIntent.INCREASE_LONG
        elif order.quantity < position:
            intent = OrderIntent.REDUCE_LONG
        elif order.quantity == position:
            intent = OrderIntent.CLOSE_LONG
        else:
            intent = OrderIntent.OPEN_SHORT
    else:
        if order.side == "SELL":
            intent = OrderIntent.INCREASE_SHORT
        elif order.quantity < abs(position):
            intent = OrderIntent.REDUCE_SHORT
        elif order.quantity == abs(position):
            intent = OrderIntent.CLOSE_SHORT
        else:
            intent = OrderIntent.OPEN_LONG

    pre_gross = abs(position) * order.price
    post_gross = abs(projected) * order.price
    if crosses:
        effect = RiskEffect.INCREASE
    elif post_gross > pre_gross + 1e-9:
        effect = RiskEffect.INCREASE
    elif post_gross < pre_gross - 1e-9:
        effect = RiskEffect.REDUCE
    else:
        effect = RiskEffect.NEUTRAL
    return IntentAssessment(
        order_intent=intent.value,
        risk_effect=effect.value,
        pre_position=position,
        projected_post_position=projected,
        pre_gross_exposure=pre_gross,
        projected_gross_exposure=post_gross,
        pre_net_exposure=position * order.price,
        projected_net_exposure=projected * order.price,
        crosses_zero=crosses,
        closing_quantity=closing,
        opening_quantity=opening,
    )


def _apply_permission_core(
    order: OrderProposal,
    position: int,
    permission: CapabilityPermission,
    *,
    orders_this_interval: int = 0,
    evaluation_time: str | None = None,
) -> GateDecision:
    authorize_permission(order, permission, evaluation_time=evaluation_time)
    assessment = assess_order(order, position)
    reduction = assessment.risk_effect == RiskEffect.REDUCE.value
    quantity = order.quantity

    # Reduction and closure remain the safety-priority path. A zero-crossing
    # proposal is clipped to the closing component under reduce-only.
    if permission.reduce_only and assessment.crosses_zero:
        quantity = assessment.closing_quantity
        clipped = replace(order, quantity=quantity)
        return GateDecision(
            allowed=quantity > 0,
            original_quantity=order.quantity,
            allowed_quantity=quantity,
            decision="CLIPPED_ZERO_CROSSING",
            reason="REDUCE_ONLY_NO_REVERSE_POSITION",
            assessment=assess_order(clipped, position),
        )
    if reduction:
        protected = (
            permission.reduce_only
            or (order.order_type == "MARKET" and not permission.allow_market_order)
            or (order.order_type == "LIMIT" and not permission.allow_limit_order)
        )
        return GateDecision(
            True,
            order.quantity,
            order.quantity,
            "ALLOW_REDUCE_ONLY" if protected else "ALLOWED",
            "RISK_REDUCTION_PRESERVED" if protected else "WITHIN_PERMISSION",
            assessment,
        )

    if permission.reduce_only:
        return GateDecision(
            False, order.quantity, 0, "REJECTED", "REDUCE_ONLY_RISK_INCREASE", assessment
        )
    if orders_this_interval >= permission.max_orders_per_interval:
        return GateDecision(
            False, order.quantity, 0, "REJECTED", "ORDER_FREQUENCY_LIMIT", assessment
        )
    if order.order_type == "MARKET" and not permission.allow_market_order:
        return GateDecision(
            False, order.quantity, 0, "REJECTED", "MARKET_ORDER_NOT_PERMITTED", assessment
        )
    if order.order_type == "LIMIT" and not permission.allow_limit_order:
        return GateDecision(
            False, order.quantity, 0, "REJECTED", "LIMIT_ORDER_NOT_PERMITTED", assessment
        )
    if assessment.risk_effect == RiskEffect.INCREASE.value and not permission.allow_risk_increase:
        return GateDecision(
            False, order.quantity, 0, "REJECTED", "RISK_INCREASE_NOT_PERMITTED", assessment
        )
    if assessment.order_intent == OrderIntent.OPEN_SHORT.value and not permission.allow_open_short:
        if assessment.closing_quantity:
            clipped = replace(order, quantity=assessment.closing_quantity)
            return GateDecision(
                True,
                order.quantity,
                assessment.closing_quantity,
                "CLIPPED_ZERO_CROSSING",
                "OPEN_SHORT_NOT_PERMITTED",
                assess_order(clipped, position),
            )
        return GateDecision(
            False, order.quantity, 0, "REJECTED", "OPEN_SHORT_NOT_PERMITTED", assessment
        )
    if position == 0 and not permission.allow_new_position:
        return GateDecision(
            False, order.quantity, 0, "REJECTED", "NEW_POSITION_NOT_PERMITTED", assessment
        )

    max_qty = int(permission.max_order_notional // order.price)
    projected_limit = min(
        permission.max_position_notional,
        permission.max_gross_exposure,
        permission.max_net_exposure,
    )
    if assessment.risk_effect == RiskEffect.INCREASE.value:
        if order.side == "BUY":
            exposure_qty = max(0, int(projected_limit // order.price) - position)
        else:
            exposure_qty = max(0, int(projected_limit // order.price) + position)
        max_qty = min(max_qty, exposure_qty)
    if max_qty <= 0:
        return GateDecision(
            False, order.quantity, 0, "REJECTED", "EXPOSURE_OR_NOTIONAL_LIMIT", assessment
        )
    if quantity > max_qty:
        clipped = replace(order, quantity=max_qty)
        return GateDecision(
            True,
            order.quantity,
            max_qty,
            "CLIPPED_LIMIT",
            "EXPOSURE_OR_NOTIONAL_LIMIT",
            assess_order(clipped, position),
        )
    return GateDecision(
        True, order.quantity, order.quantity, "ALLOWED", "WITHIN_PERMISSION", assessment
    )


def apply_permission(
    order: OrderProposal,
    position: int,
    permission: CapabilityPermission,
    *,
    orders_this_interval: int = 0,
    pre_long_position: int | None = None,
    pre_short_position: int | None = None,
    evaluation_time: str | None = None,
) -> GateDecision:
    """Apply capability permission and attach the V2.1 authorization audit.

    Existing callers may continue supplying a signed net ``position``.  Runtimes
    that maintain separate long and short books can supply both quantities so
    the audit records the true pre-trade inventory.
    """

    if (pre_long_position is None) != (pre_short_position is None):
        raise ValueError("provide both pre_long_position and pre_short_position")
    if pre_long_position is None:
        pre_long_position = max(0, position)
        pre_short_position = max(0, -position)
    assert pre_short_position is not None
    if pre_long_position - pre_short_position != position:
        raise ValueError("long/short positions do not match signed position")
    decision = _apply_permission_core(
        order,
        position,
        permission,
        orders_this_interval=orders_this_interval,
        evaluation_time=evaluation_time,
    )
    return replace(
        decision,
        actual_net_risk_audit_v2=build_actual_net_risk_audit(
            order,
            pre_long_position=pre_long_position,
            pre_short_position=pre_short_position,
            allowed_quantity=decision.allowed_quantity,
        ),
    )


def record_execution(
    decision: GateDecision,
    executed_quantity: int,
    *,
    post_long_position: int | None = None,
    post_short_position: int | None = None,
    execution_price: float | None = None,
) -> GateDecision:
    """Record the observed full or partial fill without changing the decision."""

    if decision.actual_net_risk_audit_v2 is None:
        raise ValueError("decision has no Actual Net Risk V2 audit")
    return replace(
        decision,
        actual_net_risk_audit_v2=finalize_actual_net_risk_audit(
            decision.actual_net_risk_audit_v2,
            executed_quantity,
            post_long_position=post_long_position,
            post_short_position=post_short_position,
            execution_price=execution_price,
        ),
    )


def pending_order_action(
    order: OrderProposal,
    position: int,
    permission: CapabilityPermission,
) -> tuple[str, GateDecision]:
    """Return CANCEL only for an existing risk-increasing order now denied."""
    assessment = assess_order(order, position)
    gate = apply_permission(order, position, permission)
    if assessment.risk_effect == RiskEffect.INCREASE.value and not gate.allowed:
        return "CANCEL", gate
    return "KEEP", gate
