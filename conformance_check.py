#!/usr/bin/env python3
"""Contract checker for the sales-adapter organ.

Reads an organ result (the JSON that ``organ.py`` writes to stdout) from this
process's stdin and asserts it satisfies the orchestrator CONTRACT.md shape:
a top-level object carrying ``output``, ``rationale``, and a ``self_metric``
dict with a ``confidence`` key in ``[0.0, 1.0]``. Exits non-zero with a clear
message on any violation, so the conformance workflow goes RED if the organ
ever drifts from the contract.

Usage (in CI):
    python organ.py < sample.json | python conformance_check.py sample-name
"""
from __future__ import annotations

import json
import sys

_REQUIRED_TOP_KEYS = ("output", "rationale", "self_metric")


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "<stdin>"
    try:
        result = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"contract violation [{label}]: organ output is not valid JSON ({exc})")
        return 1

    if not isinstance(result, dict):
        print(f"contract violation [{label}]: top-level output is not a JSON object")
        return 1

    for key in _REQUIRED_TOP_KEYS:
        if key not in result:
            print(f"contract violation [{label}]: missing top-level key {key!r}")
            return 1

    self_metric = result.get("self_metric")
    if not isinstance(self_metric, dict) or "confidence" not in self_metric:
        print(f"contract violation [{label}]: self_metric.confidence is required")
        return 1

    conf = self_metric["confidence"]
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not (0.0 <= conf <= 1.0):
        print(f"contract violation [{label}]: confidence {conf!r} not in [0.0, 1.0]")
        return 1

    if not isinstance(result["output"].get("candidates"), list):
        print(f"contract violation [{label}]: output.candidates must be a list")
        return 1

    print(f"  contract OK: {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
