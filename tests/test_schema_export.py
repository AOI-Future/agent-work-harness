"""Frozen acceptance for the first real self-hosted development task."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SchemaExportTest(unittest.TestCase):
    def test_contract_schemas_are_available_without_runtime_mutation(self) -> None:
        for kind, required in (
            ("task", "goal"),
            ("worker-report", "outcome"),
            ("review-report", "revision"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                runtime = Path(temporary) / "unused-runtime"
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "harness.cli",
                        "--runtime",
                        str(runtime),
                        "schema",
                        "--kind",
                        kind,
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                schema = json.loads(result.stdout)
                self.assertFalse(schema["additionalProperties"])
                self.assertIn(required, schema["required"])
                self.assertFalse(runtime.exists(), "schema export must not initialize state")

    def test_unknown_schema_kind_is_rejected(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "harness.cli", "schema", "--kind", "unknown"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
