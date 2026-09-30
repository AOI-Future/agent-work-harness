"""A deliberately broken, local-only repository for lifecycle demonstrations."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.models import Check, TaskSpec


def fixture(root: Path) -> TaskSpec:
    repo = root / "source"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src/pricing.py").write_text(
        "def total(amount, discount):\n    return amount - discount + 1\n"
    )
    (repo / "tests/test_pricing.py").write_text(
        "import unittest\nfrom src.pricing import total\n"
        "class PricingTest(unittest.TestCase):\n"
        "    def test_discount(self):\n        self.assertEqual(total(10, 2), 8)\n"
        "    def test_zero(self):\n        self.assertEqual(total(0, 0), 0)\n"
    )
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Local Harness Fixture",
            "-c",
            "user.email=fixture@localhost",
            "commit",
            "-qm",
            "test: seed controlled local fixture",
        ],
    ):
        subprocess.run(["/usr/bin/git", "-C", str(repo), *args], check=True, capture_output=True)
    return TaskSpec(
        id="pricing-fix",
        goal="Fix total so the discount is applied exactly once",
        source_repo=str(repo),
        allowed_paths=["src/pricing.py"],
        checks=[
            Check(
                id="discount-behavior",
                statement="10 minus 2 is 8; zero stays zero",
                argv=["python3", "-m", "unittest", "discover", "-s", "tests"],
            )
        ],
    )
