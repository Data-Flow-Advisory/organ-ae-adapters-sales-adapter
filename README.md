# organ-ae-adapters-sales-adapter

A pure, deterministic organ that selects *actionable sales-pipeline deals* and
turns each into a work-item directive for the Sales persona (Tim).

Extracted from discovery-engine
[`app/services/ae_adapters/sales_adapter.py`](https://github.com/Data-Flow-Advisory/discovery-engine)
(always-fed fleet, Task 1.1b).

## Contract

This is a PURE organ per `orchestrator/CONTRACT.md`:

- **Signature**: `decide(state, context) -> {output, rationale, self_metric}`
- **Deterministic**: same input always produces the same output (stable sort).
- **No side effects**: no I/O, no network, no clock, no env reads, no mutation
  of the input. The live adapter calls `sales_pipeline.list_pipeline()` and
  `utcnow_naive()`; this organ consumes the *result* of those — pre-fetched
  rows + a supplied `now` — and decides what to feed.
- **Fail-safe on empty state**: `decide({}, {})` returns a valid structure with
  low confidence; the organ never raises.
- **Stdlib-only**: no external dependencies (Python 3.10+).

## What it decides

Only deals in an **actionable** stage are fed to Tim
(`persona_project_id = 8`):

| Stage | Fed? | Why |
|-------|------|-----|
| `discovery` | ✅ | active engagement, next action available |
| `blueprint` | ✅ | active engagement, next action available |
| `proposal` | ✅ | active engagement, next action available |
| `pilot` | ✅ | active engagement, next action available |
| `lead` | ❌ | too early — no deal context yet |
| `qualified` | ❌ | needs human qualification before automation |
| `retained` | ❌ | deal already won |
| `churned` | ❌ | deal is dead |
| _anything else_ | ❌ | unknown stage |

Actionable deals are ranked **`days_in_stage` DESC, then `deal_value` DESC** —
the longest-stalled, highest-value deals first. Each carries a stable
`correlation_id = f"sales-{tenant_id}-{stage}-{slug}"` (truncated to the 64-char
column cap) for dedup downstream.

## Interface

```python
from organ import decide

result = decide(
    state={
        "pipeline_rows": [
            {
                "tenant_id": 6,
                "tenant_slug": "gliderol",
                "display_name": "Gliderol",
                "pipeline_state": {
                    "stage": "proposal",
                    "stage_changed_at": "2026-05-28T00:00:00",
                    "deal_value_estimate_usd": 12000,
                    "deal_value_currency": "GBP",
                },
            },
        ],
        # optional: override the actionable set
        # "actionable_stages": ["discovery", "blueprint", "proposal", "pilot"],
    },
    context={"now": "2026-06-11T00:00:00"},  # reference time for days_in_stage
)
```

## Output

```json
{
  "output": {
    "candidates": [
      {
        "directive": "Sales — Tim: Pipeline action needed for Gliderol. Stage: proposal. Days in stage: 14. Deal value: £12,000. Review via GET /api/v1/admin/pipeline and take the next concrete action to advance this deal.",
        "persona_project_id": 8,
        "tenant_id": 6,
        "correlation_id": "sales-6-proposal-gliderol",
        "requires_worker_type": null
      }
    ],
    "skipped": []
  },
  "rationale": "Fed 1 actionable deal(s) to Tim (persona_project_id=8), ranked by days-in-stage then deal value; 0 non-actionable row(s) skipped.",
  "self_metric": {
    "confidence": 0.95,
    "rows_considered": 1,
    "candidates_fed": 1,
    "rows_skipped": 0,
    "decision": "feed"
  }
}
```

Each candidate matches the standard always-fed candidate contract:
`{directive, persona_project_id, tenant_id, correlation_id, requires_worker_type}`.

## CLI

The organ doubles as a CLI for orchestrators that shell out:

```bash
python organ.py < samples/two_actionable_ranked.json
# reads {state, context} on stdin (or $ORGAN_INPUT), writes the result to stdout
```

## Testing

```bash
pip install pytest
pytest test_organ.py -v
```

All tests are pure functions with deterministic inputs/outputs.

## Samples

- `samples/two_actionable_ranked.json` — two actionable deals, ranked.
- `samples/all_non_actionable_skip.json` — every deal lead/won/dead → nothing fed.
- `samples/mixed_with_missing_data.json` — actionable deals with missing value /
  `stage_changed_at` / display name, plus one `qualified` deal to skip.

## References

- orchestrator `CONTRACT.md` — pure-organ specification.
- discovery-engine `app/services/ae_adapters/sales_adapter.py` — original source.
- `docs/strategic-review/always-fed-fleet-tasks.md` §1.1b — adapter spec.
