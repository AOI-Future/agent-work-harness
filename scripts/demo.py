"""Run the complete fixture loop without models, network or shared-board writes."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from harness.runtime import Harness
from scripts.fixture import fixture


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="work-harness-demo-") as temporary:
        root = Path(temporary).resolve()
        spec = fixture(root)
        harness = Harness(root / "runtime")
        try:
            harness.submit(spec)
            worker = harness.advance(
                spec.id, {"action": "claim", "role": "worker", "actor": "fixture-implementation"}
            )
            (Path(worker["lease"]["workspace"]) / "src/pricing.py").write_text(
                "def total(amount, discount):\n    return amount - discount\n"
            )
            harness.advance(
                spec.id,
                {
                    "action": "report",
                    "report": {"token": worker["lease"]["token"], "outcome": "ready"},
                },
            )
            verified = harness.advance(spec.id, {"action": "verify"})
            assert verified["state"] == "review_ready"
            reviewer = harness.advance(
                spec.id, {"action": "claim", "role": "reviewer", "actor": "fixture-review"}
            )
            result = harness.advance(
                spec.id,
                {
                    "action": "review",
                    "report": {
                        "token": reviewer["lease"]["token"],
                        "revision": reviewer["target_revision"],
                        "verdict": "pass",
                        "covered_criteria": ["discount-behavior"],
                        "findings": [],
                    },
                },
            )
            assert result["state"] == "completed" and result["evidence_passed"]
            print(
                json.dumps(
                    {
                        "fixture": "pricing-fix",
                        "state": result["state"],
                        "evidence_passed": result["evidence_passed"],
                        "attempts": result["attempts"],
                        "limitations": "deterministic fixture; no real model invoked",
                    },
                    indent=2,
                )
            )
        finally:
            harness.close()


if __name__ == "__main__":
    main()
