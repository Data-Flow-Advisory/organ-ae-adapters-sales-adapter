#!/usr/bin/env python3
"""Tests for the sales-pipeline source organ.

Pure-function tests — deterministic inputs/outputs, no IO, no network.
Run with: ``pytest test_organ.py -v``
"""
from __future__ import annotations

import json
import subprocess
import sys

import organ
from organ import (
    ACTIONABLE_STAGES,
    TIM_PERSONA_PROJECT_ID,
    build_directive,
    days_in_stage,
    deal_correlation_id,
    decide,
    format_value,
)


# ---------------------------------------------------------------------------
# Helpers to build pipeline rows.
# ---------------------------------------------------------------------------

def _row(tenant_id, slug, name, stage, changed=None, value=None, currency=None):
    return {
        "tenant_id": tenant_id,
        "tenant_slug": slug,
        "display_name": name,
        "pipeline_state": {
            "stage": stage,
            "stage_changed_at": changed,
            "deal_value_estimate_usd": value,
            "deal_value_currency": currency,
        },
    }


NOW = "2026-06-11T00:00:00"


# ---------------------------------------------------------------------------
# Contract shape.
# ---------------------------------------------------------------------------

def test_contract_shape():
    r = decide({}, {})
    assert set(r) == {"output", "rationale", "self_metric"}
    assert isinstance(r["output"], dict)
    assert isinstance(r["rationale"], str)
    assert "confidence" in r["self_metric"]


def test_empty_state_failsafe():
    r = decide({}, {})
    assert r["output"]["candidates"] == []
    c = r["self_metric"]["confidence"]
    assert 0.0 <= c <= 1.0
    assert r["self_metric"]["decision"] == "hold_no_rows"


def test_none_state_failsafe():
    r = decide(None, None)
    assert r["output"]["candidates"] == []
    assert 0.0 <= r["self_metric"]["confidence"] <= 1.0


def test_signature_is_state_context():
    import inspect
    params = list(inspect.signature(decide).parameters.keys())
    assert params[:2] == ["state", "context"]


# ---------------------------------------------------------------------------
# days_in_stage helper.
# ---------------------------------------------------------------------------

def test_days_in_stage_basic():
    assert days_in_stage("2026-06-01T00:00:00", "2026-06-11T00:00:00") == 10


def test_days_in_stage_missing_changed_returns_zero():
    assert days_in_stage(None, NOW) == 0
    assert days_in_stage("", NOW) == 0


def test_days_in_stage_missing_now_returns_zero():
    assert days_in_stage("2026-06-01T00:00:00", None) == 0


def test_days_in_stage_unparseable_returns_zero():
    assert days_in_stage("not-a-date", NOW) == 0


def test_days_in_stage_never_negative():
    # changed in the future relative to now -> clamped to 0, never negative.
    assert days_in_stage("2026-06-20T00:00:00", NOW) == 0


# ---------------------------------------------------------------------------
# format_value helper.
# ---------------------------------------------------------------------------

def test_format_value_gbp():
    assert format_value(2500, "GBP") == "£2,500"


def test_format_value_blank_currency_defaults_pound():
    assert format_value(1000, "") == "£1,000"


def test_format_value_other_currency():
    assert format_value(5000, "USD") == "USD5,000"


def test_format_value_none_is_unknown():
    assert format_value(None) == "unknown"


def test_format_value_garbage_is_unknown():
    assert format_value("abc") == "unknown"


def test_format_value_rounds():
    assert format_value(2499.6, "GBP") == "£2,500"


# ---------------------------------------------------------------------------
# correlation_id helper.
# ---------------------------------------------------------------------------

def test_correlation_id_shape():
    assert deal_correlation_id(6, "proposal", "acme") == "sales-6-proposal-acme"


def test_correlation_id_truncated_to_cap():
    cid = deal_correlation_id(6, "proposal", "x" * 200)
    assert len(cid) <= 64


def test_correlation_id_none_slug():
    assert deal_correlation_id(6, "proposal", None) == "sales-6-proposal-"


# ---------------------------------------------------------------------------
# build_directive helper.
# ---------------------------------------------------------------------------

def test_build_directive_template():
    d = build_directive("Acme Ltd", "proposal", 12, 2500, "GBP")
    assert d == (
        "Sales — Tim: Pipeline action needed for Acme Ltd. Stage: proposal. "
        "Days in stage: 12. Deal value: £2,500. Review via "
        "GET /api/v1/admin/pipeline and take the next concrete action to advance this deal."
    )


# ---------------------------------------------------------------------------
# Stage filtering.
# ---------------------------------------------------------------------------

def test_actionable_stages_fed():
    rows = [_row(i, f"t{i}", f"T{i}", stage, NOW, 1000)
            for i, stage in enumerate(sorted(ACTIONABLE_STAGES), start=1)]
    r = decide({"pipeline_rows": rows}, {"now": NOW})
    assert r["self_metric"]["candidates_fed"] == len(ACTIONABLE_STAGES)
    assert all(c["persona_project_id"] == TIM_PERSONA_PROJECT_ID
               for c in r["output"]["candidates"])


def test_non_actionable_stages_skipped():
    for stage in ("lead", "qualified", "retained", "churned", "weird-unknown"):
        r = decide({"pipeline_rows": [_row(1, "t", "T", stage, NOW, 1000)]}, {"now": NOW})
        assert r["output"]["candidates"] == [], stage
        assert r["self_metric"]["rows_skipped"] == 1
        assert r["output"]["skipped"][0]["reason"] == "stage_not_actionable"


def test_default_stage_is_lead_and_skipped():
    # Row with no stage key in pipeline_state -> defaults to "lead" -> skipped.
    row = {"tenant_id": 1, "tenant_slug": "t", "display_name": "T", "pipeline_state": {}}
    r = decide({"pipeline_rows": [row]}, {"now": NOW})
    assert r["output"]["candidates"] == []
    assert r["output"]["skipped"][0]["stage"] == "lead"


def test_mixed_actionable_and_not():
    rows = [
        _row(1, "a", "Alpha", "proposal", NOW, 5000),
        _row(2, "b", "Beta", "lead", NOW, 9999),
        _row(3, "c", "Gamma", "pilot", NOW, 3000),
        _row(4, "d", "Delta", "churned", NOW, 1),
    ]
    r = decide({"pipeline_rows": rows}, {"now": NOW})
    assert r["self_metric"]["candidates_fed"] == 2
    assert r["self_metric"]["rows_skipped"] == 2


# ---------------------------------------------------------------------------
# Ranking.
# ---------------------------------------------------------------------------

def test_rank_days_desc_then_value_desc():
    rows = [
        _row(1, "a", "Alpha", "proposal", "2026-06-10T00:00:00", 1000),   # 1 day
        _row(2, "b", "Beta", "discovery", "2026-06-01T00:00:00", 500),    # 10 days
        _row(3, "c", "Gamma", "blueprint", "2026-06-01T00:00:00", 9000),  # 10 days, higher value
    ]
    r = decide({"pipeline_rows": rows}, {"now": NOW})
    order = [c["tenant_id"] for c in r["output"]["candidates"]]
    # 10-day deals first (Gamma before Beta on value), then 1-day Alpha.
    assert order == [3, 2, 1]


def test_rank_value_only_when_no_now():
    # Without `now`, days collapses to 0 for everyone -> rank by value DESC.
    rows = [
        _row(1, "a", "Alpha", "proposal", "2026-06-01T00:00:00", 1000),
        _row(2, "b", "Beta", "proposal", "2026-05-01T00:00:00", 8000),
    ]
    r = decide({"pipeline_rows": rows}, {})
    order = [c["tenant_id"] for c in r["output"]["candidates"]]
    assert order == [2, 1]


# ---------------------------------------------------------------------------
# Candidate contract.
# ---------------------------------------------------------------------------

def test_candidate_keys_and_no_internal_sort_keys():
    r = decide({"pipeline_rows": [_row(6, "acme", "Acme", "proposal", NOW, 2500)]}, {"now": NOW})
    c = r["output"]["candidates"][0]
    assert set(c) == {"directive", "persona_project_id", "tenant_id",
                      "correlation_id", "requires_worker_type"}
    assert "_days_in_stage" not in c
    assert "_deal_value" not in c
    assert c["correlation_id"] == "sales-6-proposal-acme"
    assert c["requires_worker_type"] is None


def test_display_name_fallback_to_slug_then_tenant():
    rows = [
        {"tenant_id": 7, "tenant_slug": "slugonly", "display_name": None,
         "pipeline_state": {"stage": "pilot"}},
        {"tenant_id": 8, "tenant_slug": "", "display_name": "",
         "pipeline_state": {"stage": "pilot"}},
    ]
    r = decide({"pipeline_rows": rows}, {"now": NOW})
    directives = [c["directive"] for c in r["output"]["candidates"]]
    assert any("slugonly" in d for d in directives)
    assert any("tenant-8" in d for d in directives)


# ---------------------------------------------------------------------------
# Robustness / fail-safe.
# ---------------------------------------------------------------------------

def test_malformed_row_skipped_not_raised():
    r = decide({"pipeline_rows": ["not-a-dict", 42, None]}, {"now": NOW})
    assert r["output"]["candidates"] == []
    assert r["self_metric"]["rows_skipped"] == 3
    assert all(s["reason"] == "malformed_row" for s in r["output"]["skipped"])


def test_pipeline_rows_not_a_list_is_safe():
    r = decide({"pipeline_rows": "oops"}, {"now": NOW})
    assert r["output"]["candidates"] == []


def test_actionable_stages_override():
    rows = [_row(1, "a", "Alpha", "lead", NOW, 1000)]
    r = decide({"pipeline_rows": rows, "actionable_stages": ["lead"]}, {"now": NOW})
    assert r["self_metric"]["candidates_fed"] == 1


def test_bool_deal_value_not_treated_as_number():
    # True is an int subclass; must not rank as deal_value 1.0 / sort weirdly.
    rows = [_row(1, "a", "Alpha", "proposal", NOW, True)]
    r = decide({"pipeline_rows": rows}, {"now": NOW})
    # format_value(True) -> True is numeric to float(); but ranking weight is 0.
    assert r["self_metric"]["candidates_fed"] == 1


# ---------------------------------------------------------------------------
# Determinism.
# ---------------------------------------------------------------------------

def test_determinism():
    rows = [
        _row(1, "a", "Alpha", "proposal", "2026-06-01T00:00:00", 1000),
        _row(2, "b", "Beta", "discovery", "2026-06-05T00:00:00", 2000),
        _row(3, "c", "Gamma", "lead", NOW, 3000),
    ]
    state = {"pipeline_rows": rows}
    ctx = {"now": NOW}
    assert decide(state, ctx) == decide(state, ctx)


def test_no_input_mutation():
    rows = [_row(1, "a", "Alpha", "proposal", NOW, 1000)]
    state = {"pipeline_rows": rows}
    import copy
    before = copy.deepcopy(state)
    decide(state, {"now": NOW})
    assert state == before


# ---------------------------------------------------------------------------
# CLI adapter.
# ---------------------------------------------------------------------------

def test_cli_stdin_roundtrip():
    payload = json.dumps({
        "state": {"pipeline_rows": [_row(6, "acme", "Acme", "proposal", NOW, 2500)]},
        "context": {"now": NOW},
    })
    proc = subprocess.run(
        [sys.executable, "organ.py"],
        input=payload, capture_output=True, text=True,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert set(out) == {"output", "rationale", "self_metric"}
    assert out["self_metric"]["candidates_fed"] == 1


def test_get_candidates_alias():
    assert organ.get_candidates is organ.decide
