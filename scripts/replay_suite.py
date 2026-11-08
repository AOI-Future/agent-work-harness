"""Deterministic replay suite: fixed cases from examples/replay against the live controller.

No models, no network, no shared-board writes. Each case builds a fresh fixture
repository, replays a frozen lifecycle scenario through the real Harness, and
compares the terminal state (and refusal behavior) with the recorded expectation.
Run on every harness change via ``make replay`` (or ``make ci``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from harness.models import TaskSpec
from harness.runtime import Harness
from harness.workspace import HarnessError
from scripts.fixture import fixture

FIXED = "def total(amount, discount):\n    return amount - discount\n"
GIT = "/usr/bin/git"
CASES_DIR = Path(__file__).resolve().parent.parent / "examples" / "replay"


def _git(root: Path, *args: str) -> str:
    done = subprocess.run([GIT, "-C", str(root), *args], check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _submit(root: Path, case: dict[str, Any]) -> tuple[Harness, TaskSpec]:
    spec = fixture(root)
    overrides = case.get("spec_overrides") or {}
    if overrides:
        payload = spec.model_dump(mode="json")
        payload.update(overrides)
        spec = TaskSpec.model_validate(payload)
    harness = Harness(root / "runtime")
    harness.submit(spec, preflight=case["preflight"])
    return harness, spec


def _fix_and_report(harness: Harness, spec: TaskSpec, actor: str = "pi-worker") -> None:
    worker = harness.advance(spec.id, {"action": "claim", "role": "worker", "actor": actor})
    workspace = Path(worker["lease"]["workspace"])
    (workspace / "src" / "pricing.py").write_text(FIXED)
    harness.advance(
        spec.id,
        {"action": "report", "report": {"token": worker["lease"]["token"], "outcome": "ready"}},
    )
    harness.advance(spec.id, {"action": "verify"})


def _approve(harness: Harness, spec: TaskSpec, actor: str = "pi-reviewer") -> dict[str, Any]:
    reviewer = harness.advance(spec.id, {"action": "claim", "role": "reviewer", "actor": actor})
    return harness.advance(
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


def _simulate_push(root: Path, spec: TaskSpec, patch_path: Path) -> str:
    """Reproduce the human push step: apply the frozen patch to a clean clone."""
    shipped = root / "shipped"
    subprocess.run(
        [GIT, "clone", "-q", spec.source_repo, str(shipped)], check=True, capture_output=True
    )
    subprocess.run(
        [GIT, "-C", str(shipped), "apply", str(patch_path)], check=True, capture_output=True
    )
    subprocess.run([GIT, "-C", str(shipped), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            GIT,
            "-C",
            str(shipped),
            "-c",
            "user.name=Replay Push",
            "-c",
            "user.email=replay@localhost",
            "commit",
            "-qm",
            "replay: simulated external integration",
        ],
        check=True,
        capture_output=True,
    )
    return _git(shipped, "rev-parse", "HEAD")


def run_happy(root: Path, case: dict[str, Any]) -> dict[str, Any]:
    harness, spec = _submit(root, case)
    _fix_and_report(harness, spec)
    approved = _approve(harness, spec)
    assert approved["state"] == "completed" and approved["evidence_passed"]
    ready = harness.integration_patch(spec.id)
    assert ready["state"] == "integrate_ready"
    patch = Path(ready["patch"]["path"])
    external = _simulate_push(root, spec, patch)
    final = harness.mark_integrated(spec.id, external)
    kinds = [entry["kind"] for entry in harness.store.replay(spec.id)]
    for required in ("submitted", "implementation_reported", "integrate_ready", "integrated"):
        assert required in kinds, f"journal missing {required}"
    return {"state": final["state"], "error": None}


def run_self_review(root: Path, case: dict[str, Any]) -> dict[str, Any]:
    harness, spec = _submit(root, case)
    _fix_and_report(harness, spec)
    error = None
    try:
        _approve(harness, spec, actor="pi-worker")
    except HarnessError:
        error = "HarnessError"
    return {"state": harness.inspect(spec.id)["state"], "error": error}


def run_stale_token(root: Path, case: dict[str, Any]) -> dict[str, Any]:
    harness, spec = _submit(root, case)
    worker = harness.advance(spec.id, {"action": "claim", "role": "worker", "actor": "pi-worker"})
    Path(worker["lease"]["workspace"], "src", "pricing.py").write_text(FIXED)
    error = None
    try:
        harness.advance(
            spec.id,
            {"action": "report", "report": {"token": "lease-deadbeef", "outcome": "ready"}},
        )
    except HarnessError:
        error = "HarnessError"
    return {"state": harness.inspect(spec.id)["state"], "error": error}


def run_boundary(root: Path, case: dict[str, Any]) -> dict[str, Any]:
    harness, spec = _submit(root, case)
    worker = harness.advance(spec.id, {"action": "claim", "role": "worker", "actor": "pi-worker"})
    workspace = Path(worker["lease"]["workspace"])
    (workspace / "src" / "pricing.py").write_text(FIXED)
    (workspace / "README.md").write_text("outside the frozen scope\n")
    harness.advance(
        spec.id,
        {"action": "report", "report": {"token": worker["lease"]["token"], "outcome": "ready"}},
    )
    verdict = harness.advance(spec.id, {"action": "verify"})
    return {"state": verdict["state"], "error": None}


def run_preflight_enforce(root: Path, case: dict[str, Any]) -> dict[str, Any]:
    spec = fixture(root)
    harness = Harness(root / "runtime")
    error = None
    try:
        harness.submit(spec, preflight="enforce")
    except HarnessError:
        error = "HarnessError"
    state = harness.inspect(spec.id)["state"] if harness.store.exists(spec.id) else None
    return {"state": state, "error": error}


def run_preflight_record(root: Path, case: dict[str, Any]) -> dict[str, Any]:
    harness, spec = _submit(root, case)
    submitted = next(e for e in harness.store.replay(spec.id) if e["kind"] == "submitted")
    red = submitted["data"].get("preflight", {}).get("results", [])
    admitted_red = bool(red) and not all(r["passed"] for r in red)
    state = harness.inspect(spec.id)["state"]
    return {"state": state, "error": None, "red_journaled": admitted_red}


SCENARIOS: dict[str, Callable[[Path, dict[str, Any]], dict[str, Any]]] = {
    "happy": run_happy,
    "self_review": run_self_review,
    "stale_token": run_stale_token,
    "boundary": run_boundary,
    "preflight_enforce": run_preflight_enforce,
    "preflight_record": run_preflight_record,
}


def main() -> int:
    cases = sorted(CASES_DIR.glob("*.json"))
    if len(cases) < 5:
        print(f"replay suite too small: {len(cases)} cases", file=sys.stderr)
        return 1
    drifts = 0
    for path in cases:
        try:
            case = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            print(f"unreadable case file {path}: {exc}", file=sys.stderr)
            drifts += 1
            continue
        scenario = SCENARIOS.get(case["scenario"])
        if scenario is None:
            print(f"{case['case']}: unknown scenario {case['scenario']}", file=sys.stderr)
            drifts += 1
            continue
        with tempfile.TemporaryDirectory(prefix="harness-replay-") as temporary:
            try:
                actual = scenario(Path(temporary).resolve(), case)
            except Exception as exc:  # noqa: BLE001 - a crash is a drift, not a pass
                actual = {"state": f"CRASH:{type(exc).__name__}", "error": str(exc)[:120]}
        expected = case["expected"]
        ok = actual["state"] == expected["state"] and actual["error"] == expected["error"]
        if expected.get("journal_preflight_passed") is False:
            ok = ok and actual.get("red_journaled") is True
        mark = "OK  " if ok else "DRIFT"
        print(f"{mark} {case['case']:44s} state={actual['state']} error={actual['error']}")
        drifts += 0 if ok else 1
    total = len(cases)
    print(f"\nreplay suite: {total - drifts}/{total} cases reproduced the recorded contract")
    return 1 if drifts else 0


if __name__ == "__main__":
    sys.exit(main())
