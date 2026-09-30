"""Adapt frozen work requirements to the inherited completion verifier."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import TaskSpec
from .vendor.execution_harness.canonical import sha256_digest
from .vendor.execution_harness.models import EvidenceReceipt, TaskContract
from .vendor.execution_harness.verify import verify_completion


def completion_evidence(task: dict[str, Any], reviewer: str, now: float) -> dict[str, Any]:
    spec = TaskSpec.model_validate(task["spec"])
    workspace = Path(task["workspace"])
    start = datetime.fromtimestamp(task["created_at"], UTC)
    finish = datetime.fromtimestamp(now, UTC)
    artifact_refs = [
        {
            "artifact_id": f"file-{index}",
            "uri": (workspace / name).as_uri(),
            "sha256": "sha256:" + hashlib.sha256((workspace / name).read_bytes()).hexdigest(),
            "media_type": "application/octet-stream",
        }
        for index, name in enumerate(task["changed_paths"])
        if (workspace / name).is_file()
    ]
    # Deletions and empty-change tasks still bind to the full final revision.
    frozen_ref = {
        "artifact_id": "work-spec",
        "uri": "work-spec.json",
        "sha256": "sha256:" + task["spec_digest"],
        "media_type": "application/json",
    }
    deliverables = artifact_refs or [frozen_ref]
    criteria = [
        {
            "criterion_id": check.id,
            "statement": check.statement,
            "verification_method": "controller-owned fixed argv on pinned revision",
        }
        for check in spec.checks
    ]
    criteria.append(
        {
            "criterion_id": "independent-review",
            "statement": "Independent review",
            "verification_method": "distinct session covers all acceptance criteria",
        }
    )
    contract = TaskContract.model_validate(
        {
            "contract_id": spec.id,
            "protocol_version": "v1",
            "created_at": start,
            "expires_at": datetime.fromtimestamp(task["expires_at"], UTC),
            "requester": "local-controller",
            "task_title": spec.goal,
            "goal": spec.goal,
            "scope_in": spec.allowed_paths,
            "scope_out": ["external effects", "protected paths"],
            "source_refs": [frozen_ref],
            "deliverables": deliverables,
            "acceptance_criteria": criteria,
            "action_boundaries": [
                {
                    "action_id": "local-edit",
                    "action": "write_file",
                    "target": str(workspace),
                    "payload_digest": "sha256:" + task["spec_digest"],
                    "authority": "human-requested bounded local pilot",
                }
            ],
            "authority": {
                "allowed_actions": ["write_file"],
                "forbidden_actions": ["network", "credential_access", "external_write"],
                "workspace_refs": deliverables,
            },
            "resource_limits": {
                "max_runtime_seconds": min(spec.lifetime_seconds, 86400),
                "max_output_bytes": 1048576,
            },
            "classification": "internal",
        }
    )
    items = [
        {
            "criterion_id": check["id"],
            "status": "PASS",
            "kind": "test",
            "producer": "controller-verifier",
            "observed_at": finish,
            "summary": "fixed argv passed on " + task["verified_revision"],
        }
        for check in task["checks"]
    ]
    items.append(
        {
            "criterion_id": "independent-review",
            "status": "PASS",
            "kind": "manual_review",
            "producer": reviewer,
            "observed_at": finish,
            "summary": "distinct session reviewed pinned revision",
        }
    )
    receipt = EvidenceReceipt.model_validate(
        {
            "receipt_id": "receipt-" + task["last_worker_token"],
            "contract_id": spec.id,
            "contract_digest": sha256_digest(contract),
            "run_id": task["last_worker_token"],
            "executor": task["worker"],
            "started_at": start,
            "finished_at": finish,
            "execution_state": "verified",
            "criterion_evidence": items,
            "artifacts": artifact_refs,
            "external_effects": [],
            "residual_risks": [],
            "self_report": None,
        }
    )
    result = verify_completion(contract, receipt, now=finish)
    return {
        "passed": result.passed,
        "findings": [finding.code for finding in result.findings],
        "contract": contract.model_dump(mode="json"),
        "receipt": receipt.model_dump(mode="json"),
        "revision": task["verified_revision"],
    }
