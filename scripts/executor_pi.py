"""Thin executor seam: turn claim packets into local worker/reviewer sessions.

The adapter is a trusted local controller operator. It speaks to the harness only
through the official CLI, never through the state database, and it never judges
its own work: the controller verifies on a clean copy and an independent reviewer
completes the task.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

Control = Callable[[str, dict[str, Any]], dict[str, Any]]
Backend = Callable[[str, dict[str, Any], Path, float], str]


class ExecutorError(RuntimeError):
    """The adapter stopped without submitting; the lease recovers via the controller."""


def worker_prompt(packet: dict[str, Any]) -> str:
    criteria = "\n".join(f"- {c['id']}: {c['statement']}" for c in packet["criteria"])
    allowed = ", ".join(packet["allowed_paths"])
    protected = ", ".join(packet["protected_paths"])
    failures = json.dumps(packet["failures"], ensure_ascii=False)
    return (
        f"あなたはタスク {packet['task_id']} 専用のワーカーセッションです。\n"
        f"目的: {packet['goal']}\n"
        f"編集可能なパス: {allowed}\n"
        f"保護パス（変更禁止）: {protected}\n"
        f"受入基準:\n{criteria}\n"
        f"過去の失敗（修正ラウンドではこれを必ず踏襲）: {failures}\n"
        f"禁止行動: {', '.join(packet['forbidden_actions'])}\n"
        "現在の作業コピー内のファイルだけを変更してください。"
        "行頭を NEEDS_DECISION にした行を出力した場合は判断要求として扱う。"
        "基準を満たせないと判断した場合は NEEDS_DECISION と出力して停止し、"
        "それ以外は実装を完了して停止してください。"
    )


def reviewer_prompt(packet: dict[str, Any]) -> str:
    criteria = "\n".join(f"- {c['id']}: {c['statement']}" for c in packet["criteria"])
    checks = json.dumps(packet["checks"], ensure_ascii=False)
    failures = json.dumps(packet["failures"], ensure_ascii=False)
    return (
        f"あなたはタスク {packet['task_id']} の独立レビュアーです。"
        "現在の作業コピーのあらゆるファイル変更を禁止します。\n"
        f"目的: {packet['goal']}\n"
        f"受入基準:\n{criteria}\n"
        f"コントローラの検証結果: {checks}\n"
        f"過去の失敗: {failures}\n"
        f"対象 revision: {packet['target_revision']}\n"
        "git diff で base と比較し、全基準を独立に検証してください。"
        "最終行のみ次の形式の JSON にしてください:\n"
        '{{"verdict":"pass|changes|needs_decision",'
        '"covered_criteria":[基準id全部],"findings":[変更要求時のみ非空]}}'
    )


def parse_verdict(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and {"verdict", "covered_criteria"} <= value.keys():
            return cast("dict[str, Any]", value)
    raise ExecutorError("reviewer produced no parseable verdict; nothing was submitted")


def pi_backend(role: str, model: str | None) -> Backend:
    def run(prompt: str, _packet: dict[str, Any], workspace: Path, timeout: float) -> str:
        argv = ["pi", "--print", "--no-session"]
        if role == "reviewer":
            argv += ["--tools", "read,bash"]
        if model:
            argv += ["--model", model]
        try:
            result = subprocess.run(
                argv + ["--", prompt],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutorError("backend session exceeded its timeout") from exc
        except OSError as exc:
            raise ExecutorError(f"backend session could not start: {exc}") from exc
        if result.returncode != 0:
            raise ExecutorError(f"backend session exited {result.returncode}")
        return result.stdout

    return run


def cmd_backend(argv: list[str]) -> Backend:
    def run(prompt: str, packet: dict[str, Any], workspace: Path, timeout: float) -> str:
        payload = json.dumps({"prompt": prompt, "task_id": packet["task_id"]})
        try:
            result = subprocess.run(
                argv,
                input=payload,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutorError("backend command exceeded its timeout") from exc
        except OSError as exc:
            raise ExecutorError(f"backend command could not start: {exc}") from exc
        if result.returncode != 0:
            raise ExecutorError(f"backend command exited {result.returncode}")
        return result.stdout

    return run


def cli_control(runtime: Path, python: str = sys.executable) -> Control:
    def run(operation: str, args: dict[str, Any]) -> dict[str, Any]:
        argv = [python, "-m", "harness.cli", "--runtime", str(runtime)]
        report_path: str | None = None
        if operation == "claim":
            argv += [
                "claim",
                args["task"],
                "--role",
                args["role"],
                "--actor",
                args["actor"],
            ]
        elif operation in {"report", "review"}:
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                    json.dump(args["report"], handle)
                    report_path = handle.name
            except (OSError, TypeError, ValueError) as exc:
                if report_path is not None:
                    Path(report_path).unlink(missing_ok=True)
                raise ExecutorError(f"report could not be serialized: {exc}") from exc
            argv += [operation, args["task"], report_path]
        elif operation == "verify":
            argv += ["verify", args["task"]]
        elif operation == "inspect":
            argv += ["inspect", args["task"]]
        else:
            raise ExecutorError("adapter may not issue this controller operation")
        try:
            # verify can legitimately run every pinned check plus copy time; keep a wide cap.
            result = subprocess.run(argv, capture_output=True, text=True, timeout=7200, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ExecutorError("controller call exceeded its wall timeout") from exc
        finally:
            if report_path is not None:
                Path(report_path).unlink(missing_ok=True)
        try:
            parsed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutorError("controller returned no JSON") from exc
        if not isinstance(parsed, dict):
            raise ExecutorError("controller returned a non-object payload")
        payload = cast("dict[str, Any]", parsed)
        if result.returncode != 0:
            raise ExecutorError(
                "controller rejected: "
                f"{payload.get('reason', 'unknown')}; next: {payload.get('next', 'unknown')}"
            )
        return payload

    return run


def lease_capped_timeout(packet: dict[str, Any], timeout: float) -> float:
    """Never let a backend outlive its lease minus a submission margin."""
    remaining = float(packet["lease"]["expires_at"]) - time.time() - 30.0
    return max(1.0, min(timeout, remaining))


def run_worker(
    control: Control, task_id: str, actor: str, backend: Backend, timeout: float
) -> dict[str, Any]:
    packet = control("claim", {"task": task_id, "role": "worker", "actor": actor})
    workspace = Path(packet["lease"]["workspace"])
    timeout = lease_capped_timeout(packet, timeout)
    try:
        stdout = backend(worker_prompt(packet), packet, workspace, timeout)
        refusal = any(line.strip().startswith("NEEDS_DECISION") for line in stdout.splitlines())
        outcome: str = "needs_decision" if refusal else "ready"
    except ExecutorError:
        outcome = "needs_decision"
    report = {"token": packet["lease"]["token"], "outcome": outcome}
    return control("report", {"task": task_id, "report": report})


def run_reviewer(
    control: Control, task_id: str, actor: str, backend: Backend, timeout: float
) -> dict[str, Any]:
    packet = control("claim", {"task": task_id, "role": "reviewer", "actor": actor})
    workspace = Path(packet["lease"]["workspace"])
    timeout = lease_capped_timeout(packet, timeout)
    verdict = parse_verdict(backend(reviewer_prompt(packet), packet, workspace, timeout))
    return control(
        "review",
        {
            "task": task_id,
            "report": {
                "token": packet["lease"]["token"],
                "revision": packet["target_revision"],
                "verdict": verdict["verdict"],
                "covered_criteria": verdict["covered_criteria"],
                "findings": [
                    f if isinstance(f, str) else json.dumps(f, ensure_ascii=False)
                    for f in verdict.get("findings", [])
                ],
            },
        },
    )


def run_loop(
    control: Control,
    task_id: str,
    worker_actor: str,
    reviewer_actor: str,
    worker_backend: Backend,
    reviewer_backend: Backend,
    timeout: float,
) -> dict[str, Any]:
    # Each fix cycle spends ~5 iterations (inspect/worker/inspect/verify/review-inspect);
    # the controller escalates on its own attempt budgets long before this bound.
    for _ in range(60):
        state = str(control("inspect", {"task": task_id})["state"])
        if state == "ready":
            run_worker(control, task_id, worker_actor, worker_backend, timeout)
        elif state == "verifying":
            control("verify", {"task": task_id})
        elif state == "review_ready":
            run_reviewer(control, task_id, reviewer_actor, reviewer_backend, timeout)
        elif state in {"completed", "waiting_human", "escalated"}:
            return control("inspect", {"task": task_id})
        else:
            raise ExecutorError(f"loop cannot drive state {state}")
    raise ExecutorError("loop budget exhausted without a terminal state")


def main() -> int:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--runtime", type=Path, required=True)
    root.add_argument("--task", required=True)
    root.add_argument("--mode", choices=["worker", "reviewer", "loop"], default="loop")
    root.add_argument("--worker-actor", default="pi-worker")
    root.add_argument("--reviewer-actor", default="pi-reviewer")
    root.add_argument("--model", help="explicit executor-side model override")
    root.add_argument("--timeout", type=float, default=1500.0)
    root.add_argument(
        "--backend-cmd",
        nargs="+",
        metavar="ARGV",
        help="deterministic argv backend (stdin: prompt+task_id JSON); for tests and stubs",
    )
    args = root.parse_args()
    control = cli_control(args.runtime.absolute())
    if args.backend_cmd:
        worker_backend = reviewer_backend = cmd_backend(list(args.backend_cmd))
    else:
        worker_backend = pi_backend("worker", args.model)
        reviewer_backend = pi_backend("reviewer", args.model)
    try:
        if args.mode == "worker":
            result: dict[str, Any] = run_worker(
                control, args.task, args.worker_actor, worker_backend, args.timeout
            )
        elif args.mode == "reviewer":
            result = run_reviewer(
                control, args.task, args.reviewer_actor, reviewer_backend, args.timeout
            )
        else:
            result = run_loop(
                control,
                args.task,
                args.worker_actor,
                args.reviewer_actor,
                worker_backend,
                reviewer_backend,
                args.timeout,
            )
    except ExecutorError as exc:
        print(json.dumps({"error": "adapter_stopped", "reason": str(exc)}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
