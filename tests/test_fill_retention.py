"""The corrected retention metric, and what it says about the Shadow arm."""

from __future__ import annotations

import pytest

from governance_layer import FillRetentionAccumulator, run_corrected_retention_comparison
from governance_layer.static_experiment import run_static_paired_comparison


def test_partial_fills_do_not_inflate_retention_above_the_quantity_filled():
    ledger = FillRetentionAccumulator()
    ledger.observe_order("a", proposed_quantity=6, authorized_quantity=6)
    ledger.observe_fill("a", 2)
    ledger.observe_fill("a", 4)
    assert ledger.fill_event_count == 2
    assert ledger.filled_order_count == 1
    assert ledger.fill_retention_rate == 1.0
    assert ledger.filled_order_rate == 1.0


def test_a_structurally_unfillable_order_leaves_the_denominator():
    ledger = FillRetentionAccumulator()
    ledger.observe_order("a", proposed_quantity=4, authorized_quantity=4)
    ledger.observe_order("b", proposed_quantity=4, authorized_quantity=4, venue_eligible=False)
    ledger.observe_fill("a", 4)
    assert ledger.eligible_order_count == 1
    assert ledger.excluded_order_count == 1
    assert ledger.fill_retention_rate == 1.0


def test_an_arm_that_never_reaches_a_venue_reports_undefined_not_zero():
    ledger = FillRetentionAccumulator(venue_routed=False)
    ledger.observe_order("a", proposed_quantity=10, authorized_quantity=6)
    assert ledger.fill_retention_rate is None
    assert ledger.filled_order_rate is None
    assert ledger.execution_conversion_rate is None
    # The counterfactual that an observe-only arm can legitimately report.
    assert ledger.authorized_retention_rate == 0.6


def test_governance_clipping_shows_up_as_reduced_retention():
    ledger = FillRetentionAccumulator()
    ledger.observe_order("a", proposed_quantity=10, authorized_quantity=4)
    ledger.observe_fill("a", 4)
    assert ledger.fill_retention_rate == 0.4
    assert ledger.authorized_retention_rate == 0.4
    assert ledger.execution_conversion_rate == 1.0


def test_a_blocked_order_counts_as_fully_unretained():
    ledger = FillRetentionAccumulator()
    ledger.observe_order("a", proposed_quantity=8, authorized_quantity=0)
    assert ledger.fill_retention_rate == 0.0
    assert ledger.filled_order_rate == 0.0


def test_a_zero_quantity_proposal_is_rejected():
    ledger = FillRetentionAccumulator()
    with pytest.raises(ValueError):
        ledger.observe_order("a", proposed_quantity=0, authorized_quantity=0)
    with pytest.raises(ValueError):
        ledger.observe_fill("a", 0)


def test_the_corrected_comparison_leaves_shadow_retention_undefined():
    result = run_corrected_retention_comparison()
    shadow = result["arms"]["shadow"]
    assert shadow["venue_routed"] is False
    assert shadow["fill_retention_rate"] is None
    assert shadow["executed_quantity"] == 0
    assert shadow["authorized_retention_rate"] > 0


def test_the_corrected_comparison_reports_executing_arms_by_quantity():
    result = run_corrected_retention_comparison()
    for arm in ("scalar", "hybrid_v2_1_full", "hybrid_v2_1_lean"):
        row = result["arms"][arm]
        assert row["venue_routed"] is True
        assert 0.0 < row["fill_retention_rate"] <= 1.0
        assert row["executed_quantity"] == row["authorized_quantity"]
        assert row["fill_event_count"] >= row["eligible_order_count"] * 0
    assert result["arms"]["scalar"]["fill_retention_rate"] > (
        result["arms"]["hybrid_v2_1_full"]["fill_retention_rate"]
    )


def test_the_correction_does_not_touch_the_historical_v1_result():
    historical = run_static_paired_comparison()
    assert historical["experiment"] == "v2_1_lean_static_paired_diagnostic_v1"
    assert historical["arms"]["shadow"]["fill_retention_rate"] == 0.0
    assert historical["arms"]["scalar"]["fill_retention_rate"] == pytest.approx(
        11 / 18, abs=1e-9
    )
    corrected = run_corrected_retention_comparison()
    assert corrected["experiment"] != historical["experiment"]
    assert corrected["scenario_coverage"] == historical["scenario_coverage"]
    assert corrected["seeds"] == historical["seeds"]
