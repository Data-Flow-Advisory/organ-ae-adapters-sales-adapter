#!/usr/bin/env python3
"""Connection-standard port checker for the sales-adapter organ.

Implements the port half of the conformance Action mandated by
orchestrator/CONNECTORS.md ("Conformance gains a port check"):

    The conformance Action additionally asserts: `ports.json` parses; every
    `type` exists in `types.json`; and the organ's `decide` actually reads
    each declared input name and writes each declared output name (sampled
    against the organ's own samples).

This script exits non-zero with a clear message on any violation, so the
conformance workflow goes RED if the ports manifest ever drifts from the
organ's real wiring or references a type outside the shared vocabulary.

Run from the repo root:

    python ports_check.py

It is self-contained (no args): it loads ``ports.json`` + ``types.json`` from
the repo root, imports ``organ.decide``, and exercises it against every file
in ``samples/``.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
# Make `import organ` work regardless of the process's cwd.
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _fail(msg: str) -> None:
    print(f"port conformance violation: {msg}")
    raise SystemExit(1)


def _load_json(path: str) -> Any:
    full = os.path.join(_HERE, path)
    if not os.path.exists(full):
        _fail(f"required file {path!r} is missing")
    try:
        with open(full, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        _fail(f"{path!r} is not valid JSON ({exc})")


def _validate_port_list(ports: Any, kind: str, *, need_required: bool) -> List[Dict[str, Any]]:
    if not isinstance(ports, list):
        _fail(f"ports.json {kind!r} must be a JSON array")
    out: List[Dict[str, Any]] = []
    for i, p in enumerate(ports):
        if not isinstance(p, dict):
            _fail(f"ports.json {kind}[{i}] must be an object")
        name = p.get("name")
        ptype = p.get("type")
        if not isinstance(name, str) or not name:
            _fail(f"ports.json {kind}[{i}] needs a non-empty string 'name'")
        if not isinstance(ptype, str) or not ptype:
            _fail(f"ports.json {kind}[{i}] ({name!r}) needs a non-empty string 'type'")
        if need_required and "required" in p and not isinstance(p["required"], bool):
            _fail(f"ports.json {kind}[{i}] ({name!r}) 'required' must be a bool")
        out.append(p)
    return out


def _load_samples() -> List[Dict[str, Any]]:
    sdir = os.path.join(_HERE, "samples")
    if not os.path.isdir(sdir):
        _fail("samples/ directory is missing — port check needs samples to verify reads/writes")
    samples: List[Dict[str, Any]] = []
    for fname in sorted(os.listdir(sdir)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(sdir, fname), "r", encoding="utf-8") as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError as exc:
                _fail(f"sample {fname!r} is not valid JSON ({exc})")
        if not isinstance(data, dict):
            _fail(f"sample {fname!r} top-level must be a JSON object")
        samples.append({"name": fname, "data": data})
    if not samples:
        _fail("samples/ contains no .json samples")
    return samples


def main() -> int:
    # 1. ports.json parses and is well-formed.
    ports = _load_json("ports.json")
    if not isinstance(ports, dict):
        _fail("ports.json top-level must be a JSON object")
    inputs = _validate_port_list(ports.get("inputs", []), "inputs", need_required=True)
    outputs = _validate_port_list(ports.get("outputs", []), "outputs", need_required=False)
    if not inputs and not outputs:
        _fail("ports.json declares no inputs and no outputs — an organ with no ports is connectable-by-luck only")
    print(f"  OK ports.json parses ({len(inputs)} input(s), {len(outputs)} output(s))")

    # 2. Every declared type exists in the shared vocabulary.
    vocab = _load_json("types.json")
    if not isinstance(vocab, dict) or not isinstance(vocab.get("types"), dict):
        _fail("types.json must be an object with a 'types' object")
    known_types = set(vocab["types"].keys())
    for p in inputs + outputs:
        if p["type"] not in known_types:
            _fail(
                f"port {p['name']!r} references type {p['type']!r} which is not in the "
                f"vocabulary (types.json). Known: {sorted(known_types)}"
            )
    print(f"  OK every declared type exists in the vocabulary ({len(known_types)} types known)")

    # 3. decide reads each declared input name and writes each declared output
    #    name, sampled against the organ's own samples.
    try:
        from organ import decide  # noqa: WPS433 (intentional late import)
    except Exception as exc:  # pragma: no cover - import failure is a real fault
        _fail(f"could not import organ.decide ({exc})")

    samples = _load_samples()

    # Required inputs must appear under `state` in every sample (evidence the
    # wire is genuinely supplied + read). Optional inputs must appear in at
    # least one sample.
    for p in inputs:
        name = p["name"]
        required = bool(p.get("required", False))
        present = [s["name"] for s in samples
                   if isinstance(s["data"].get("state"), dict) and name in s["data"]["state"]]
        if required and len(present) != len(samples):
            missing = [s["name"] for s in samples if s["name"] not in present]
            _fail(f"required input port {name!r} is absent from state in sample(s): {missing}")
        if not present:
            _fail(f"input port {name!r} appears in no sample's state — cannot evidence decide reads it")
    print(f"  OK every declared input name is supplied in the samples ({len(inputs)} input(s))")

    # Output ports must appear under `output` when decide runs on the samples.
    declared_outputs = [p["name"] for p in outputs]
    for s in samples:
        state = s["data"].get("state") or {}
        context = s["data"].get("context") or {}
        result = decide(state, context)
        if not isinstance(result, dict) or not isinstance(result.get("output"), dict):
            _fail(f"decide() on sample {s['name']!r} did not return an object with an 'output' dict")
        out_keys = result["output"]
        for name in declared_outputs:
            if name not in out_keys:
                _fail(
                    f"declared output port {name!r} not written by decide() on sample "
                    f"{s['name']!r} (output keys: {sorted(out_keys)})"
                )
    print(f"  OK every declared output name is written by decide() across {len(samples)} sample(s) "
          f"({len(declared_outputs)} output(s))")

    print("port conformance OK: ports.json is valid, typed against the vocabulary, and matches decide()'s real wiring")
    return 0


if __name__ == "__main__":
    sys.exit(main())
