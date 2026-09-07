"""Frozen schemas for simulator-level capability control."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .actual_risk import ActualNetRiskAuditV2


class OrderIntent(str, Enum):
    OPEN_LONG = "OPEN_LONG"
    INCREASE_LONG = "INCREASE_LONG"
    REDUCE_LONG = "REDUCE_LONG"
    CLOSE_LONG = "CLOSE_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    INCREASE_SHORT = "INCREASE_SHORT"
    REDUCE_SHORT = "REDUCE_SHORT"
    CLOSE_SHORT = "CLOSE_SHORT"
    MARKET_MAKING_QUOTE = "MARKET_MAKING_QUOTE"


class RiskEffect(str, Enum):
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True)
class CapabilityPermission:
    governance_decision_id: str
    agent_id: str
    governance_state: str
    valid_from: str
    valid_until: str
    allow_new_position: bool
    allow_risk_increase: bool
    allow_open_short: bool
    allow_leverage_increase: bool
    reduce_only: bool
    allow_market_order: bool
    allow_limit_order: bool
    allow_cancel_order: bool
    max_order_notional: float
    max_position_notional: float
    max_gross_exposure: float
    max_net_exposure: float
    max_gross_leverage: float | None
    max_orders_per_interval: int
    reason_code: str

    def validate(self) -> CapabilityPermission:
        if not self.governance_decision_id or not self.agent_id:
            raise ValueError("decision and agent IDs are required")
        if self.governance_state not in {
            "NORMAL",
            "CAUTION",
            "RESTRICTED",
            "ISOLATED",
            "STAGED_REENTRY_1",
            "STAGED_REENTRY_2",
        }:
            raise ValueError("unknown governance state")
        for value in (
            self.max_order_notional,
            self.max_position_notional,
            self.max_gross_exposure,
            self.max_net_exposure,
        ):
            if value < 0:
                raise ValueError("permission limits must be non-negative")
        if self.max_gross_leverage is not None:
            raise ValueError("StockSim has no genuine leverage accounting")
        if self.max_orders_per_interval < 0:
            raise ValueError("order frequency limit must be non-negative")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.validate())


@dataclass(frozen=True)
class OrderProposal:
    order_id: str
    agent_id: str
    role: str
    instrument: str
    side: str
    quantity: int
    order_type: str
    price: float
    source: str
    proposed_at: str
    market_making_quote: bool = False
    parent_order_direction: str = ""
    hold_until_index: int | None = None

    def validate(self) -> OrderProposal:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if self.order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("order type must be MARKET or LIMIT")
        if self.quantity <= 0 or self.price <= 0:
            raise ValueError("quantity and price must be positive")
        if self.parent_order_direction and self.parent_order_direction != self.side:
            raise ValueError("proposal cannot rewrite institutional parent direction")
        return self


@dataclass(frozen=True)
class IntentAssessment:
    order_intent: str
    risk_effect: str
    pre_position: int
    projected_post_position: int
    pre_gross_exposure: float
    projected_gross_exposure: float
    pre_net_exposure: float
    projected_net_exposure: float
    crosses_zero: bool
    closing_quantity: int
    opening_quantity: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    original_quantity: int
    allowed_quantity: int
    decision: str
    reason: str
    assessment: IntentAssessment
    governance_forced_direction_change: bool = False
    actual_net_risk_audit_v2: ActualNetRiskAuditV2 | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            **asdict(self),
            "assessment": self.assessment.to_dict(),
        }
        if self.actual_net_risk_audit_v2 is not None:
            result["actual_net_risk_audit_v2"] = self.actual_net_risk_audit_v2.to_dict()
        return result
