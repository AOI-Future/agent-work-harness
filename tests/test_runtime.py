from __future__ import annotations

import json
import os
import py_compile
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from harness.checks import run_check
from harness.models import Check, TaskSpec
from harness.runtime import Harness
from harness.workspace import HarnessError, copy_workspace, git, snapshot
from scripts.fixture import fixture


class RuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.spec = fixture(self.root)
        self.now = 1800000000.0
        self.harness = Harness(self.root / "runtime", clock=lambda: self.now)
        self.harness.submit(self.spec)

    def tearDown(self) -> None:
        self.harness.close()
        self.temporary.cleanup()

    def claim(self, role: str = "worker", actor: str | None = None) -> dict[str, Any]:
        return self.harness.advance(
            self.spec.id, {"action": "claim", "role": role, "actor": actor or role}
        )

    def implement(self, packet: dict[str, Any], *, good: bool = True) -> None:
        workspace = Path(packet["lease"]["workspace"])
        (workspace / "src/pricing.py").write_text(
            "def total(amount, discount):\n    return amount - discount"
            + ("\n" if good else " + 1\n")
        )
        self.harness.advance(
            self.spec.id,
            {"action": "report", "report": {"token": packet["lease"]["token"], "outcome": "ready"}},
        )

    def verified(self) -> dict[str, Any]:
        self.implement(self.claim())
        return self.harness.advance(self.spec.id, {"action": "verify"})

    def review(self, packet: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        report = {
            "token": packet["lease"]["token"],
            "revision": packet["target_revision"],
            "verdict": "pass",
            "covered_criteria": ["discount-behavior"],
            "findings": [],
        }
        return self.harness.advance(
            self.spec.id, {"action": "review", "report": report | overrides}
        )

    def test_end_to_end_and_original_untouched(self) -> None:
        self.assertEqual(self.verified()["state"], "review_ready")
        view = self.review(self.claim("reviewer"))
        self.assertEqual(view["state"], "completed")
        self.assertTrue(view["evidence_passed"])
        self.assertEqual(git(Path(self.spec.source_repo), "status", "--porcelain"), b"")
        self.assertIn("+ 1", (Path(self.spec.source_repo) / "src/pricing.py").read_text())
        evidence = self.harness.store.get(self.spec.id)["evidence"]
        self.assertTrue(evidence["passed"])
        self.assertEqual(len(evidence["receipt"]["criterion_evidence"]), 2)

    def test_registration_idempotent_and_contract_frozen(self) -> None:
        self.harness.submit(self.spec)
        self.assertEqual(len(self.harness.store.events(self.spec.id)), 1)
        with self.assertRaises(HarnessError):
            self.harness.submit(self.spec.model_copy(update={"goal": "another request"}))

    def test_duplicate_claim_rejected(self) -> None:
        self.claim()
        with self.assertRaises(HarnessError):
            self.claim(actor="another-worker")

    def test_parallel_controllers_only_one_claim(self) -> None:
        def attempt(index: int) -> bool:
            controller = Harness(self.root / "runtime", clock=lambda: self.now)
            try:
                controller.advance(
                    self.spec.id, {"action": "claim", "role": "worker", "actor": f"worker-{index}"}
                )
                return True
            except HarnessError:
                return False
            finally:
                controller.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt, range(2))), 1)

    def test_failure_cannot_complete_and_escalation_is_bounded(self) -> None:
        tiers = []
        for _ in range(3):
            packet = self.claim()
            tiers.append(packet["lease"]["tier"])
            self.implement(packet, good=False)
            view = self.harness.advance(self.spec.id, {"action": "verify"})
            self.assertFalse(view["evidence_passed"])
        self.assertEqual(tiers, ["small", "small", "strong"])
        self.assertEqual(view["state"], "escalated")
        with self.assertRaises(HarnessError):
            self.claim()

    def test_verification_state_cannot_be_skipped(self) -> None:
        with self.assertRaises(HarnessError):
            self.harness.advance(self.spec.id, {"action": "verify"})
        with self.assertRaises(HarnessError):
            self.claim("reviewer")

    def test_self_review_rejected(self) -> None:
        self.verified()
        with self.assertRaises(HarnessError):
            self.claim("reviewer", "worker")

    def test_every_previous_author_is_excluded_from_review(self) -> None:
        self.verified()
        self.review(
            self.claim("reviewer"), verdict="changes", findings=["src/pricing.py: edge case"]
        )
        self.implement(self.claim(actor="worker-2"))
        self.harness.advance(self.spec.id, {"action": "verify"})
        with self.assertRaises(HarnessError):
            self.claim("reviewer", "worker")

    def test_forged_test_bytecode_cannot_bypass_frozen_acceptance(self) -> None:
        packet = self.claim()
        workspace = Path(packet["lease"]["workspace"])
        source = workspace / "tests/test_pricing.py"
        original = source.read_bytes()
        timestamp = source.stat().st_mtime
        forged = (
            "import unittest\nclass AlwaysPass(unittest.TestCase):\n    def test_pass(self): pass\n"
        )
        forged += " " * (len(original) - len(forged.encode()))
        source.write_text(forged)
        os.utime(source, (timestamp, timestamp))
        py_compile.compile(str(source), doraise=True)
        source.write_bytes(original)
        os.utime(source, (timestamp, timestamp))
        self.harness.advance(
            self.spec.id,
            {"action": "report", "report": {"token": packet["lease"]["token"], "outcome": "ready"}},
        )
        result = self.harness.advance(self.spec.id, {"action": "verify"})
        self.assertEqual(result["state"], "ready")
        self.assertFalse(result["evidence_passed"])

    def test_slow_verification_does_not_lock_other_tasks_or_allow_duplicate_verifier(self) -> None:
        marker = self.root / "check-started"
        slow = Check(
            id="slow",
            statement="bounded slow verification",
            timeout_seconds=10,
            argv=[
                sys.executable,
                "-c",
                f"from pathlib import Path; import time; "
                f"Path({str(marker)!r}).write_text('started'); time.sleep(2)",
            ],
        )
        spec = self.spec.model_copy(update={"id": "slow-task", "checks": [slow]})
        self.harness.submit(spec)
        packet = self.harness.advance(spec.id, {"action": "claim", "role": "worker", "actor": "w"})
        self.harness.advance(
            spec.id,
            {"action": "report", "report": {"token": packet["lease"]["token"], "outcome": "ready"}},
        )
        errors: list[BaseException] = []

        def verify() -> None:
            controller = Harness(self.root / "runtime", clock=lambda: self.now)
            try:
                controller.advance(spec.id, {"action": "verify"})
            except BaseException as error:
                errors.append(error)
            finally:
                controller.close()

        thread = threading.Thread(target=verify)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            started = time.monotonic()
            self.claim()
            self.assertLess(time.monotonic() - started, 1.5)
            with self.assertRaises(HarnessError):
                self.harness.advance(spec.id, {"action": "verify"})
        finally:
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_frozen_scope_and_acceptance_tamper(self) -> None:
        packet = self.claim()
        (Path(packet["lease"]["workspace"]) / "tests/test_pricing.py").write_text("# bypass\n")
        self.implement(packet)
        view = self.harness.advance(self.spec.id, {"action": "verify"})
        self.assertEqual(view["state"], "waiting_human")

    def test_ignored_file_outside_scope_is_detected(self) -> None:
        packet = self.claim()
        (Path(packet["lease"]["workspace"]) / ".env").write_text("fixture-only\n")
        self.implement(packet)
        self.assertEqual(
            self.harness.advance(self.spec.id, {"action": "verify"})["state"], "waiting_human"
        )

    def test_revision_change_before_review_invalidates_checks(self) -> None:
        view = self.verified()
        (Path(view["workspace"]) / "src/pricing.py").write_text("# changed again\n")
        result = self.claim("reviewer")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["checks"], [])

    def test_revision_change_during_review_blocks_completion(self) -> None:
        self.verified()
        packet = self.claim("reviewer")
        workspace = Path(self.harness.store.get(self.spec.id)["workspace"])
        (workspace / "src/pricing.py").write_text("# drift\n")
        self.assertEqual(self.review(packet)["state"], "ready")

    def test_permission_change_is_part_of_revision(self) -> None:
        view = self.verified()
        file = Path(view["workspace"]) / "src/pricing.py"
        file.chmod(0o755)
        self.assertEqual(self.claim("reviewer")["state"], "ready")

    def test_check_that_modifies_source_cannot_attest_itself(self) -> None:
        check = Check(
            id="tamper",
            statement="a check may not change the reviewed source",
            argv=[
                sys.executable,
                "-c",
                "from pathlib import Path; Path('src/pricing.py').write_text('# mutated')",
            ],
        )
        spec = self.spec.model_copy(update={"id": "check-tamper", "checks": [check]})
        self.harness.submit(spec)
        packet = self.harness.advance(spec.id, {"action": "claim", "role": "worker", "actor": "w"})
        self.harness.advance(
            spec.id,
            {"action": "report", "report": {"token": packet["lease"]["token"], "outcome": "ready"}},
        )
        self.assertEqual(self.harness.advance(spec.id, {"action": "verify"})["state"], "ready")

    def test_expired_reviewer_recovery_does_not_repeat_implementation(self) -> None:
        self.verified()
        self.claim("reviewer")
        self.now += self.spec.lease_seconds + 1
        view = self.harness.advance(self.spec.id, {"action": "recover"})
        self.assertEqual(view["state"], "review_ready")
        self.assertEqual(view["attempts"], 1)
        self.assertEqual(
            self.review(self.claim("reviewer", "second-reviewer"))["state"], "completed"
        )

    def test_reports_cannot_be_replayed(self) -> None:
        packet = self.claim()
        self.implement(packet)
        with self.assertRaises(HarnessError):
            self.implement(packet)

    def test_reviewer_copy_is_read_only_by_validation(self) -> None:
        self.verified()
        packet = self.claim("reviewer")
        (Path(packet["lease"]["workspace"]) / "src/pricing.py").write_text("# reviewer edit\n")
        self.assertEqual(self.review(packet)["state"], "ready")

    def test_wrong_revision_or_partial_review_rejected(self) -> None:
        self.verified()
        packet = self.claim("reviewer")
        with self.assertRaises(HarnessError):
            self.review(packet, revision="another-revision")
        with self.assertRaises(HarnessError):
            self.review(packet, covered_criteria=[])
        with self.assertRaises(HarnessError):
            self.review(packet, verdict="changes")
        with self.assertRaises(HarnessError):
            self.review(packet, findings=["unresolved issue"])

    def test_review_changes_start_new_implementation_and_recheck(self) -> None:
        self.verified()
        packet = self.claim("reviewer")
        view = self.review(
            packet, verdict="changes", findings=["src/pricing.py: add edge handling"]
        )
        self.assertEqual(view["state"], "ready")
        next_packet = self.claim(actor="worker-2")
        self.assertNotEqual(next_packet["lease"]["workspace"], packet["lease"]["workspace"])
        self.implement(next_packet)
        self.assertEqual(
            self.harness.advance(self.spec.id, {"action": "verify"})["state"], "review_ready"
        )

    def test_restart_preserves_state_and_token_fencing(self) -> None:
        packet = self.claim()
        self.harness.close()
        self.harness = Harness(self.root / "runtime", clock=lambda: self.now)
        self.assertEqual(self.harness.inspect(self.spec.id)["state"], "implementing")
        self.now += self.spec.lease_seconds + 1
        with self.assertRaises(HarnessError):
            self.implement(packet)
        view = self.harness.advance(self.spec.id, {"action": "recover"})
        self.assertEqual(view["state"], "ready")
        replacement = self.claim(actor="worker-2")
        self.assertNotEqual(packet["lease"]["workspace"], replacement["lease"]["workspace"])
        old_file = Path(packet["lease"]["workspace"]) / "src/pricing.py"
        old_file.write_text("# late old worker\n")
        self.implement(replacement)
        self.assertEqual(
            self.harness.advance(self.spec.id, {"action": "verify"})["state"], "review_ready"
        )

    def test_completed_revision_drift_is_quarantined(self) -> None:
        self.verified()
        view = self.review(self.claim("reviewer"))
        (Path(view["workspace"]) / "src/pricing.py").write_text("# drift\n")
        view = self.harness.inspect(self.spec.id)
        self.assertEqual(view["state"], "waiting_human")
        self.assertFalse(view["evidence_passed"])

    def test_dependency_must_complete_and_remain_valid(self) -> None:
        child = self.spec.model_copy(update={"id": "child", "depends_on": [self.spec.id]})
        self.harness.submit(child)
        command = {"action": "claim", "role": "worker", "actor": "child-worker"}
        with self.assertRaises(HarnessError):
            self.harness.advance("child", command)
        self.verified()
        view = self.review(self.claim("reviewer"))
        (Path(view["workspace"]) / "src/pricing.py").write_text("# stale parent\n")
        with self.assertRaises(HarnessError):
            self.harness.advance("child", command)

    def test_decision_required_and_task_expiry_stop_execution(self) -> None:
        hold = self.spec.model_copy(update={"id": "hold", "requires_human": True})
        self.harness.submit(hold)
        with self.assertRaises(HarnessError):
            self.harness.advance("hold", {"action": "claim", "role": "worker", "actor": "w"})
        self.now += self.spec.lifetime_seconds + 1
        self.assertEqual(self.harness.inspect(self.spec.id)["state"], "waiting_human")

    def test_unknown_dependency_and_dirty_source_rejected(self) -> None:
        with self.assertRaises(HarnessError):
            self.harness.submit(
                self.spec.model_copy(update={"id": "new", "depends_on": ["missing"]})
            )
        (Path(self.spec.source_repo) / "unrelated.txt").write_text("another task\n")
        with self.assertRaises(HarnessError):
            self.harness.submit(self.spec.model_copy(update={"id": "dirty"}))

    def test_notifications_cursor_and_intervention_metrics(self) -> None:
        self.verified()
        self.assertEqual(self.harness.store.events(self.spec.id, notifications=True), [])
        self.review(self.claim("reviewer"))
        events = self.harness.store.events(self.spec.id, notifications=True)
        self.assertEqual([event["kind"] for event in events], ["completed"])
        self.assertEqual(
            self.harness.store.events(self.spec.id, notifications=True, after=events[-1]["seq"]), []
        )
        view = self.harness.advance(self.spec.id, {"action": "intervention", "seconds": 15})
        self.assertEqual(view["human_interventions"], 1)
        self.assertEqual(view["human_seconds"], 15)
        self.assertIsNone(view["reported_cost"])

    def test_symlink_and_path_escape_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            TaskSpec.model_validate(self.spec.model_dump() | {"allowed_paths": ["../escape"]})
        packet = self.claim()
        (Path(packet["lease"]["workspace"]) / "src/link").symlink_to(self.root / "source")
        self.implement(packet)
        self.assertEqual(
            self.harness.advance(self.spec.id, {"action": "verify"})["state"], "waiting_human"
        )

    def test_actor_reserved_and_numeric_inputs_strict(self) -> None:
        with self.assertRaises(HarnessError):
            self.claim(actor="controller-verifier")
        with self.assertRaises(HarnessError):
            self.harness.advance(self.spec.id, {"action": "intervention", "seconds": float("nan")})

    def test_cli_invalid_input_does_not_echo_values(self) -> None:
        invalid = self.root / "invalid.json"
        invalid.write_text(json.dumps({"secret": "do-not-echo-fixture"}))
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "harness.cli",
                "--runtime",
                str(self.root / "cli-state"),
                "submit",
                str(invalid),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("do-not-echo-fixture", result.stdout + result.stderr)


class CheckTest(unittest.TestCase):
    def test_directory_and_file_replacement_survive_workspace_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            spec = fixture(root)
            source = Path(spec.source_repo)
            base = git(source, "rev-parse", "HEAD").decode().strip()
            previous = root / "previous"
            copy_workspace(source, base, previous, None)
            (previous / "src/pricing.py").unlink()
            (previous / "src").rmdir()
            (previous / "src").write_text("directory replaced by file\n")
            copied = root / "copied"
            copy_workspace(source, base, copied, previous)
            self.assertEqual(snapshot(copied), snapshot(previous))
            (copied / "src").unlink()
            (copied / "src/new").mkdir(parents=True)
            (copied / "src/new/module.py").write_text("file replaced by directory\n")
            copied_again = root / "copied-again"
            copy_workspace(source, base, copied_again, copied)
            self.assertEqual(snapshot(copied_again), snapshot(copied))

    def test_output_limit_timeout_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            noisy = Check(
                id="noisy",
                statement="bounded output",
                argv=[sys.executable, "-c", "print('x'*1100000)"],
            )
            self.assertEqual(run_check(noisy, workspace)["reason"], "output_limit")
            slow = Check(
                id="slow",
                statement="bounded time",
                timeout_seconds=1,
                argv=[sys.executable, "-c", "import time; time.sleep(5)"],
            )
            self.assertEqual(run_check(slow, workspace)["reason"], "timeout")
            closed = slow.model_copy(
                update={
                    "argv": [
                        sys.executable,
                        "-c",
                        "import os,time; os.close(1); os.close(2); time.sleep(5)",
                    ]
                }
            )
            self.assertEqual(run_check(closed, workspace)["reason"], "timeout")
            os.environ["HARNESS_TEST_CREDENTIAL"] = "fixture-only"
            try:
                safe = Check(
                    id="env",
                    statement="minimal env",
                    argv=[
                        sys.executable,
                        "-c",
                        "import os; assert 'HARNESS_TEST_CREDENTIAL' not in os.environ",
                    ],
                )
                self.assertTrue(run_check(safe, workspace)["passed"])
            finally:
                del os.environ["HARNESS_TEST_CREDENTIAL"]


if __name__ == "__main__":
    unittest.main()
