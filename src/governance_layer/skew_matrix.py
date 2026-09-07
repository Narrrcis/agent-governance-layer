"""Deterministic authorization/execution state-skew pressure matrix.

Scenarios describe how the independent StockSim books move while a native
order rests.  Acceptance properties are evaluated separately from those inputs:
observed decomposition, hard exposure compliance, silent substitution, and
risk-reduction blocking.  This is a regression harness, not calibration or
effect evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .actual_risk import assess_native_execution_plan
from .models import OrderProposal
from .pending_guard import pending_native_order_action
from .permissions import permission_for

_NOW = "2026-11-01T09:30:00+00:00"
_PRICE = 100.0


@dataclass(frozen=True)
class _SkewScenario:
    case_id: str
    mechanism: str
    side: str
    quantity: int
    is_short: bool
    is_short_cover: bool
    authorization_long: int
    authorization_short: int
    execution_long: int
    execution_short: int


@dataclass(frozen=True)
class SkewMatrixResult:
    case_id: str
    governance_state: str
    scenario_mechanism: str
    authorized_opening_quantity: int
    actual_opening_quantity: int
    actual_executed_quantity: int
    actual_close_quantity: int
    realized_opening_exceeds_authorized: bool
    actual_uses_execution_book: bool
    gross_limit: float
    net_limit: float
    actual_post_gross_exposure: float
    actual_post_net_exposure: float
    gross_limit_breach_if_executed: bool
    net_limit_breach_if_executed: bool
    guard_action: str
    guard_reason: str
    hard_boundary_compliant_if_guard_honored: bool
    hard_boundary_compliant_under_cancel_execute_race: bool
    risk_reduction_blocked: bool
    unnecessary_intervention: bool
    silent_authorization_substitution: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SCENARIOS = (
    _SkewScenario(
        case_id="S0",
        mechanism="book_unchanged_native_open_short_control",
        side="SELL",
        quantity=20,
        is_short=True,
        is_short_cover=False,
        authorization_long=70,
        authorization_short=60,
        execution_long=70,
        execution_short=60,
    ),
    _SkewScenario(
        case_id="S1",
        mechanism="short_book_grows_while_native_open_short_rests",
        side="SELL",
        quantity=20,
        is_short=True,
        is_short_cover=False,
        authorization_long=70,
        authorization_short=60,
        execution_long=70,
        execution_short=80,
    ),
    _SkewScenario(
        case_id="S2",
        mechanism="native_close_long_target_disappears_and_book_reverses",
        side="SELL",
        quantity=20,
        is_short=False,
        is_short_cover=False,
        authorization_long=20,
        authorization_short=0,
        execution_long=0,
        execution_short=95,
    ),
    _SkewScenario(
        case_id="S3",
        mechanism="native_close_long_target_shrinks",
        side="SELL",
        quantity=20,
        is_short=False,
        is_short_cover=False,
        authorization_long=20,
        authorization_short=0,
        execution_long=5,
        execution_short=0,
    ),
)


def _opening(change: Any) -> int:
    return (
        change.new_or_increase_long_quantity
        + change.new_or_increase_short_quantity
    )


def _closing(change: Any) -> int:
    return (
        change.close_or_reduce_long_quantity
        + change.close_or_reduce_short_quantity
    )


def run_authorization_execution_skew_matrix(
    *, severe_state: str = "RESTRICTED"
) -> tuple[SkewMatrixResult, ...]:
    """Evaluate S0-S3 once in NORMAL and once in one severe state."""

    if severe_state not in {"RESTRICTED", "ISOLATED"}:
        raise ValueError("severe_state must be RESTRICTED or ISOLATED")
    results: list[SkewMatrixResult] = []
    for state in ("NORMAL", severe_state):
        permission = permission_for(
            run_id="authorization-execution-skew-matrix",
            agent_id="market_maker",
            role="market_maker",
            state=state,
            governance_time=_NOW,
            reason_code="AUTHORIZATION_EXECUTION_STATE_SKEW",
            index=0,
        )
        for scenario in _SCENARIOS:
            order = OrderProposal(
                order_id=f"{scenario.case_id}-{state}",
                agent_id="market_maker",
                role="market_maker",
                instrument="AAPL",
                side=scenario.side,
                quantity=scenario.quantity,
                order_type="LIMIT",
                price=_PRICE,
                source="authorization_execution_state_skew",
                proposed_at=_NOW,
                market_making_quote=True,
            ).validate()
            native_leg = {
                "quantity": scenario.quantity,
                "is_short": scenario.is_short,
                "is_short_cover": scenario.is_short_cover,
            }
            authorized = assess_native_execution_plan(
                side=scenario.side,
                legs=(native_leg,),
                pre_long_position=scenario.authorization_long,
                pre_short_position=scenario.authorization_short,
                price=_PRICE,
                require_fully_executable=False,
            )
            actual = assess_native_execution_plan(
                side=scenario.side,
                legs=(native_leg,),
                pre_long_position=scenario.execution_long,
                pre_short_position=scenario.execution_short,
                price=_PRICE,
                require_fully_executable=False,
            )
            guard = pending_native_order_action(
                order,
                pre_long_position=scenario.execution_long,
                pre_short_position=scenario.execution_short,
                permission=permission,
                is_short=scenario.is_short,
                is_short_cover=scenario.is_short_cover,
            )
            gross_breach = actual.post_gross_exposure > permission.max_gross_exposure
            net_breach = (
                actual.post_net_directional_exposure > permission.max_net_exposure
            )
            actual_opening = _opening(actual)
            actual_closing = _closing(actual)
            intervention = guard.action == "CANCEL"
            results.append(
                SkewMatrixResult(
                    case_id=scenario.case_id,
                    governance_state=state,
                    scenario_mechanism=scenario.mechanism,
                    authorized_opening_quantity=_opening(authorized),
                    actual_opening_quantity=actual_opening,
                    actual_executed_quantity=actual.quantity,
                    actual_close_quantity=actual_closing,
                    realized_opening_exceeds_authorized=(
                        actual_opening > _opening(authorized)
                    ),
                    actual_uses_execution_book=(
                        actual.pre_long_position == scenario.execution_long
                        and actual.pre_short_position == scenario.execution_short
                    ),
                    gross_limit=permission.max_gross_exposure,
                    net_limit=permission.max_net_exposure,
                    actual_post_gross_exposure=actual.post_gross_exposure,
                    actual_post_net_exposure=actual.post_net_directional_exposure,
                    gross_limit_breach_if_executed=gross_breach,
                    net_limit_breach_if_executed=net_breach,
                    guard_action=guard.action,
                    guard_reason=guard.reason,
                    hard_boundary_compliant_if_guard_honored=(
                        intervention or not (gross_breach or net_breach)
                    ),
                    hard_boundary_compliant_under_cancel_execute_race=not (
                        gross_breach or net_breach
                    ),
                    risk_reduction_blocked=(intervention and actual_closing > 0),
                    unnecessary_intervention=(
                        intervention and not (gross_breach or net_breach)
                    ),
                    silent_authorization_substitution=False,
                )
            )
    return tuple(results)
