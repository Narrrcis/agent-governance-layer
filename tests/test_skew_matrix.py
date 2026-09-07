from __future__ import annotations

from governance_layer import run_authorization_execution_skew_matrix


def _rows(severe_state: str = "RESTRICTED"):
    return {
        (row.case_id, row.governance_state): row
        for row in run_authorization_execution_skew_matrix(
            severe_state=severe_state
        )
    }


def test_skew_matrix_separates_four_scenarios_from_acceptance_properties() -> None:
    rows = _rows()
    assert set(rows) == {
        (case, state)
        for case in ("S0", "S1", "S2", "S3")
        for state in ("NORMAL", "RESTRICTED")
    }
    assert all(row.actual_uses_execution_book for row in rows.values())
    assert not any(row.silent_authorization_substitution for row in rows.values())
    assert not any(row.risk_reduction_blocked for row in rows.values())


def test_s0_is_a_negative_control_and_exposes_strict_severe_state_cost() -> None:
    rows = _rows()
    normal = rows["S0", "NORMAL"]
    severe = rows["S0", "RESTRICTED"]
    assert normal.authorized_opening_quantity == normal.actual_opening_quantity == 20
    assert not normal.realized_opening_exceeds_authorized
    assert normal.guard_action == "KEEP"
    assert severe.guard_action == "CANCEL"
    assert severe.unnecessary_intervention
    assert severe.hard_boundary_compliant_if_guard_honored


def test_s1_would_cross_severe_gross_limit_and_selective_guard_cancels_it() -> None:
    rows = _rows()
    normal = rows["S1", "NORMAL"]
    severe = rows["S1", "RESTRICTED"]
    assert not normal.gross_limit_breach_if_executed
    assert normal.guard_action == "KEEP"
    assert severe.gross_limit_breach_if_executed
    assert severe.guard_action == "CANCEL"
    assert severe.hard_boundary_compliant_if_guard_honored
    assert not severe.hard_boundary_compliant_under_cancel_execute_race


def test_s2_close_target_disappearance_is_native_no_op_not_reverse_opening() -> None:
    rows = _rows()
    for state in ("NORMAL", "RESTRICTED"):
        row = rows["S2", state]
        assert row.actual_executed_quantity == 0
        assert row.actual_opening_quantity == 0
        assert row.guard_action == "KEEP"
        assert row.guard_reason == "NATIVE_CLOSE_TARGET_ABSENT"


def test_s3_close_target_shrink_is_safe_underfill_in_both_states() -> None:
    rows = _rows()
    for state in ("NORMAL", "RESTRICTED"):
        row = rows["S3", state]
        assert row.actual_executed_quantity == 5
        assert row.actual_close_quantity == 5
        assert row.actual_opening_quantity == 0
        assert row.guard_action == "KEEP"


def test_isolated_matrix_keeps_close_semantics_and_cancels_opening_legs() -> None:
    rows = _rows("ISOLATED")
    assert rows["S0", "ISOLATED"].guard_action == "CANCEL"
    assert rows["S1", "ISOLATED"].guard_action == "CANCEL"
    assert rows["S2", "ISOLATED"].guard_action == "KEEP"
    assert rows["S3", "ISOLATED"].guard_action == "KEEP"
    assert not any(row.risk_reduction_blocked for row in rows.values())
