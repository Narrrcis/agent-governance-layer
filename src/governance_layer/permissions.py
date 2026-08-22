"""Role-specific, preregistered state-to-capability mappings."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from .models import CapabilityPermission

ROLE_BASE = {
    "market_maker": {
        "allow_open_short": True,
        "max_order_notional": 12000.0,
        "max_position_notional": 30000.0,
        "max_gross_exposure": 40000.0,
        "max_net_exposure": 25000.0,
        "max_orders_per_interval": 4,
    },
    "retail": {
        "allow_open_short": True,
        "max_order_notional": 5000.0,
        "max_position_notional": 20000.0,
        "max_gross_exposure": 20000.0,
        "max_net_exposure": 20000.0,
        "max_orders_per_interval": 2,
    },
    "institutional": {
        "allow_open_short": True,
        "max_order_notional": 20000.0,
        "max_position_notional": 100000.0,
        "max_gross_exposure": 120000.0,
        "max_net_exposure": 100000.0,
        "max_orders_per_interval": 3,
    },
}


STATE_RULES = {
    "NORMAL": dict(
        multiplier=1.0,
        new=True,
        increase=True,
        short="BASE",
        reduce=False,
        market=True,
        limit=True,
        cancel=True,
        frequency=1.0,
    ),
    "CAUTION": dict(
        multiplier=0.60,
        new=True,
        increase=True,
        short=False,
        reduce=False,
        market=False,
        limit=True,
        cancel=True,
        frequency=0.67,
    ),
    "RESTRICTED": dict(
        multiplier=0.40,
        new=False,
        increase=False,
        short=False,
        reduce=True,
        market=True,
        limit=True,
        cancel=True,
        frequency=0.50,
    ),
    "ISOLATED": dict(
        multiplier=0.25,
        new=False,
        increase=False,
        short=False,
        reduce=True,
        market=True,
        limit=True,
        cancel=True,
        frequency=0.50,
    ),
    "STAGED_REENTRY_1": dict(
        multiplier=0.30,
        new=True,
        increase=True,
        short=False,
        reduce=False,
        market=False,
        limit=True,
        cancel=True,
        frequency=0.50,
    ),
    "STAGED_REENTRY_2": dict(
        multiplier=0.55,
        new=True,
        increase=True,
        short="CONDITIONAL",
        reduce=False,
        market=False,
        limit=True,
        cancel=True,
        frequency=0.67,
    ),
}


def permission_for(
    *,
    run_id: str,
    agent_id: str,
    role: str,
    state: str,
    governance_time: str,
    reason_code: str,
    index: int,
) -> CapabilityPermission:
    base = ROLE_BASE[role]
    rule = STATE_RULES[state]
    allow_short = (
        base["allow_open_short"]
        if rule["short"] == "BASE"
        else role == "market_maker"
        if rule["short"] == "CONDITIONAL"
        else bool(rule["short"])
    )
    current = datetime.fromisoformat(governance_time)
    multiplier = float(rule["multiplier"])
    decision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}|{index}|{agent_id}|capability"))
    return CapabilityPermission(
        governance_decision_id=decision_id,
        agent_id=agent_id,
        governance_state=state,
        valid_from=governance_time,
        valid_until=(current + timedelta(seconds=60)).isoformat(),
        allow_new_position=bool(rule["new"]),
        allow_risk_increase=bool(rule["increase"]),
        allow_open_short=allow_short,
        allow_leverage_increase=False,
        reduce_only=bool(rule["reduce"]),
        allow_market_order=bool(rule["market"]),
        allow_limit_order=bool(rule["limit"]),
        allow_cancel_order=bool(rule["cancel"]),
        max_order_notional=base["max_order_notional"] * multiplier,
        max_position_notional=base["max_position_notional"] * multiplier,
        max_gross_exposure=base["max_gross_exposure"] * multiplier,
        max_net_exposure=base["max_net_exposure"] * multiplier,
        max_gross_leverage=None,
        max_orders_per_interval=max(1, int(base["max_orders_per_interval"] * rule["frequency"])),
        reason_code=reason_code,
    ).validate()


SCALAR_ACTION_CAP = {
    "NORMAL": 1.0,
    "CAUTION": 0.85,
    "RESTRICTED": 0.60,
    "ISOLATED": 0.0,
    "STAGED_REENTRY_1": 0.35,
    "STAGED_REENTRY_2": 0.65,
}
