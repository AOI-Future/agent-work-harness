from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness.runtime import Harness
from scripts.executor_pi import (
    Control,
    ExecutorError,
    parse_verdict,
    run_loop,
    run_worker,
)
from scripts.fixture import fixture


class ExecutorSeamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.spec = fixture(self.root)
        self.harness = Harness(self.root / "runtime", clock=lambda: 1800000000.0)
        self.harness.submit(self.spec)

    def tearDown(self) -> None:
        self.harness.close()
        self.temporary.cleanup()

    def control(self) -> Control:
        def run(operation: str, args: dict[str, Any]) -> dict[str, Any]:
            if operation in {"claim", "report", "review", "verify"}:
                command: dict[str, Any] = {"action": operation}
                command.update({k: v for k, v in args.items() if k != "task"})
                return self.harness.advance(self.spec.id, command)
            if operation == "inspect":
                return self.harness.inspect(self.spec.id)
            raise AssertionError(operation)

        return run

    @staticmethod
    def fixing_backend(prompt: str, packet: dict[str, Any], workspace: Path, t: float) -> str:
        assert "受入基準" in prompt
        (workspace / "src/pricing.py").write_text(
            "def total(amount, discount):\n    return amount - discount\n"
        )
        return "implemented"

    @staticmethod
    def passing_review(prompt: str, packet: dict[str, Any], workspace: Path, t: float) -> str:
        assert "独立レビュアー" in prompt
        return 'notes\n{"verdict":"pass","covered_criteria":["discount-behavior"],"findings":[]}'

    def test_loop_completes_without_model_calls(self) -> None:
        view = run_loop(
            self.control(),
            self.spec.id,
            "stub-worker",
            "stub-reviewer",
            self.fixing_backend,
            self.passing_review,
            60.0,
        )
        self.assertEqual(view["state"], "completed")
        self.assertTrue(view["evidence_passed"])
        self.assertEqual(view["human_interventions"], 0)

    def test_failing_backend_returns_control_to_human(self) -> None:
        def broken(prompt: str, packet: dict[str, Any], workspace: Path, t: float) -> str:
            raise ExecutorError("session died")

        view = run_worker(self.control(), self.spec.id, "stub-worker", broken, 60.0)
        self.assertEqual(view["state"], "waiting_human")

    def test_unparseable_verdict_submits_nothing(self) -> None:
        with self.assertRaises(ExecutorError):
            parse_verdict("I think it looks fine\nno json here")

    def test_parse_verdict_takes_last_strict_object(self) -> None:
        verdict = parse_verdict(
            '{"notes":"decoy without keys"}\n{"verdict":"pass","covered_criteria":["a"]}'
        )
        self.assertEqual(verdict["verdict"], "pass")
