"""Actual Net Risk V2: proposed, authorized, and executed position semantics.

The module is deliberately independent from legacy ``order_intent`` and
``risk_effect`` labels.  Those labels remain available for compatibility, but
this audit classifies each quantity by the position change it can actually
produce.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .models import OrderProposal

ACTUAL_NET_RISK_VERSION = "actual_net_risk_v2.1.1"


@dataclass(frozen=True)
class RiskChange:
    """The position effect of one proposed, authorized, or executed quantity."""

    quantity: int
    pre_long_position: int
    pre_short_position: int
    pre_net_position: int
    post_long_position: int
    post_short_position: int
    post_net_position: int
    close_or_reduce_long_quantity: int
    close_or_reduce_short_quantity: int
    new_or_increase_long_quantity: int
    new_or_increase_short_quantity: int
    actually_reduced_long: bool
    actually_closed_short: bool
    actually_added_long: bool
    actually_added_short: bool
    pre_net_direction: str
    post_net_direction: str
    pre_net_directional_exposure: float
    post_net_directional_exposure: float
    net_directional_risk_increased: bool
    pre_gross_exposure: float
    post_gross_exposure: float
    gross_exposure_increased: bool
    actual_net_risk_increase_quantity: int
    actual_net_risk_increase: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ActualNetRiskAuditV2:
    """A versioned audit that keeps proposed, authorized, and filled risk apart."""

    audit_semantics_version: str
    side: str
    price: float
    requested_quantity: int
    allowed_quantity: int
    executed_quantity: int | None
    execution_recorded: bool
    proposed_risk: RiskChange
    authorized_risk: RiskChange
    executed_risk: RiskChange | None
    execution_exceeds_authorized: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "audit_semantics_version": self.audit_semantics_version,
            "side": self.side,
            "price": self.price,
            "requested_quantity": self.requested_quantity,
            "allowed_quantity": self.allowed_quantity,
            "executed_quantity": self.executed_quantity,
            "execution_recorded": self.execution_recorded,
            "proposed_risk": self.proposed_risk.to_dict(),
            "authorized_risk": self.authorized_risk.to_dict(),
            "executed_risk": (
                self.executed_risk.to_dict() if self.executed_risk is not None else None
            ),
            "execution_exceeds_authorized": self.execution_exceeds_authorized,
        }


def _direction(position: int) -> str:
    if position > 0:
        return "LONG"
    if position < 0:
        return "SHORT"
    return "FLAT"


def _validate_positions(long_position: int, short_position: int, price: float) -> None:
    if long_position < 0 or short_position < 0:
        raise ValueError("long and short positions must be non-negative")
    if price <= 0:
        raise ValueError("price must be positive")


def assess_risk_change(
    *,
    side: str,
    quantity: int,
    pre_long_position: int,
    pre_short_position: int,
    price: float,
) -> RiskChange:
    """Split a BUY/SELL into close/reduce and reverse-opening components."""

    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    _validate_positions(pre_long_position, pre_short_position, price)

    close_long = min(quantity, pre_long_position) if side == "SELL" else 0
    close_short = min(quantity, pre_short_position) if side == "BUY" else 0
    add_short = quantity - close_long if side == "SELL" else 0
    add_long = quantity - close_short if side == "BUY" else 0
    post_long = pre_long_position - close_long + add_long
    post_short = pre_short_position - close_short + add_short
    pre_net = pre_long_position - pre_short_position
    post_net = post_long - post_short
    pre_net_exposure = abs(pre_net) * price
    post_net_exposure = abs(post_net) * price
    pre_gross = (pre_long_position + pre_short_position) * price
    post_gross = (post_long + post_short) * price
    epsilon = 1e-9
    new_risk_quantity = add_long + add_short

    return RiskChange(
        quantity=quantity,
        pre_long_position=pre_long_position,
        pre_short_position=pre_short_position,
        pre_net_position=pre_net,
        post_long_position=post_long,
        post_short_position=post_short,
        post_net_position=post_net,
        close_or_reduce_long_quantity=close_long,
        close_or_reduce_short_quantity=close_short,
        new_or_increase_long_quantity=add_long,
        new_or_increase_short_quantity=add_short,
        actually_reduced_long=close_long > 0,
        actually_closed_short=close_short > 0,
        actually_added_long=add_long > 0,
        actually_added_short=add_short > 0,
        pre_net_direction=_direction(pre_net),
        post_net_direction=_direction(post_net),
        pre_net_directional_exposure=pre_net_exposure,
        post_net_directional_exposure=post_net_exposure,
        net_directional_risk_increased=post_net_exposure > pre_net_exposure + epsilon,
        pre_gross_exposure=pre_gross,
        post_gross_exposure=post_gross,
        gross_exposure_increased=post_gross > pre_gross + epsilon,
        actual_net_risk_increase_quantity=new_risk_quantity,
        actual_net_risk_increase=int(new_risk_quantity > 0),
    )


def assess_observed_risk_change(
    *,
    side: str,
    pre_long_position: int,
    pre_short_position: int,
    post_long_position: int,
    post_short_position: int,
    price: float,
) -> RiskChange:
    """Classify the position delta an execution venue actually applied.

    StockSim maintains independent long and short books.  A SELL marked
    ``is_short`` can add short inventory while long inventory remains open, and
    a plain BUY can add long inventory while a short book remains open.  That
    cannot be reconstructed safely from side and quantity alone, so executed
    risk uses the observed before/after books as its authority.
    """

    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    _validate_positions(pre_long_position, pre_short_position, price)
    _validate_positions(post_long_position, post_short_position, price)
    delta_long = post_long_position - pre_long_position
    delta_short = post_short_position - pre_short_position
    if side == "BUY" and (delta_long < 0 or delta_short > 0):
        raise ValueError("observed position delta is not a BUY execution")
    if side == "SELL" and (delta_long > 0 or delta_short < 0):
        raise ValueError("observed position delta is not a SELL execution")

    close_long = -delta_long if side == "SELL" else 0
    close_short = -delta_short if side == "BUY" else 0
    add_long = delta_long if side == "BUY" else 0
    add_short = delta_short if side == "SELL" else 0
    quantity = close_long + close_short + add_long + add_short
    pre_net = pre_long_position - pre_short_position
    post_net = post_long_position - post_short_position
    pre_net_exposure = abs(pre_net) * price
    post_net_exposure = abs(post_net) * price
    pre_gross = (pre_long_position + pre_short_position) * price
    post_gross = (post_long_position + post_short_position) * price
    new_risk_quantity = add_long + add_short
    epsilon = 1e-9
    return RiskChange(
        quantity=quantity,
        pre_long_position=pre_long_position,
        pre_short_position=pre_short_position,
        pre_net_position=pre_net,
        post_long_position=post_long_position,
        post_short_position=post_short_position,
        post_net_position=post_net,
        close_or_reduce_long_quantity=close_long,
        close_or_reduce_short_quantity=close_short,
        new_or_increase_long_quantity=add_long,
        new_or_increase_short_quantity=add_short,
        actually_reduced_long=close_long > 0,
        actually_closed_short=close_short > 0,
        actually_added_long=add_long > 0,
        actually_added_short=add_short > 0,
        pre_net_direction=_direction(pre_net),
        post_net_direction=_direction(post_net),
        pre_net_directional_exposure=pre_net_exposure,
        post_net_directional_exposure=post_net_exposure,
        net_directional_risk_increased=post_net_exposure > pre_net_exposure + epsilon,
        pre_gross_exposure=pre_gross,
        post_gross_exposure=post_gross,
        gross_exposure_increased=post_gross > pre_gross + epsilon,
        actual_net_risk_increase_quantity=new_risk_quantity,
        actual_net_risk_increase=int(new_risk_quantity > 0),
    )


def assess_native_execution_plan(
    *,
    side: str,
    legs: Sequence[Mapping[str, Any]],
    pre_long_position: int,
    pre_short_position: int,
    price: float,
    require_fully_executable: bool = True,
) -> RiskChange:
    """Classify the exact StockSim close/open legs authorized for submission.

    StockSim does not infer close-first behavior from ``BUY``/``SELL``.  Its
    native flags are authoritative: ``is_short`` opens short,
    ``is_short_cover`` closes short, plain BUY opens long, and plain SELL closes
    long.  Capturing the generated legs prevents authorization risk from being
    silently represented with a different execution semantics.
    """

    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    _validate_positions(pre_long_position, pre_short_position, price)
    post_long = pre_long_position
    post_short = pre_short_position
    planned_quantity = 0
    for leg in legs:
        quantity = int(leg.get("quantity", 0))
        if quantity <= 0:
            raise ValueError("native leg quantity must be positive")
        is_short = bool(leg.get("is_short", False))
        is_short_cover = bool(leg.get("is_short_cover", False))
        if is_short and is_short_cover:
            raise ValueError("native leg cannot open and cover short simultaneously")
        if side == "BUY":
            if is_short:
                raise ValueError("BUY native leg cannot open short")
            if is_short_cover:
                post_short -= min(quantity, post_short)
            else:
                post_long += quantity
        else:
            if is_short_cover:
                raise ValueError("SELL native leg cannot cover short")
            if is_short:
                post_short += quantity
            else:
                post_long -= min(quantity, post_long)
        planned_quantity += quantity
    change = assess_observed_risk_change(
        side=side,
        pre_long_position=pre_long_position,
        pre_short_position=pre_short_position,
        post_long_position=post_long,
        post_short_position=post_short,
        price=price,
    )
    if require_fully_executable and change.quantity != planned_quantity:
        raise ValueError("native authorization plan contains an unexecutable close leg")
    return change


def build_actual_net_risk_audit(
    order: OrderProposal,
    *,
    pre_long_position: int,
    pre_short_position: int,
    allowed_quantity: int,
    executed_quantity: int | None = None,
    authorized_legs: Sequence[Mapping[str, Any]] | None = None,
) -> ActualNetRiskAuditV2:
    """Build proposed, authorized, and (when known) executed risk records.

    An execution may be larger than the authorization only to record a safety
    breach from an external execution venue; it is never silently clipped.
    """

    order.validate()
    if allowed_quantity < 0 or allowed_quantity > order.quantity:
        raise ValueError("allowed quantity must be between zero and requested quantity")
    if executed_quantity is not None and executed_quantity < 0:
        raise ValueError("executed quantity must be non-negative")
    proposed = assess_risk_change(
        side=order.side,
        quantity=order.quantity,
        pre_long_position=pre_long_position,
        pre_short_position=pre_short_position,
        price=order.price,
    )
    if authorized_legs is None:
        authorized = assess_risk_change(
            side=order.side,
            quantity=allowed_quantity,
            pre_long_position=pre_long_position,
            pre_short_position=pre_short_position,
            price=order.price,
        )
    else:
        if sum(int(leg.get("quantity", 0)) for leg in authorized_legs) != allowed_quantity:
            raise ValueError("native authorization legs do not match allowed quantity")
        authorized = assess_native_execution_plan(
            side=order.side,
            legs=authorized_legs,
            pre_long_position=pre_long_position,
            pre_short_position=pre_short_position,
            price=order.price,
        )
    executed = (
        assess_risk_change(
            side=order.side,
            quantity=executed_quantity,
            pre_long_position=pre_long_position,
            pre_short_position=pre_short_position,
            price=order.price,
        )
        if executed_quantity is not None
        else None
    )
    return ActualNetRiskAuditV2(
        audit_semantics_version=ACTUAL_NET_RISK_VERSION,
        side=order.side,
        price=order.price,
        requested_quantity=order.quantity,
        allowed_quantity=allowed_quantity,
        executed_quantity=executed_quantity,
        execution_recorded=executed_quantity is not None,
        proposed_risk=proposed,
        authorized_risk=authorized,
        executed_risk=executed,
        execution_exceeds_authorized=(
            executed_quantity is not None and executed_quantity > allowed_quantity
        ),
    )


def finalize_actual_net_risk_audit(
    audit: ActualNetRiskAuditV2,
    executed_quantity: int,
    *,
    post_long_position: int | None = None,
    post_short_position: int | None = None,
    execution_price: float | None = None,
) -> ActualNetRiskAuditV2:
    """Attach an observed full or partial fill to an existing authorization audit."""

    if executed_quantity < 0:
        raise ValueError("executed quantity must be non-negative")
    authorized = audit.authorized_risk
    if (post_long_position is None) != (post_short_position is None):
        raise ValueError("provide both observed post-execution positions")
    if post_long_position is None:
        executed = assess_risk_change(
            side=audit.side,
            quantity=executed_quantity,
            pre_long_position=authorized.pre_long_position,
            pre_short_position=authorized.pre_short_position,
            price=audit.price,
        )
    else:
        assert post_short_position is not None
        executed = assess_observed_risk_change(
            side=audit.side,
            pre_long_position=authorized.pre_long_position,
            pre_short_position=authorized.pre_short_position,
            post_long_position=post_long_position,
            post_short_position=post_short_position,
            price=execution_price or audit.price,
        )
        if executed.quantity > executed_quantity:
            raise ValueError("observed position delta exceeds cumulative executed quantity")
    return ActualNetRiskAuditV2(
        audit_semantics_version=audit.audit_semantics_version,
        side=audit.side,
        price=audit.price,
        requested_quantity=audit.requested_quantity,
        allowed_quantity=audit.allowed_quantity,
        executed_quantity=executed_quantity,
        execution_recorded=True,
        proposed_risk=audit.proposed_risk,
        authorized_risk=audit.authorized_risk,
        executed_risk=executed,
        execution_exceeds_authorized=executed_quantity > audit.allowed_quantity,
    )
