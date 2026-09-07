"""Revalidate resting native orders against the live independent books."""

from __future__ import annotations

from dataclasses import dataclass

from .actual_risk import RiskChange, assess_native_execution_plan
from .models import CapabilityPermission, OrderProposal


@dataclass(frozen=True)
class PendingNativeOrderDecision:
    action: str
    reason: str
    actual_if_executed: RiskChange
    gross_limit_breach_if_executed: bool
    net_limit_breach_if_executed: bool


def pending_native_order_action(
    order: OrderProposal,
    *,
    pre_long_position: int,
    pre_short_position: int,
    permission: CapabilityPermission,
    is_short: bool = False,
    is_short_cover: bool = False,
) -> PendingNativeOrderDecision:
    """Return a selective KEEP/CANCEL decision using native leg semantics.

    Closing and covering legs remain available even in severe states.  A native
    opening leg is cancelled when current permissions forbid opening/increasing
    risk or when its execution would cross the current gross/net hard limit.
    """

    order.validate()
    permission.validate()
    if order.agent_id != permission.agent_id:
        raise ValueError("permission agent does not match order agent")
    effect = assess_native_execution_plan(
        side=order.side,
        legs=(
            {
                "quantity": order.quantity,
                "is_short": is_short,
                "is_short_cover": is_short_cover,
            },
        ),
        pre_long_position=pre_long_position,
        pre_short_position=pre_short_position,
        price=order.price,
        require_fully_executable=False,
    )
    opening = (
        effect.new_or_increase_long_quantity
        + effect.new_or_increase_short_quantity
    )
    gross_breach = effect.post_gross_exposure > permission.max_gross_exposure + 1e-9
    net_breach = (
        effect.post_net_directional_exposure > permission.max_net_exposure + 1e-9
    )

    # A native close/cover that has become a no-op or remains risk-reducing is
    # never turned into an opening by StockSim.  Keeping it cannot amplify risk.
    if opening == 0:
        return PendingNativeOrderDecision(
            action="KEEP",
            reason=(
                "NATIVE_CLOSE_TARGET_ABSENT"
                if effect.quantity == 0
                else "NATIVE_RISK_REDUCTION_PRESERVED"
            ),
            actual_if_executed=effect,
            gross_limit_breach_if_executed=gross_breach,
            net_limit_breach_if_executed=net_breach,
        )

    if permission.reduce_only or not permission.allow_risk_increase:
        reason = "SEVERE_STATE_NATIVE_OPENING_NOT_PERMITTED"
    elif effect.new_or_increase_short_quantity and not permission.allow_open_short:
        reason = "NATIVE_OPEN_SHORT_NOT_PERMITTED"
    elif (
        effect.pre_long_position == 0
        and effect.pre_short_position == 0
        and not permission.allow_new_position
    ):
        reason = "NATIVE_NEW_POSITION_NOT_PERMITTED"
    elif gross_breach or net_breach:
        reason = "NATIVE_EXECUTION_HARD_EXPOSURE_LIMIT"
    elif order.order_type == "MARKET" and not permission.allow_market_order:
        reason = "NATIVE_MARKET_ORDER_NOT_PERMITTED"
    elif order.order_type == "LIMIT" and not permission.allow_limit_order:
        reason = "NATIVE_LIMIT_ORDER_NOT_PERMITTED"
    else:
        return PendingNativeOrderDecision(
            action="KEEP",
            reason="NATIVE_OPENING_STILL_PERMITTED",
            actual_if_executed=effect,
            gross_limit_breach_if_executed=gross_breach,
            net_limit_breach_if_executed=net_breach,
        )
    return PendingNativeOrderDecision(
        action="CANCEL",
        reason=reason,
        actual_if_executed=effect,
        gross_limit_breach_if_executed=gross_breach,
        net_limit_breach_if_executed=net_breach,
    )
