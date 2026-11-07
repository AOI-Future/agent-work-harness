"""Deterministic argv backend for adapter wiring tests.

Reads the seam stdin contract ({prompt, task_id}), fixes the fixture product
idempotently, and prints a reviewer verdict so one stub can serve both roles.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXED = "def total(amount, discount):\n    return amount - discount\n"


def main() -> int:
    payload = json.load(sys.stdin)
    product = Path("src/pricing.py")
    if product.exists():
        product.write_text(FIXED)
    print(json.dumps({"outcome": "ready", "prompt_len": len(payload["prompt"])}))
    print(
        json.dumps({"verdict": "pass", "covered_criteria": ["discount-behavior"], "findings": []})
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
