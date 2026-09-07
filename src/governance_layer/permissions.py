"""Role-specific, preregistered state-to-capability mappings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import timedelta
from typing import TypedDict

from .models import CapabilityPermission, parse_governance_time


class RoleEnvelope(TypedDict):
    """Absolute limits for a role, before the state multiplier is applied."""

    allow_open_short: bool
    max_order_notional: float
    max_position_notional: float
    max_gross_exposure: float
    max_net_exposure: float
    max_orders_per_interval: int


class StateRule(TypedDict):
    """What a governance state permits, and how hard it scales the envelope.

    ``short`` is tri-valued: ``True``/``False`` decide directly, ``"BASE"``
    defers to the role envelope, and ``"CONDITIONAL"`` grants the capability to
    market makers only.
    """

    multiplier: float
    new: bool
    increase: bool
    short: bool | str
    reduce: bool
    market: bool
    limit: bool
    cancel: bool
    frequency: float

# Bumped whenever the state-to-capability mapping below changes meaning. It is
# part of the decision-ID material, so a permission issued under a different
# policy can never collide with this one.
POLICY_SCHEMA_VERSION = "governance_policy_v2.1.0"


def derive_decision_id(
    permission: CapabilityPermission,
    *,
    run_id: str,
    index: int,
) -> str:
    """Derive a decision ID that commits to the entire permission.

    The ID is a SHA-256 over the canonical JSON of every field except the ID
    itself, plus the policy schema version and the issuing run and index. Two
    permissions that differ in state, role, reason, validity window or any
    limit therefore get different IDs, so an ID can be used to detect a
    substituted or replayed authorization rather than merely to label one.
    """

    payload = asdict(permission)
    payload.pop("governance_decision_id", None)
    material = {
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "run_id": run_id,
        "index": index,
        "permission": payload,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

ROLE_BASE: dict[str, RoleEnvelope] = {
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


STATE_RULES: dict[str, StateRule] = {
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
    short_rule = rule["short"]
    if short_rule == "BASE":
        allow_short = base["allow_open_short"]
    elif short_rule == "CONDITIONAL":
        allow_short = role == "market_maker"
    else:
        allow_short = bool(short_rule)
    current = parse_governance_time(governance_time, "governance_time")
    multiplier = float(rule["multiplier"])
    permission = CapabilityPermission(
        governance_decision_id="",
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
    )
    return replace(
        permission,
        governance_decision_id=derive_decision_id(permission, run_id=run_id, index=index),
    ).validate()


SCALAR_ACTION_CAP: dict[str, float] = {
    "NORMAL": 1.0,
    "CAUTION": 0.85,
    "RESTRICTED": 0.60,
    "ISOLATED": 0.0,
    "STAGED_REENTRY_1": 0.35,
    "STAGED_REENTRY_2": 0.65,
}
