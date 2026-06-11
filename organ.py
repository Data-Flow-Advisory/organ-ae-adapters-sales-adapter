#!/usr/bin/env python3
"""Sales-pipeline source adapter — pure decision organ.

A pure, stdlib-only decision organ extracted from discovery-engine's
``app/services/ae_adapters/sales_adapter.py`` (always-fed Task 1.1b).

CONTRACT (orchestrator pure-organ protocol — orchestrator/CONTRACT.md)
----------------------------------------------------------------------
    decide(state, context) -> {"output", "rationale", "self_metric"}

  * Pure: no DB, no network, no filesystem, no env reads, no clock — every
    input arrives via ``state`` / ``context``. (The live adapter calls
    ``sales_pipeline.list_pipeline()`` and ``utcnow_naive()``; this organ
    consumes the *result* of those — pre-fetched pipeline rows + a supplied
    ``now`` — and decides what to feed.)
  * Deterministic: same (state, context) always yields the same result.
  * Fail-safe: never raises. Bad/empty input returns a valid structure with a
    low ``confidence`` and an explanatory ``rationale``.
  * Stdlib-only: imports nothing outside the Python standard library.
  * ``self_metric.confidence`` is a float in ``[0.0, 1.0]``.

WHAT THIS ORGAN DECIDES
-----------------------
Which pipeline deals are *actionable* and therefore should be fed to the
Sales persona (Tim, ``persona_project_id=8``) as work-item directives.

Faithful to the live wire's invariants:

  * Only deals in an **actionable** stage are fed: ``discovery``, ``blueprint``,
    ``proposal``, ``pilot``. ``lead`` (too early), ``qualified`` (needs a human
    first), ``retained`` (won), ``churned`` (dead), and any unknown stage are
    excluded.
  * Each actionable deal becomes one candidate directive with a stable
    ``correlation_id`` (``f"sales-{tenant_id}-{stage}-{slug}"``) for dedup.
  * Candidates are ranked ``days_in_stage`` DESC, then ``deal_value`` DESC —
    the longest-stalled, highest-value deals first.

The CLI (``python organ.py < input.json``) reads ``{state, context}`` on
stdin (or ``$ORGAN_INPUT``) and writes ``{output, rationale, self_metric}`` to
stdout, so the orchestrator can shell out to it like any other organ.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

# Stages where Tim can take a meaningful next action. Mirrors the live wire's
# ``ACTIONABLE_STAGES`` frozenset exactly.
#   "lead"      excluded — too early, no deal context yet.
#   "qualified" excluded — needs human qualification before automation.
#   "retained"  excluded — deal already won, nothing to advance.
#   "churned"   excluded — deal is dead, nothing to advance.
# Any other / unknown stage string is also excluded.
ACTIONABLE_STAGES = frozenset({
    "discovery",
    "blueprint",
    "proposal",
    "pilot",
})

# Tim's persona_project_id on the DataFlow Advisory tenant.
TIM_PERSONA_PROJECT_ID = 8

# correlation_id column cap on LocalCronWorkItem.
_CORRELATION_MAX_LEN = 64


# ---------------------------------------------------------------------------
# Pure helpers — no IO, deterministic, testable in isolation.
# ---------------------------------------------------------------------------

def days_in_stage(stage_changed_at: Any, now: Any) -> int:
    """Whole days from ``stage_changed_at`` to ``now`` (both naive-UTC ISO).

    Faithful port of the live adapter's ``_days_in_stage`` — but ``now`` is
    supplied (the live wire reads ``utcnow_naive()``; a pure organ cannot read
    the clock). Returns 0 when either timestamp is missing or unparseable, and
    never returns a negative value. A missing ``stage_changed_at`` is treated
    as "just entered stage", not an error.
    """
    if not stage_changed_at:
        return 0
    try:
        changed = datetime.fromisoformat(str(stage_changed_at))
        now_dt = datetime.fromisoformat(str(now)) if now else None
        if now_dt is None:
            return 0
        delta = now_dt - changed
        return max(0, delta.days)
    except Exception:
        return 0


def format_value(deal_value: Any, currency: str = "GBP") -> str:
    """Format a deal value for the directive text.

    Faithful port of ``_format_value``: currency symbol (£ for GBP/blank),
    rounded to the nearest integer with thousands separators. Returns
    ``"unknown"`` when no parseable value is recorded.
    """
    if deal_value is None:
        return "unknown"
    try:
        v = float(deal_value)
    except (TypeError, ValueError):
        return "unknown"
    symbol = "£" if str(currency).upper() in ("GBP", "") else str(currency)
    return f"{symbol}{int(round(v)):,}"


def build_directive(display_name: str, stage: str, days: int,
                    deal_value: Any, currency: str) -> str:
    """Compose the directive text for one actionable deal.

    Byte-for-byte identical template to the live adapter's ``_build_directive``.
    """
    value_str = format_value(deal_value, currency)
    return (
        f"Sales — Tim: Pipeline action needed for {display_name}. Stage: {stage}. "
        f"Days in stage: {days}. Deal value: {value_str}. Review via "
        "GET /api/v1/admin/pipeline and take the next concrete action to advance this deal."
    )


def deal_correlation_id(tenant_id: Any, stage: Any, slug: Any) -> str:
    """Derive the dedup ``correlation_id`` for a pipeline deal.

    ``f"sales-{tenant_id}-{stage}-{slug}"`` — identical to the live wire —
    truncated to the 64-char column cap so an over-long slug can never mint a
    key the column would reject.
    """
    cid = f"sales-{tenant_id}-{stage}-{slug or ''}"
    return cid[:_CORRELATION_MAX_LEN]


# ---------------------------------------------------------------------------
# The decision — the pure organ entry point.
# ---------------------------------------------------------------------------

def _empty_report() -> Dict[str, Any]:
    return {
        "output": {"candidates": [], "skipped": []},
        "rationale": "",
        "self_metric": {
            "confidence": 0.0,
            "rows_considered": 0,
            "candidates_fed": 0,
            "rows_skipped": 0,
            "decision": "hold",
        },
    }


def decide(state: Optional[Dict[str, Any]],
           context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Decide which pipeline deals to feed to the Sales persona.

    The canonical pure-organ contract function: ``decide(state, context)``.

    ``state`` keys (all optional; defensive defaults applied):
      - ``pipeline_rows`` (list) — pre-fetched pipeline rows, each shaped like
        ``sales_pipeline.list_pipeline()`` output::

            {
              "tenant_id":    int | None,
              "tenant_slug":  str,
              "display_name": str,
              "pipeline_state": {
                "stage":                  str,
                "stage_changed_at":       ISO str | None,
                "deal_value_estimate_usd": float | None,
                "deal_value_currency":    str | None,
              },
            }

      - ``actionable_stages`` (list[str]) — override the actionable set;
        defaults to ``ACTIONABLE_STAGES``.

    ``context`` keys (all optional):
      - ``now`` (ISO str) — the reference time for ``days_in_stage``. When
        absent every deal reports ``days_in_stage=0`` (ranking then collapses
        to deal-value DESC).

    Returns ``{output, rationale, self_metric}``. Never raises. ``output``
    carries:
      - ``candidates`` — ranked list of directive dicts, each
        ``{directive, persona_project_id, tenant_id, correlation_id,
        requires_worker_type}`` (the standard always-fed candidate contract).
      - ``skipped`` — ``{tenant_id, stage, reason}`` for each non-actionable
        row, so the caller can see what was held and why.
    """
    report = _empty_report()
    try:
        if not isinstance(state, dict):
            state = {}
        if not isinstance(context, dict):
            context = {}

        rows = state.get("pipeline_rows", [])
        if not isinstance(rows, list):
            rows = []

        raw_stages = state.get("actionable_stages")
        if isinstance(raw_stages, (list, set, frozenset, tuple)):
            actionable = frozenset(str(s) for s in raw_stages)
        else:
            actionable = ACTIONABLE_STAGES

        now = context.get("now")

        candidates: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        for row in rows:
            if not isinstance(row, dict):
                skipped.append({"tenant_id": None, "stage": None, "reason": "malformed_row"})
                continue

            pstate = row.get("pipeline_state")
            if not isinstance(pstate, dict):
                pstate = {}
            stage = pstate.get("stage", "lead")

            tenant_id = row.get("tenant_id")

            if stage not in actionable:
                skipped.append({
                    "tenant_id": tenant_id,
                    "stage": stage,
                    "reason": "stage_not_actionable",
                })
                continue

            slug = row.get("tenant_slug") or ""
            display_name = row.get("display_name") or slug or f"tenant-{tenant_id}"

            days = days_in_stage(pstate.get("stage_changed_at"), now)
            deal_value = pstate.get("deal_value_estimate_usd")
            currency = pstate.get("deal_value_currency") or "GBP"

            directive = build_directive(display_name, stage, days, deal_value, currency)
            correlation_id = deal_correlation_id(tenant_id, stage, slug)

            candidates.append({
                "directive": directive,
                "persona_project_id": TIM_PERSONA_PROJECT_ID,
                "tenant_id": tenant_id,
                "correlation_id": correlation_id,
                "requires_worker_type": None,
                # Internal sort keys — stripped before returning.
                "_days_in_stage": days,
                "_deal_value": float(deal_value) if isinstance(deal_value, (int, float)) and not isinstance(deal_value, bool) else 0.0,
            })

        # Rank: days_in_stage DESC, then deal_value DESC. ``sorted`` is stable,
        # so deals tying on both keys keep their input order — deterministic.
        candidates.sort(key=lambda c: (-c["_days_in_stage"], -c["_deal_value"]))

        for c in candidates:
            c.pop("_days_in_stage", None)
            c.pop("_deal_value", None)

        report["output"]["candidates"] = candidates
        report["output"]["skipped"] = skipped
        report["self_metric"]["rows_considered"] = len(rows)
        report["self_metric"]["candidates_fed"] = len(candidates)
        report["self_metric"]["rows_skipped"] = len(skipped)

        if not rows:
            report["rationale"] = "No pipeline rows supplied; nothing to feed."
            report["self_metric"].update({"confidence": 0.9, "decision": "hold_no_rows"})
        elif candidates:
            report["rationale"] = (
                f"Fed {len(candidates)} actionable deal(s) to Tim "
                f"(persona_project_id={TIM_PERSONA_PROJECT_ID}), ranked by days-in-stage "
                f"then deal value; {len(skipped)} non-actionable row(s) skipped."
            )
            report["self_metric"].update({"confidence": 0.95, "decision": "feed"})
        else:
            report["rationale"] = (
                f"No actionable deals among {len(rows)} pipeline row(s); all in "
                "non-actionable stages (lead/qualified/retained/churned/unknown)."
            )
            report["self_metric"].update({"confidence": 0.9, "decision": "skip_none_actionable"})

        return report

    except Exception as exc:  # fail-safe — a broken decision must never raise.
        return {
            "output": {"candidates": [], "skipped": [], "skipped_reason": "error_fail_open"},
            "rationale": f"decide() failed open: {exc}",
            "self_metric": {
                "confidence": 0.0,
                "rows_considered": 0,
                "candidates_fed": 0,
                "rows_skipped": 0,
                "decision": "error",
                "error": str(exc),
            },
        }


# Backward-compatible alias mirroring the live adapter's verb.
get_candidates = decide


# ---------------------------------------------------------------------------
# CLI adapter — stdin JSON in, stdout JSON out. Not part of the pure contract.
# ---------------------------------------------------------------------------

def _read_input() -> Dict[str, Any]:
    text = sys.stdin.read()
    if not text.strip():
        import os
        text = os.getenv("ORGAN_INPUT", "{}")
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def main() -> int:
    try:
        data = _read_input()
        result = decide(data.get("state") or {}, data.get("context") or {})
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        json.dump({
            "output": {"candidates": [], "skipped": [], "skipped_reason": "error_fail_open"},
            "rationale": f"CLI fatal error: {exc}",
            "self_metric": {"confidence": 0.0, "decision": "error", "error": str(exc)},
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
