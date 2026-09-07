"""Bind one live AML-Sim/StockSim agent to the V2.1 execution audit bridge.

The simulator owns execution and portfolio accounting; this module owns only
the audit.  It attaches to the native lifecycle at the single point where the
observed inventory change is already final -- immediately after
``BaseAMLAgent.on_trade_execution`` has called ``super().on_trade_execution``
and has produced its pre- and post-fill portfolio snapshots.

Three properties are load-bearing and are asserted here rather than assumed:

* The filled quantity is derived from the *observed* long/short delta, never
  from ``trade_data["quantity"]`` and never from ``allowed_quantity``.  StockSim
  clamps a SHORT_COVER to the inventory actually held and can execute nothing at
  all, so the requested quantity on the message is not a fill.
* A fill that has already reached the venue can never be undone by an audit
  failure.  Every raw execution message is therefore persisted before any
  derived record, and an audit failure marks the run unusable for effect
  analysis instead of being swallowed or raised back into the callback.
* A run that is not governed produces no governance audit at all, through an
  explicit no-op recorder rather than through a partially initialised one.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .execution_audit import ExecutionAuditEvent, StockSimExecutionAuditBridge
from .models import GateDecision, OrderProposal

AUDIT_BINDING_VERSION = "governance_runtime_binding_v2.1.1"

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def filesystem_agent_slug(agent_id: str) -> str:
    """Return a filename-safe token that still identifies this agent uniquely.

    Agent IDs come from simulator configuration and are not guaranteed to be
    safe path components. Interpolating one straight into a filename would let
    ``../`` or a path separator place audit artifacts outside the audit
    directory. Unsafe characters are replaced, and whenever anything had to be
    replaced a digest of the original is appended so that two distinct agents
    can never collapse onto the same audit file.
    """

    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", agent_id)
    # Collapse dot runs so no artifact is ever named with a ".." component.
    cleaned = re.sub(r"\.{2,}", "_", cleaned).strip(".")
    if not cleaned:
        cleaned = "agent"
    if cleaned != agent_id or len(cleaned) > 64:
        digest = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()[:12]
        return f"{cleaned[:64]}-{digest}"
    return cleaned

RAW_EVENT_FILE = "execution_events_raw.jsonl"
AUDIT_EVENT_FILE = "execution_audit.jsonl"
INTEGRITY_FILE = "audit_integrity.json"
VALIDATION_FILE = "audit_validation_failures.json"

# Failure reasons.  Every one of these makes the run ineligible for the formal
# effect analysis; none of them pretends the fill did not happen.
REASON_POSITION_MISMATCH = "POSITION_MISMATCH"
REASON_POSITION_DELTA_INCONSISTENT = "POSITION_DELTA_INCONSISTENT"
REASON_AUDIT_WRITE_FAILED = "AUDIT_WRITE_FAILED"
REASON_UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"
REASON_REGISTRATION_FAILED = "REGISTRATION_FAILED"

# Who asked for a cancellation.  A market maker replaces its own quotes
# constantly, so an unattributed cancel/execute race says nothing about the
# governance mechanism; only a guard-issued cancel that loses the race does.
CANCEL_ORIGIN_AGENT = "agent_quote_management"
CANCEL_ORIGIN_GOVERNANCE = "governance_guard"
CANCEL_ORIGIN_UNKNOWN = "unknown"


class AuditInvalidError(RuntimeError):
    """Raised only by explicit strict checks, never inside a fill callback."""


@dataclass(frozen=True)
class AuditFailure:
    reason: str
    detail: str
    order_id: str = ""
    fill_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditIntegrityReport:
    """Closing statement for one agent's audit within one run."""

    binding_version: str
    run_id: str
    agent_id: str
    mechanism: str
    governed: bool
    audit_valid: bool
    audit_complete: bool
    eligible_for_effect_analysis: bool
    registered_order_count: int = 0
    raw_event_count: int = 0
    audit_event_count: int = 0
    duplicate_message_count: int = 0
    no_position_change_count: int = 0
    unattributed_fill_count: int = 0
    cancel_requests_by_origin: dict[str, int] = field(default_factory=dict)
    race_fill_count: int = 0
    governance_race_fill_count: int = 0
    governance_race_fill_opening_count: int = 0
    executed_quantity_total: int = 0
    authorized_quantity_total: int = 0
    proposed_quantity_total: int = 0
    severe_state_executed_net_risk_increase: int = 0
    execution_exceeds_authorized: int = 0
    realized_opening_exceeds_authorized: int = 0
    realized_opening_exceeds_authorized_order_count: int = 0
    severe_state_realized_opening_exceeds_authorized: int = 0
    severe_state_gross_exposure_limit_breach: int = 0
    severe_state_net_exposure_limit_breach: int = 0
    severe_state_hard_exposure_limit_breach: int = 0
    stop_requested: bool = False
    failures: list[dict[str, Any]] = field(default_factory=list)
    artifact_sha256: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def positions_from_snapshot(
    snapshot: Mapping[str, Any], instrument: str
) -> tuple[int, int]:
    """Read ``positions[instrument][long|short]`` from a native snapshot."""

    positions = snapshot.get("positions")
    if not isinstance(positions, Mapping) or instrument not in positions:
        raise KeyError(instrument)
    book = positions[instrument]
    return int(book.get("long", 0)), int(book.get("short", 0))


def fill_quantity_from_delta(
    side: str,
    pre_long: int,
    pre_short: int,
    post_long: int,
    post_short: int,
) -> int:
    """Derive the executed quantity from the inventory the simulator actually moved.

    Returns ``0`` when the callback produced no position change, and raises when
    the delta cannot be produced by a single fill on ``side``.
    """

    delta_long = post_long - pre_long
    delta_short = post_short - pre_short
    if side == "BUY":
        closed_short, opened = -delta_short, delta_long
    elif side == "SELL":
        closed_short, opened = -delta_long, delta_short
    else:
        raise ValueError("side must be BUY or SELL")
    if closed_short < 0 or opened < 0:
        raise ValueError(
            f"position delta long={delta_long} short={delta_short} is not a {side} fill"
        )
    if closed_short and opened and side == "BUY" and pre_short != closed_short:
        raise ValueError("a BUY may only open long after the short book is closed")
    if closed_short and opened and side == "SELL" and pre_long != closed_short:
        raise ValueError("a SELL may only open short after the long book is closed")
    return closed_short + opened


def message_fill_key(trade_data: Mapping[str, Any]) -> tuple[str, bool]:
    """Return a stable per-fill identity and whether it had to be synthesised.

    The order-book exchange stamps every trade with a monotonic ``seq``, which is
    a true broker-side fill identity.  The candle exchange fills an order exactly
    once and stamps none, so the key falls back to the message content and is
    reported as synthetic.
    """

    order_id = str(trade_data.get("order_id", ""))
    for name in ("fill_id", "trade_id", "execution_id", "seq"):
        value = trade_data.get(name)
        if value not in (None, ""):
            return f"{order_id}|{name}={value}", False
    parts = "|".join(
        str(trade_data.get(name, ""))
        for name in ("order_status", "timestamp", "quantity", "price")
    )
    return f"{order_id}|content={parts}", True


class NullExecutionAuditRecorder:
    """Explicit no-op recorder for a run that is not under governance audit.

    It is a distinct type rather than a disabled flag so that a non-governed run
    cannot accidentally hold a half-configured real bridge.
    """

    governed = False

    def __init__(self, *, reason: str = "EXECUTION_AUDIT_DISABLED") -> None:
        self.reason = reason
        self.audit_valid = True
        self.stop_requested = False

    def register_order(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def bind_native_order_ids(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def note_cancel_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def note_cancel_confirmed(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_fill(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def mark_invalid(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def events(self) -> tuple[ExecutionAuditEvent, ...]:
        return ()

    def close(self) -> AuditIntegrityReport:
        return AuditIntegrityReport(
            binding_version=AUDIT_BINDING_VERSION,
            run_id="",
            agent_id="",
            mechanism="",
            governed=False,
            audit_valid=True,
            audit_complete=True,
            eligible_for_effect_analysis=False,
        )


class ExecutionAuditRecorder:
    """One bridge, one agent, one run, one audit directory."""

    governed = True

    def __init__(
        self,
        *,
        run_id: str,
        agent_id: str,
        mechanism: str,
        audit_dir: Path | str,
        bridge: StockSimExecutionAuditBridge | None = None,
        strict_on_unattributed: bool = False,
    ) -> None:
        if not run_id:
            raise ValueError("a governed execution audit requires a run_id")
        if not agent_id:
            raise ValueError("a governed execution audit requires an agent_id")
        self.run_id = run_id
        self.agent_id = agent_id
        self.agent_slug = filesystem_agent_slug(agent_id)
        self.mechanism = mechanism
        self.audit_dir = Path(audit_dir)
        self.bridge = bridge or StockSimExecutionAuditBridge()
        self.strict_on_unattributed = strict_on_unattributed

        self.audit_valid = True
        self.stop_requested = False
        self.failures: list[AuditFailure] = []
        self._seen_fill_keys: set[str] = set()
        self._orders: dict[str, dict[str, Any]] = {}
        self._native_to_logical: dict[str, str] = {}
        self._audit_events: list[ExecutionAuditEvent] = []
        self._cancel_origin_by_native: dict[str, str] = {}
        self._cancel_origin_by_logical: dict[str, str] = {}
        self._cancel_requests_by_origin: dict[str, int] = {}
        self._race_fills = 0
        self._governance_race_fills = 0
        self._governance_race_opening_fills = 0
        self._raw_count = 0
        self._duplicates = 0
        self._no_change = 0
        self._unattributed = 0
        self._closed = False
        self._prepare_dir()

    # -- lifecycle ---------------------------------------------------------

    def _prepare_dir(self) -> None:
        try:
            self.audit_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._fail(REASON_AUDIT_WRITE_FAILED, f"cannot create {self.audit_dir}: {exc}")

    def _path(self, name: str) -> Path:
        stem, suffix = name.rsplit(".", 1)
        candidate = self.audit_dir / f"{stem}_{self.agent_slug}.{suffix}"
        # Defence in depth: the slug should already make this impossible, but
        # an audit that escapes its directory is never acceptable.
        base = self.audit_dir.resolve()
        if not candidate.resolve().parent == base:
            raise ValueError(f"audit path escapes the audit directory: {candidate}")
        return candidate

    def _append(self, name: str, row: Mapping[str, Any]) -> None:
        payload = {
            "binding_version": AUDIT_BINDING_VERSION,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "mechanism": self.mechanism,
            **row,
        }
        try:
            with self._path(name).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # The fill already happened.  Surface it on stderr so the evidence is
            # not confined to a file system that just refused a write.
            print(
                json.dumps(
                    {
                        "governance_audit_write_failure": str(exc),
                        "unwritten_record": payload,
                    },
                    default=str,
                ),
                file=sys.stderr,
                flush=True,
            )
            self._fail(REASON_AUDIT_WRITE_FAILED, f"{name}: {exc}")

    def _fail(self, reason: str, detail: str, *, order_id: str = "", fill_key: str = "") -> None:
        self.audit_valid = False
        self.stop_requested = True
        self.failures.append(
            AuditFailure(reason=reason, detail=detail, order_id=order_id, fill_key=fill_key)
        )

    def mark_invalid(self, reason: str, detail: str) -> None:
        """Public entry point for a caller that detected an audit-invalidating fact."""

        self._fail(reason, detail)

    # -- authorization -----------------------------------------------------

    def register_order(
        self,
        order: OrderProposal,
        decision: GateDecision,
        *,
        pre_long_position: int,
        pre_short_position: int,
        governance_state: str,
        native_order_ids: tuple[str, ...] = (),
    ) -> None:
        try:
            self.bridge.register_authorization(
                order,
                decision,
                pre_long_position=pre_long_position,
                pre_short_position=pre_short_position,
                governance_state=governance_state,
                native_order_ids=tuple(native_order_ids),
            )
        except ValueError as exc:
            self._fail(REASON_REGISTRATION_FAILED, str(exc), order_id=order.order_id)
            return
        self._orders[order.order_id] = {
            "instrument": order.instrument,
            "side": order.side,
            "proposed_quantity": order.quantity,
            "allowed_quantity": decision.allowed_quantity,
            "governance_state": governance_state,
        }
        for native_order_id in native_order_ids:
            self._native_to_logical[str(native_order_id)] = order.order_id
        self._append(
            AUDIT_EVENT_FILE,
            {
                "record_type": "authorization",
                "order_id": order.order_id,
                "native_order_ids": [str(value) for value in native_order_ids],
                "governance_state": governance_state,
                "decision": decision.to_dict(),
            },
        )

    def bind_native_order_ids(
        self, logical_order_id: str, native_order_ids: tuple[str, ...]
    ) -> None:
        try:
            self.bridge.bind_native_order_ids(logical_order_id, tuple(native_order_ids))
        except ValueError as exc:
            self._fail(REASON_REGISTRATION_FAILED, str(exc), order_id=logical_order_id)
            return
        for native_order_id in native_order_ids:
            self._native_to_logical[str(native_order_id)] = logical_order_id

    def note_cancel_request(
        self, native_order_id: str, *, origin: str = CANCEL_ORIGIN_UNKNOWN
    ) -> None:
        logical = self._logical(native_order_id)
        if logical is None:
            return
        native_order_id = str(native_order_id)
        self.bridge.request_cancel(logical)
        self._cancel_origin_by_native[native_order_id] = origin
        # A governance-issued cancel outranks a routine one on the same logical
        # order: the safety question is whether the guard lost the race.
        if (
            origin == CANCEL_ORIGIN_GOVERNANCE
            or logical not in self._cancel_origin_by_logical
        ):
            self._cancel_origin_by_logical[logical] = origin
        self._cancel_requests_by_origin[origin] = (
            self._cancel_requests_by_origin.get(origin, 0) + 1
        )
        self._append(
            AUDIT_EVENT_FILE,
            {
                "record_type": "cancel_requested",
                "order_id": logical,
                "native_order_id": native_order_id,
                "cancel_origin": origin,
            },
        )

    def note_cancel_confirmed(
        self, native_order_id: str, *, origin: str | None = None
    ) -> None:
        logical = self._logical(native_order_id)
        if logical is None:
            return
        native_order_id = str(native_order_id)
        self.bridge.record_cancel(logical)
        # The venue confirmation carries no origin; reuse what the request said.
        resolved = origin or self._cancel_origin_by_native.get(
            native_order_id, CANCEL_ORIGIN_UNKNOWN
        )
        self._cancel_origin_by_native.setdefault(native_order_id, resolved)
        self._append(
            AUDIT_EVENT_FILE,
            {
                "record_type": "cancel_confirmed",
                "order_id": logical,
                "native_order_id": native_order_id,
                "cancel_origin": resolved,
            },
        )

    def cancel_origin(self, native_order_id: str, logical_order_id: str = "") -> str:
        """Origin of the cancellation a fill on this order would be racing."""

        native_order_id = str(native_order_id)
        if native_order_id in self._cancel_origin_by_native:
            return self._cancel_origin_by_native[native_order_id]
        return self._cancel_origin_by_logical.get(logical_order_id, CANCEL_ORIGIN_UNKNOWN)

    # -- execution ---------------------------------------------------------

    def record_fill(
        self,
        trade_data: Mapping[str, Any],
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        *,
        governance_state_at_execution: str | None = None,
        gross_exposure_limit_at_execution: float | None = None,
        net_exposure_limit_at_execution: float | None = None,
    ) -> ExecutionAuditEvent | None:
        """Audit one native fill.  Never raises into the simulator callback."""

        native_order_id = str(trade_data.get("order_id", ""))
        fill_key, synthetic_key = message_fill_key(trade_data)
        logical = self._logical(native_order_id)
        raw = {
            "record_type": "native_trade_execution",
            "order_id": logical or "",
            "native_order_id": native_order_id,
            "fill_key": fill_key,
            "fill_key_synthetic": synthetic_key,
            "trade_data": dict(trade_data),
            "portfolio_before": dict(before),
            "portfolio_after": dict(after),
        }
        self._append(RAW_EVENT_FILE, raw)
        self._raw_count += 1

        if fill_key in self._seen_fill_keys:
            self._duplicates += 1
            self._append(
                AUDIT_EVENT_FILE,
                {
                    "record_type": "duplicate_execution_message_ignored",
                    "order_id": logical or "",
                    "fill_key": fill_key,
                },
            )
            return None
        self._seen_fill_keys.add(fill_key)

        if logical is None:
            self._unattributed += 1
            self._append(
                AUDIT_EVENT_FILE,
                {
                    "record_type": "unattributed_execution",
                    "native_order_id": native_order_id,
                    "fill_key": fill_key,
                },
            )
            if self.strict_on_unattributed:
                self._fail(
                    REASON_REGISTRATION_FAILED,
                    "fill on an order that was never authorised through the gate",
                    fill_key=fill_key,
                )
            return None

        meta = self._orders[logical]
        instrument = meta["instrument"]
        try:
            pre_long, pre_short = positions_from_snapshot(before, instrument)
            post_long, post_short = positions_from_snapshot(after, instrument)
        except (KeyError, TypeError, ValueError) as exc:
            self._fail(
                REASON_UNKNOWN_INSTRUMENT,
                f"{instrument} missing from portfolio snapshot: {exc}",
                order_id=logical,
                fill_key=fill_key,
            )
            return None

        try:
            quantity = fill_quantity_from_delta(
                meta["side"], pre_long, pre_short, post_long, post_short
            )
        except ValueError as exc:
            self._fail(
                REASON_POSITION_DELTA_INCONSISTENT,
                str(exc),
                order_id=logical,
                fill_key=fill_key,
            )
            return None

        if quantity == 0:
            self._no_change += 1
            self._append(
                AUDIT_EVENT_FILE,
                {
                    "record_type": "execution_message_without_position_change",
                    "order_id": logical,
                    "fill_key": fill_key,
                    "message_quantity": trade_data.get("quantity"),
                },
            )
            return None

        try:
            event = self.bridge.record_execution(
                logical,
                fill_quantity=quantity,
                execution_price=self._price(trade_data),
                pre_long_position=pre_long,
                pre_short_position=pre_short,
                post_long_position=post_long,
                post_short_position=post_short,
                governance_state_at_execution=governance_state_at_execution,
                gross_exposure_limit_at_execution=(
                    gross_exposure_limit_at_execution
                ),
                net_exposure_limit_at_execution=net_exposure_limit_at_execution,
            )
        except ValueError as exc:
            self._fail(
                REASON_POSITION_MISMATCH, str(exc), order_id=logical, fill_key=fill_key
            )
            return None

        self._audit_events.append(event)
        raced_origin = ""
        if event.cancel_execute_race:
            raced_origin = self.cancel_origin(native_order_id, logical)
            self._race_fills += 1
            if raced_origin == CANCEL_ORIGIN_GOVERNANCE:
                self._governance_race_fills += 1
                if event.executed_risk.actual_net_risk_increase == 1:
                    self._governance_race_opening_fills += 1
        self._append(
            AUDIT_EVENT_FILE,
            {
                "record_type": "execution_audit",
                "fill_key": fill_key,
                "fill_key_synthetic": synthetic_key,
                "native_order_id": native_order_id,
                "message_quantity": trade_data.get("quantity"),
                "allowed_quantity": meta["allowed_quantity"],
                "raced_cancel_origin": raced_origin,
                "event": event.to_dict(),
            },
        )
        return event

    @staticmethod
    def _price(trade_data: Mapping[str, Any]) -> float | None:
        value = trade_data.get("price")
        try:
            price = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    def _logical(self, native_order_id: str) -> str | None:
        native_order_id = str(native_order_id)
        if native_order_id in self._native_to_logical:
            return self._native_to_logical[native_order_id]
        if native_order_id in self._orders:
            return native_order_id
        return None

    # -- closing -----------------------------------------------------------

    def events(self) -> tuple[ExecutionAuditEvent, ...]:
        return tuple(self._audit_events)

    def close(self, *, strict: bool = False) -> AuditIntegrityReport:
        """Flush, hash, and certify (or refuse to certify) this agent's audit."""

        if self._closed:
            return self._report()
        self._closed = True
        if not self.audit_valid:
            self._write_validation_artifact()
        report = self._report()
        try:
            (self.audit_dir / f"audit_integrity_{self.agent_id}.json").write_text(
                json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8"
            )
        except OSError as exc:
            print(
                json.dumps(
                    {
                        "governance_audit_integrity_write_failure": str(exc),
                        "report": report.to_dict(),
                    },
                    default=str,
                ),
                file=sys.stderr,
                flush=True,
            )
            report.audit_valid = False
            report.eligible_for_effect_analysis = False
        if strict and not report.audit_valid:
            raise AuditInvalidError(
                f"run {self.run_id} agent {self.agent_id} audit is invalid: "
                f"{[failure['reason'] for failure in report.failures]}"
            )
        return report

    def _write_validation_artifact(self) -> None:
        payload = {
            "binding_version": AUDIT_BINDING_VERSION,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "mechanism": self.mechanism,
            "status": "audit_invalid",
            "eligible_for_effect_analysis": False,
            "failures": [failure.to_dict() for failure in self.failures],
        }
        try:
            (self.audit_dir / f"audit_validation_failures_{self.agent_id}.json").write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8"
            )
        except OSError as exc:
            print(
                json.dumps({"governance_audit_validation_write_failure": str(exc), **payload}),
                file=sys.stderr,
                flush=True,
            )

    def _report(self) -> AuditIntegrityReport:
        complete = self._unattributed == 0
        return AuditIntegrityReport(
            binding_version=AUDIT_BINDING_VERSION,
            run_id=self.run_id,
            agent_id=self.agent_id,
            mechanism=self.mechanism,
            governed=True,
            audit_valid=self.audit_valid,
            audit_complete=complete,
            eligible_for_effect_analysis=self.audit_valid and complete,
            registered_order_count=len(self._orders),
            raw_event_count=self._raw_count,
            audit_event_count=len(self._audit_events),
            duplicate_message_count=self._duplicates,
            no_position_change_count=self._no_change,
            unattributed_fill_count=self._unattributed,
            cancel_requests_by_origin=dict(self._cancel_requests_by_origin),
            race_fill_count=self._race_fills,
            governance_race_fill_count=self._governance_race_fills,
            governance_race_fill_opening_count=self._governance_race_opening_fills,
            executed_quantity_total=sum(
                event.fill_quantity for event in self._audit_events
            ),
            authorized_quantity_total=sum(
                meta["allowed_quantity"] for meta in self._orders.values()
            ),
            proposed_quantity_total=sum(
                meta["proposed_quantity"] for meta in self._orders.values()
            ),
            severe_state_executed_net_risk_increase=sum(
                int(event.severe_state_executed_net_risk_increase)
                for event in self._audit_events
            ),
            execution_exceeds_authorized=sum(
                int(event.execution_exceeds_authorized) for event in self._audit_events
            ),
            realized_opening_exceeds_authorized=sum(
                int(event.realized_opening_exceeds_authorized)
                for event in self._audit_events
            ),
            realized_opening_exceeds_authorized_order_count=sum(
                int(event.realized_opening_exceeds_authorized_first_observation)
                for event in self._audit_events
            ),
            severe_state_realized_opening_exceeds_authorized=sum(
                int(event.severe_state_realized_opening_exceeds_authorized)
                for event in self._audit_events
            ),
            severe_state_gross_exposure_limit_breach=sum(
                int(event.severe_state_gross_exposure_limit_breach)
                for event in self._audit_events
            ),
            severe_state_net_exposure_limit_breach=sum(
                int(event.severe_state_net_exposure_limit_breach)
                for event in self._audit_events
            ),
            severe_state_hard_exposure_limit_breach=sum(
                int(event.severe_state_hard_exposure_limit_breach)
                for event in self._audit_events
            ),
            stop_requested=self.stop_requested,
            failures=[failure.to_dict() for failure in self.failures],
            artifact_sha256=self._artifact_hashes(),
        )

    def _artifact_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for name in (RAW_EVENT_FILE, AUDIT_EVENT_FILE):
            path = self._path(name)
            if path.exists():
                hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes


def build_execution_audit_recorder(
    *,
    enabled: bool,
    run_id: str,
    agent_id: str,
    mechanism: str,
    audit_dir: Path | str,
    strict_on_unattributed: bool = False,
) -> ExecutionAuditRecorder | NullExecutionAuditRecorder:
    """Return a governed recorder, or an explicit no-op when audit is off."""

    if not enabled:
        return NullExecutionAuditRecorder()
    if not run_id or not agent_id:
        # Refuse to guess.  A misconfigured governed run must not quietly write
        # an audit under an anonymous identity.
        return NullExecutionAuditRecorder(reason="EXECUTION_AUDIT_MISCONFIGURED")
    return ExecutionAuditRecorder(
        run_id=run_id,
        agent_id=agent_id,
        mechanism=mechanism,
        audit_dir=audit_dir,
        strict_on_unattributed=strict_on_unattributed,
    )


def recorder_from_env(
    agent_id: str,
    *,
    env: Mapping[str, str] | None = None,
    default_audit_dir: Path | str | None = None,
) -> ExecutionAuditRecorder | NullExecutionAuditRecorder:
    """Build the recorder a launcher configured through the process environment."""

    env = os.environ if env is None else env
    enabled = env.get("GOVERNANCE_EXECUTION_AUDIT", "0") == "1"
    configured = env.get("GOVERNANCE_EXECUTION_AUDIT_DIR", "")
    if configured:
        audit_dir = Path(configured)
    else:
        base = default_audit_dir or Path(env.get("METRICS_OUTPUT_DIR", "metrics"))
        audit_dir = Path(base) / "governance_execution_audit"
    return build_execution_audit_recorder(
        enabled=enabled,
        run_id=env.get("GOVERNANCE_RUN_ID", ""),
        agent_id=agent_id,
        mechanism=env.get("NATIVE_GOVERNANCE_MECHANISM", ""),
        audit_dir=audit_dir,
        strict_on_unattributed=env.get("GOVERNANCE_AUDIT_STRICT", "0") == "1",
    )
