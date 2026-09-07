from __future__ import annotations

from governance_layer.static_experiment import ARMS, SCENARIOS, run_static_paired_comparison


def test_static_paired_comparison_is_fixed_no_llm_and_covers_required_scenarios() -> None:
    result = run_static_paired_comparison((20261101,))
    assert result["llm_api_used"] is False
    assert set(result["arms"]) == set(ARMS)
    assert set(SCENARIOS) <= set(result["scenario_coverage"])


def test_full_and_lean_have_zero_hard_safety_violations_in_static_replay() -> None:
    result = run_static_paired_comparison((20261101,))
    for arm in ("hybrid_v2_1_full", "hybrid_v2_1_lean"):
        metrics = result["arms"][arm]
        assert metrics["actual_risk_amplification"] == 0
        assert metrics["severe_state_executed_net_risk_increase"] == 0
        assert metrics["risk_reduction_blocked"] == 0
        assert metrics["close_or_cover_blocked"] == 0
        assert metrics["forced_direction_change"] == 0
        assert metrics["scalar_fallback_failure"] == 0
