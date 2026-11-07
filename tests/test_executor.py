from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from harness.models import TaskSpec
from harness.runtime import Harness
from scripts.executor_pi import (
    Control,
    ExecutorError,
    lease_capped_timeout,
    parse_verdict,
    reviewer_prompt,
    run_loop,
    run_worker,
    worker_prompt,
)
from scripts.fixture import fixture

STUB_BACKEND = str(Path(__file__).resolve().parent / "stub_backend.py")


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

    def test_worker_prompt_carries_failures_and_refusal_channel(self) -> None:
        packet = self.harness.advance(
            self.spec.id, {"action": "claim", "role": "worker", "actor": "stub-worker"}
        )
        prompt = worker_prompt(packet)
        self.assertIn("受入基準", prompt)
        self.assertIn("保護パス", prompt)
        self.assertIn("過去の失敗", prompt)
        self.assertIn("NEEDS_DECISION", prompt)

    def test_reviewer_prompt_is_read_only_and_revision_bound(self) -> None:
        run_worker(self.control(), self.spec.id, "stub-worker", self.fixing_backend, 60.0)
        self.control()("verify", {"task": self.spec.id})
        packet = self.harness.advance(
            self.spec.id, {"action": "claim", "role": "reviewer", "actor": "stub-reviewer"}
        )
        prompt = reviewer_prompt(packet)
        self.assertIn("独立レビュアー", prompt)
        self.assertIn(packet["target_revision"], prompt)

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

    def test_refusal_sentinel_reaches_human(self) -> None:
        def refusing(prompt: str, packet: dict[str, Any], workspace: Path, t: float) -> str:
            return "作業途中です\nNEEDS_DECISION: 設計判断が必要"

        view = run_worker(self.control(), self.spec.id, "stub-worker", refusing, 60.0)
        self.assertEqual(view["state"], "waiting_human")

    def test_timeout_is_capped_by_lease(self) -> None:
        packet = {"lease": {"expires_at": time.time() + 10}}
        self.assertLessEqual(lease_capped_timeout(packet, 1500.0), 1.0)

    def test_fix_loop_survives_changes_then_pass(self) -> None:
        control = self.control()
        run_worker(control, self.spec.id, "stub-worker", self.fixing_backend, 60.0)
        control("verify", {"task": self.spec.id})

        calls = {"n": 0}

        def review_once(prompt: str, packet: dict[str, Any], workspace: Path, t: float) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                return json.dumps(
                    {
                        "verdict": "changes",
                        "covered_criteria": ["discount-behavior"],
                        "findings": ["off-by-one が残っている"],
                    }
                )
            return self.passing_review(prompt, packet, workspace, t)

        view = run_loop(
            control,
            self.spec.id,
            "stub-worker",
            "stub-reviewer",
            self.fixing_backend,
            review_once,
            60.0,
        )
        self.assertEqual(view["state"], "completed")
        self.assertEqual(calls["n"], 2)

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


class AdapterMainTest(unittest.TestCase):
    """Exercises main() end-to-end through the real CLI via --backend-cmd argv."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.spec: TaskSpec = fixture(self.root)
        self.harness = Harness(self.root / "runtime")
        self.harness.submit(self.spec)
        self.harness.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_adapter(self, *extra: str) -> subprocess.CompletedProcess[str]:
        repo = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.executor_pi",
                "--runtime",
                str(self.root / "runtime"),
                "--task",
                self.spec.id,
                "--timeout",
                "60",
                *extra,
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    def test_loop_completes_through_argv_backend(self) -> None:
        result = self.run_adapter("--mode", "loop", "--backend-cmd", sys.executable, STUB_BACKEND)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "completed")

    def test_backend_cmd_keeps_argv_as_list_not_characters(self) -> None:
        result = self.run_adapter("--mode", "worker", "--backend-cmd", sys.executable, STUB_BACKEND)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "verifying")

    def test_unresolvable_backend_degrades_to_needs_decision(self) -> None:
        result = self.run_adapter(
            "--mode", "worker", "--backend-cmd", "/nonexistent/backend-binary"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        view = json.loads(result.stdout)
        self.assertEqual(view["state"], "waiting_human")
        self.assertIn("needs_decision", [c["status"] for c in view["checks"]] or ["needs_decision"])


if __name__ == "__main__":
    unittest.main()
