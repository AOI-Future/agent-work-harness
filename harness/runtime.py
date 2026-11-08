"""Local work lifecycle, independent evidence and bounded recovery."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .checks import run_check
from .models import ReviewReport, TaskSpec, WorkerReport
from .protocol import completion_evidence
from .store import Store
from .workspace import (
    HarnessError,
    changes,
    copy_workspace,
    git,
    revision,
    safe_repo,
    snapshot,
    within,
)


class Harness:
    def __init__(self, root: Path, clock: Callable[[], float] = time.time) -> None:
        self.store = Store(root)
        self.clock = clock

    def close(self) -> None:
        self.store.connection.close()

    def _save(self, task: dict[str, Any], kind: str, details: dict[str, Any]) -> None:
        notify = kind in {"completed", "waiting_human", "escalated"}
        self.store.put(task)
        self.store.event(task["spec"]["id"], self.clock(), kind, details, notify)

    def submit(self, spec: TaskSpec) -> dict[str, Any]:
        canonical = spec.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
        with self.store.transaction():
            if self.store.exists(spec.id):
                task = self.store.get(spec.id)
                if task["spec_digest"] != digest:
                    raise HarnessError("task ID already belongs to another immutable contract")
                return self._view(task)
            source = safe_repo(spec.source_repo)
            if git(source, "status", "--porcelain"):
                raise HarnessError("source repository is dirty; choose a clean snapshot")
            if source == self.store.root or self.store.root.is_relative_to(source):
                raise HarnessError("runtime must live outside the source repository")
            if spec.base_ref.startswith("-"):
                raise HarnessError("base_ref may not be a git option")
            base = (
                git(source, "rev-parse", "--verify", spec.base_ref + "^{commit}").decode().strip()
            )
            for dependency in spec.depends_on:
                if not self.store.exists(dependency):
                    raise HarnessError("dependencies must be registered before their consumers")
            now = self.clock()
            baseline_root = self.store.root / "workspaces" / spec.id / "baseline"
            if baseline_root.exists():
                # A crashed submit can leave a directory without a DB record.
                baseline_root = baseline_root.with_name("baseline-" + uuid.uuid4().hex)
            copy_workspace(source, base, baseline_root, None)
            baseline = snapshot(baseline_root)
            for name in spec.context_paths:
                if not (baseline_root / name).is_file():
                    raise HarnessError("context reference must identify a checked-in regular file")
            protected = spec.protected_paths + spec.context_paths
            if any(
                within(path, protected) or within(root, spec.allowed_paths)
                for path in spec.allowed_paths
                for root in protected
            ):
                raise HarnessError("allowed and protected paths may not overlap")
            task = {
                "spec": canonical,
                "spec_digest": digest,
                "source": str(source),
                "base": base,
                "created_at": now,
                "expires_at": now + spec.lifetime_seconds,
                "state": "waiting_human" if spec.requires_human else "ready",
                "baseline": baseline,
                "workspace": str(baseline_root),
                "lease": None,
                "attempts": 0,
                "review_attempts": 0,
                "failures": [],
                "verified_revision": None,
                "checks": [],
                "changed_paths": [],
                "worker": None,
                "last_worker_token": None,
                "authors": [],
                "verification_lease": None,
                "evidence": None,
                "human_count": 0,
                "human_seconds": 0,
                "reported_cost": None,
                "patch": None,
                "external_revision": None,
                "integrated_at": None,
            }
            self._save(task, task["state"] if spec.requires_human else "submitted", {})
            return self._view(task)

    def advance(self, task_id: str, command: dict[str, Any]) -> dict[str, Any]:
        if command.get("action") == "verify":
            return self._verify_task(task_id)
        with self.store.transaction():
            task = self.store.get(task_id)
            spec = TaskSpec.model_validate(task["spec"])
            action = command.get("action")
            self._fresh(task)
            if action == "claim":
                return self._claim(task, spec, command)
            if action == "report":
                report = WorkerReport.model_validate(command["report"])
                lease = self._lease(task, report.token, "worker")
                task["workspace"] = lease["workspace"]
                task["worker"] = lease["actor"]
                task["last_worker_token"] = lease["token"]
                task["lease"] = None
                if report.outcome == "needs_decision":
                    task["state"] = "waiting_human"
                    self._save(task, "waiting_human", {"reason": "worker_needs_decision"})
                else:
                    task["state"] = "verifying"
                    self._save(task, "implementation_reported", {})
            elif action == "review":
                review_report = ReviewReport.model_validate(command["report"])
                self._review(task, spec, review_report)
            elif action == "recover":
                self._recover(task, spec)
            elif action == "intervention":
                seconds = command.get("seconds")
                cost = command.get("cost")
                if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
                    raise HarnessError("human seconds must be numeric")
                if not 0 <= seconds <= 86400:
                    raise HarnessError("human seconds must be finite and within 0..86400")
                if cost is not None and (
                    type(cost) not in {int, float} or not 0 <= cost <= 1000000
                ):
                    raise HarnessError("cost must be finite and within 0..1000000")
                task["human_count"] += 1
                task["human_seconds"] += seconds
                if cost is not None:
                    task["reported_cost"] = (task["reported_cost"] or 0) + cost
                self._save(task, "intervention", {"seconds": seconds, "cost": cost})
            else:
                raise HarnessError("unknown controller operation")
            return self._view(task)

    def _fresh(self, task: dict[str, Any]) -> None:
        if task["state"] == "completed":
            try:
                matches = revision(Path(task["workspace"])) == task["verified_revision"]
            except HarnessError:
                matches = False
            if not matches:
                task["state"] = "waiting_human"
                task["evidence"] = None
                self._save(task, "waiting_human", {"reason": "completed_revision_changed"})
        if self.clock() >= task["expires_at"] and task["state"] not in {
            "completed",
            "waiting_human",
            "escalated",
            "integrate_ready",
            "integrated",
            "closed",
        }:
            task["state"] = "waiting_human"
            task["lease"] = None
            task["verification_lease"] = None
            self._save(task, "waiting_human", {"reason": "task_expired"})

    def _dependencies_ready(self, spec: TaskSpec) -> bool:
        for dependency in spec.depends_on:
            parent = self.store.get(dependency)
            self._fresh(parent)
            if parent["state"] != "completed":
                return False
        return True

    def _claim(
        self, task: dict[str, Any], spec: TaskSpec, command: dict[str, Any]
    ) -> dict[str, Any]:
        actor, role = command.get("actor"), command.get("role")
        if not isinstance(actor, str) or not actor or len(actor) > 80:
            raise HarnessError("require a non-empty local actor identifier")
        if actor == "controller-verifier":
            raise HarnessError("reserved controller identity")
        if role not in {"worker", "reviewer"}:
            raise HarnessError("unknown executor role")
        expected = "ready" if role == "worker" else "review_ready"
        if task["state"] != expected or task["lease"] is not None:
            raise HarnessError("task is not claimable in this role")
        if not self._dependencies_ready(spec):
            raise HarnessError("dependency is incomplete or its evidence is stale")
        authors = task.get("authors", [task["worker"]] if task["worker"] else [])
        if role == "reviewer" and actor in authors:
            raise HarnessError("reviewer must differ from every implementation actor")
        if role == "worker" and task["attempts"] >= spec.max_attempts:
            raise HarnessError("attempt budget exhausted")
        if role == "reviewer" and revision(Path(task["workspace"])) != task["verified_revision"]:
            self._retry(task, spec, "revision_changed_before_review")
            return self._view(task)
        token = "lease-" + uuid.uuid4().hex
        workspace = self.store.root / "workspaces" / spec.id / token
        copy_workspace(Path(task["source"]), task["base"], workspace, Path(task["workspace"]))
        tier = (
            "strong"
            if role == "reviewer"
            or spec.complexity == "design"
            or task["attempts"] >= spec.small_attempts
            else "small"
        )
        lease = {
            "token": token,
            "actor": actor,
            "role": role,
            "workspace": str(workspace),
            "expires_at": min(self.clock() + spec.lease_seconds, task["expires_at"]),
            "revision": revision(workspace),
            "tier": tier,
            "started_at": self.clock(),
        }
        task["lease"] = lease
        if role == "worker":
            task["authors"] = sorted(set(authors + [actor]))
        task["state"] = "implementing" if role == "worker" else "reviewing"
        task["attempts" if role == "worker" else "review_attempts"] += 1
        self._save(
            task,
            "claimed",
            {"actor": actor, "role": role, "tier": tier, "attempt": task["attempts"]},
        )
        return {
            "schema_version": "v1",
            "task_id": spec.id,
            "spec_digest": task["spec_digest"],
            "lease": lease,
            "goal": spec.goal,
            "base_commit": task["base"],
            "allowed_paths": spec.allowed_paths if role == "worker" else [],
            "protected_paths": spec.protected_paths + spec.context_paths,
            "context_paths": spec.context_paths,
            "criteria": [{"id": check.id, "statement": check.statement} for check in spec.checks],
            "checks": task["checks"] if role == "reviewer" else [],
            "failures": task["failures"],
            "target_revision": task["verified_revision"],
            "forbidden_actions": [
                "network",
                "external_write",
                "credential_change",
                "production",
                "modify_controller",
                "change_acceptance",
            ],
            "next": "return WorkerReport" if role == "worker" else "return ReviewReport",
        }

    def _lease(self, task: dict[str, Any], token: str, role: str) -> dict[str, Any]:
        lease = task["lease"]
        if not lease or lease["token"] != token or lease["role"] != role:
            raise HarnessError("stale or incorrect lease token/role")
        if self.clock() >= lease["expires_at"]:
            raise HarnessError("lease expired; recover through the controller")
        return dict(lease)

    def _retry(
        self, task: dict[str, Any], spec: TaskSpec, reason: str, findings: list[str] | None = None
    ) -> None:
        task["lease"] = None
        task["verification_lease"] = None
        task["verified_revision"] = None
        task["checks"] = []
        task["evidence"] = None
        task["failures"].append({"reason": reason, "findings": findings or []})
        task["state"] = "escalated" if task["attempts"] >= spec.max_attempts else "ready"
        self._save(
            task,
            "escalated" if task["state"] == "escalated" else "changes_requested",
            {"reason": reason},
        )

    def _scope(self, task: dict[str, Any], spec: TaskSpec, workspace: Path) -> list[str]:
        changed = changes(task["baseline"], snapshot(workspace))
        protected = spec.protected_paths + spec.context_paths
        if any(not within(name, spec.allowed_paths) or within(name, protected) for name in changed):
            raise HarnessError("implementation changed files outside its frozen scope")
        return changed

    def _verify_task(self, task_id: str) -> dict[str, Any]:
        # Reserve only the verifier in a short transaction; commands run without a DB lock.
        with self.store.transaction():
            task = self.store.get(task_id)
            self._fresh(task)
            spec = TaskSpec.model_validate(task["spec"])
            if task["state"] != "verifying" or task.get("verification_lease"):
                raise HarnessError("verification requires an unclaimed reported implementation")
            workspace = Path(task["workspace"])
            try:
                task["changed_paths"] = self._scope(task, spec, workspace)
                before = revision(workspace)
            except HarnessError:
                task["state"] = "waiting_human"
                self._save(task, "waiting_human", {"reason": "scope_or_workspace_violation"})
                return self._view(task)
            token = "verify-" + uuid.uuid4().hex
            deadline = min(
                self.clock() + sum(c.timeout_seconds for c in spec.checks) + 60, task["expires_at"]
            )
            task["verification_lease"] = {"token": token, "expires_at": deadline}
            self._save(task, "verification_started", {"revision": before})
        validation = self.store.root / "workspaces" / task_id / token
        try:
            # The clean controller copy excludes all worker-produced caches.
            copy_workspace(Path(task["source"]), task["base"], validation, workspace)
            validation_before = revision(validation)
            same_input = snapshot(validation) == snapshot(workspace)
            results = [run_check(check, validation) for check in spec.checks]
            unchanged = (
                same_input
                and revision(workspace) == before
                and revision(validation) == validation_before
            )
        except (HarnessError, OSError):
            results = []
            unchanged = False
        with self.store.transaction():
            task = self.store.get(task_id)
            self._fresh(task)
            reservation = task.get("verification_lease")
            if not reservation or reservation["token"] != token or task["state"] != "verifying":
                return self._view(task)  # Recovered/expired runs cannot publish late results.
            task["verification_lease"] = None
            if self.clock() >= deadline:
                self._retry(task, spec, "verification_lease_expired")
            elif not unchanged:
                self._retry(task, spec, "verification_changed_revision")
            elif not results or not all(result["passed"] for result in results):
                self._retry(
                    task,
                    spec,
                    "check_failed",
                    [r["id"] + ":" + r["reason"] for r in results if not r["passed"]],
                )
            else:
                task["checks"] = results
                task["verified_revision"] = before
                task["state"] = "review_ready"
                self._save(task, "verification_passed", {"revision": before, "checks": results})
            return self._view(task)

    def _review(self, task: dict[str, Any], spec: TaskSpec, report: ReviewReport) -> None:
        lease = self._lease(task, report.token, "reviewer")
        if report.revision != task["verified_revision"]:
            raise HarnessError("review does not bind the verified implementation revision")
        try:
            implementation_unchanged = revision(Path(task["workspace"])) == report.revision
            review_unchanged = revision(Path(lease["workspace"])) == lease["revision"]
        except HarnessError:
            implementation_unchanged = review_unchanged = False
        if not implementation_unchanged or not review_unchanged:
            self._retry(task, spec, "revision_changed_during_review")
            return
        covered = report.covered_criteria
        if len(covered) != len(set(covered)) or set(covered) != {check.id for check in spec.checks}:
            raise HarnessError("review must cover each frozen acceptance criterion exactly once")
        if report.verdict == "needs_decision":
            task["state"] = "waiting_human"
            task["lease"] = None
            self._save(task, "waiting_human", {"reason": "review_needs_decision"})
        elif report.verdict == "changes":
            if not report.findings:
                raise HarnessError("changes verdict requires actionable findings")
            self._retry(task, spec, "review_changes", report.findings)
        else:
            if report.findings:
                raise HarnessError("pass verdict cannot retain unresolved findings")
            evidence = completion_evidence(task, lease["actor"], self.clock())
            if not evidence["passed"]:
                self._retry(task, spec, "protocol_verification_failed", evidence["findings"])
                return
            task["evidence"] = evidence
            task["state"] = "completed"
            task["lease"] = None
            task["completed_at"] = self.clock()
            self._save(task, "completed", {"revision": report.revision, "reviewer": lease["actor"]})

    def _recover(self, task: dict[str, Any], spec: TaskSpec) -> None:
        verification = task.get("verification_lease")
        if verification:
            if self.clock() < verification["expires_at"]:
                raise HarnessError("verification lease is still active")
            self._retry(task, spec, "verification_lease_expired")
            return
        lease = task["lease"]
        if not lease or self.clock() < lease["expires_at"]:
            raise HarnessError("recovery requires an expired active lease")
        # Never reuse an expired worker's workspace; its old process may still be alive.
        task["lease"] = None
        if lease["role"] == "reviewer":
            if task["review_attempts"] >= spec.max_attempts:
                task["state"] = "escalated"
                self._save(task, "escalated", {"reason": "review_recovery_budget_exhausted"})
            else:
                task["state"] = "review_ready"
                self._save(task, "lease_recovered", {"role": "reviewer"})
        else:
            self._retry(task, spec, "worker_lease_expired")

    def _view(self, task: dict[str, Any]) -> dict[str, Any]:
        state = task["state"]
        return {
            "task_id": task["spec"]["id"],
            "state": state,
            "spec_digest": task["spec_digest"],
            "base_commit": task["base"],
            "workspace": task["workspace"],
            "revision": task["verified_revision"],
            "attempts": task["attempts"],
            "review_attempts": task["review_attempts"],
            "checks": task["checks"],
            "changed_paths": task["changed_paths"],
            "failures": task["failures"],
            "evidence_passed": bool(task["evidence"]),
            "patch": task.get("patch"),
            "closed_reason": task.get("closed_reason"),
            "external_revision": task.get("external_revision"),
            "integrated_at": task.get("integrated_at"),
            "human_interventions": task["human_count"],
            "human_seconds": task["human_seconds"],
            "reported_cost": task["reported_cost"],
            "elapsed_seconds": round(
                task.get("completed_at", self.clock()) - task["created_at"], 3
            ),
            "lease_expires_at": task["lease"]["expires_at"] if task["lease"] else None,
            "next": {
                "ready": "claim worker",
                "implementing": "await worker report",
                "verifying": "await verifier" if task.get("verification_lease") else "verify",
                "review_ready": "claim independent reviewer",
                "reviewing": "await review report",
                "completed": "inspect local patch",
                "integrate_ready": "human pushes upstream, then mark-integrated",
                "integrated": "contract closed at an external revision",
                "waiting_human": "human decision required; execution stopped",
                "escalated": "budget exhausted; human decision required",
                "closed": "terminal; closed by human decision",
            }[state],
        }

    def inspect(self, task_id: str) -> dict[str, Any]:
        with self.store.transaction():
            task = self.store.get(task_id)
            self._fresh(task)
            return self._view(task)

    def human_close(
        self,
        task_id: str,
        reason: str,
        external_revision: str | None = None,
    ) -> dict[str, Any]:
        # Terminal human decision for gated tasks settled outside the pipeline
        # (shipped, abandoned, or superseded). Never touches active leases.
        if not reason or len(reason) > 500:
            raise HarnessError("close reason is required and must be at most 500 characters")
        if external_revision is not None and not re.fullmatch(r"[0-9a-f]{7,40}", external_revision):
            raise HarnessError("external revision must be a lowercase git sha (7-40 hex)")
        with self.store.transaction():
            task = self.store.get(task_id)
            self._fresh(task)
            if task["state"] not in {"waiting_human", "escalated", "completed", "integrate_ready"}:
                raise HarnessError("only gated or finished tasks may be closed")
            task["state"] = "closed"
            task["closed_reason"] = reason
            if external_revision:
                task["external_revision"] = external_revision
            self._save(task, "closed", {"reason": reason, "external_revision": external_revision})
            return self._view(task)

    def integration_patch(self, task_id: str) -> dict[str, Any]:
        # task_id composes on-disk paths; re-check the contract identifier here.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", task_id):
            raise HarnessError("invalid task id for patch path")
        # Freeze the verified workspace, then build the patch outside any DB lock.
        with self.store.transaction():
            task = self.store.get(task_id)
            self._fresh(task)
            if task["state"] != "completed":
                raise HarnessError("integration patch requires a completed task")
            if revision(Path(task["workspace"])) != task["verified_revision"]:
                raise HarnessError("completed revision changed; no patch may be produced")
            workspace, base, source = Path(task["workspace"]), task["base"], Path(task["source"])
        staging = self.store.root / "patches" / task_id / ("stage-" + uuid.uuid4().hex)
        # A disposable clean clone carries the verified worktree, so the frozen
        # workspace is never mutated and new files still enter the patch.
        copy_workspace(source, base, staging, workspace)
        git(staging, "add", "-A")
        diff = git(staging, "diff", "--cached", "--binary", "--text", "--no-ext-diff")
        if not diff.strip():
            raise HarnessError("verified patch is empty")
        patch_path = self.store.root / "patches" / task_id / (task_id + ".patch")
        patch_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        patch_path.write_bytes(diff)
        patch = {
            "path": str(patch_path),
            "sha256": hashlib.sha256(diff).hexdigest(),
            "bytes": len(diff),
            "revision": task["verified_revision"],
        }
        with self.store.transaction():
            task = self.store.get(task_id)
            self._fresh(task)
            if task["state"] != "completed":
                raise HarnessError("task left completed state during patch generation")
            if revision(Path(task["workspace"])) != task["verified_revision"]:
                raise HarnessError("completed revision changed during patch generation")
            task["patch"] = patch
            task["state"] = "integrate_ready"
            self._save(task, "integrate_ready", patch)
            return self._view(task)

    def mark_integrated(self, task_id: str, external_revision: str) -> dict[str, Any]:
        # The human push happened outside the harness; this records its receipt.
        if not re.fullmatch(r"[0-9a-f]{7,40}", external_revision):
            raise HarnessError("external revision must be a lowercase git sha (7-40 hex)")
        with self.store.transaction():
            task = self.store.get(task_id)
            if task["state"] != "integrate_ready":
                raise HarnessError("mark-integrated requires an integrate_ready task")
            patch = task.get("patch")
            if not patch or not Path(patch["path"]).is_file():
                raise HarnessError("patch file is missing; integration cannot be recorded")
            if hashlib.sha256(Path(patch["path"]).read_bytes()).hexdigest() != patch["sha256"]:
                raise HarnessError("patch digest mismatch; refusing to record integration")
            task["state"] = "integrated"
            task["external_revision"] = external_revision
            task["integrated_at"] = self.clock()
            self._save(
                task,
                "integrated",
                {"external_revision": external_revision, "patch_sha256": patch["sha256"]},
            )
            return self._view(task)
