"""Emit contracts/*.schema.json from the pydantic contract models.

WHY GENERATED, AND WHY THAT IS STILL SCHEMA-FIRST
-------------------------------------------------
The Master Plan asks for contracts with codegen on both sides. What must be true is that there is exactly
ONE definition of every contract and that the JSON Schema a customer validates against cannot drift from
the types the engine enforces. Two hand-maintained artefacts guarantee the opposite: they agree on the day
they are written and silently diverge afterwards, and a schema that disagrees with the engine is worse
than no schema at all, because it is believed.

So the pydantic models in ``mizan/contracts`` are the single definition, and this script derives the JSON
Schema from them. ``tests/contracts/test_schema_generation.py`` regenerates and diffs, so a model change
that is not reflected in ``contracts/`` fails CI. The freeze applies to both together.

Run:  python scripts/generate_schemas.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mizan.contracts import SCHEMA_VERSION, TOP_LEVEL_CONTRACTS  # noqa: E402

CONTRACTS_DIR = REPO_ROOT / "contracts"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
BASE_ID = "https://mizan.dev/contracts"

TITLES = {
    "trade_proposal": "What an agent proposes. Never what it is permitted to do.",
    "risk_context": "Everything the deterministic engine is allowed to know, captured at one instant.",
    "policy": "The versioned, hashed rules a decision was made under.",
    "risk_evaluation": "The deterministic verdict. Produced with no LLM in the path.",
    "governor_decision": "Arbitration between the deterministic verdict and the advisory opinion.",
    "execution_authorization": "Short-lived, single-use, state-bound permission to submit one order.",
    "execution_result": "What happened at the gate and, if it got that far, at the broker.",
    "decision_record": "One immutable, hash-chained link in a tenant's decision chain.",
    "control_event": "A graduated-response level change or kill-switch flip, in the same chain.",
}


def _harden(node: Any) -> Any:
    """Recursively close every object and pin every environment enum to paper.

    Two properties are enforced here rather than trusted: an object that does not say
    ``additionalProperties: false`` silently accepts unknown fields, and an ``environment`` that is not
    exactly ``["paper"]`` is a representable live path (Hard Rule B1).
    """
    if isinstance(node, dict):
        out = {key: _harden(value) for key, value in node.items()}
        if out.get("type") == "object" or "properties" in out:
            out.setdefault("additionalProperties", False)
        properties = out.get("properties")
        if isinstance(properties, dict) and "environment" in properties:
            environment = dict(properties["environment"])
            environment.pop("const", None)
            environment["type"] = "string"
            environment["enum"] = ["paper"]
            environment["description"] = (
                "Paper is the only representable environment. Live trading is a separate deployment "
                "and security boundary, not a value of this field (Hard Rule B1)."
            )
            properties["environment"] = environment
        return out
    if isinstance(node, list):
        return [_harden(item) for item in node]
    return node


def schema_for(name: str) -> dict[str, Any]:
    model = TOP_LEVEL_CONTRACTS[name]
    schema = model.model_json_schema(mode="validation", ref_template="#/$defs/{model}")
    schema = _harden(schema)
    ordered: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "$id": f"{BASE_ID}/{SCHEMA_VERSION}/{name}.schema.json",
        "title": model.__name__,
        "description": TITLES.get(name, model.__doc__ or "").strip().splitlines()[0]
        if TITLES.get(name) or model.__doc__
        else model.__name__,
        "x-mizan-schema-version": SCHEMA_VERSION,
        "x-mizan-generated-from": f"mizan.contracts.{model.__module__.rsplit('.', 1)[-1]}.{model.__name__}",
    }
    for key, value in schema.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def render(name: str) -> str:
    return json.dumps(schema_for(name), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 if any file would change")
    args = parser.parse_args()

    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for name in TOP_LEVEL_CONTRACTS:
        target = CONTRACTS_DIR / f"{name}.schema.json"
        rendered = render(name)
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current == rendered:
            continue
        if args.check:
            stale.append(target.name)
            continue
        target.write_text(rendered, encoding="utf-8")
        print(f"wrote {target.relative_to(REPO_ROOT)}")

    if stale:
        print("stale schema(s); run python scripts/generate_schemas.py", file=sys.stderr)
        for name in stale:
            print(f"  {name}", file=sys.stderr)
        return 1
    if args.check:
        print(f"{len(TOP_LEVEL_CONTRACTS)} schema(s) up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
