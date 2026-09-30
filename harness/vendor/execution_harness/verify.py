"""Fail-closed completion and approval binding verification."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .canonical import sha256_digest
from .models import ApprovalRequest, ArtifactRef, EvidenceReceipt, TaskContract


@dataclass(frozen=True)
class VerificationFinding:
    code: str
    detail: str


@dataclass(frozen=True)
class CompletionVerification:
    passed: bool
    findings: list[VerificationFinding]


def _now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value


def _local_artifact_matches(artifact: ArtifactRef) -> bool:
    """Recompute a file:// artifact checksum from one non-symlink descriptor."""
    parsed = urlparse(artifact.uri)
    if parsed.scheme != "file" or parsed.netloc:
        return False
    try:
        path = Path(unquote(parsed.path))
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            return False
        with os.fdopen(descriptor, "rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError:
        return False
    return artifact.sha256 == f"sha256:{digest}"


def _matches_any(artifact: ArtifactRef, candidates: list[ArtifactRef]) -> bool:
    """Match the complete declared reference, not an attacker-selected URI."""
    return any(artifact == candidate for candidate in candidates)


def verify_completion(
    contract: TaskContract, receipt: EvidenceReceipt, now: datetime | None = None
) -> CompletionVerification:
    """Determine eligibility only; never mutates an execution or queue state."""
    current = _now(now)
    findings: list[VerificationFinding] = []
    contract_digest = sha256_digest(contract)
    if receipt.contract_id != contract.contract_id:
        findings.append(VerificationFinding("CONTRACT_ID_MISMATCH", "receipt contract ID differs"))
    if receipt.contract_digest != contract_digest:
        findings.append(
            VerificationFinding("CONTRACT_DIGEST_MISMATCH", "receipt does not bind contract")
        )
    if receipt.execution_state != "verified":
        findings.append(
            VerificationFinding("RECEIPT_NOT_VERIFIED", "receipt state is not verified")
        )
    if receipt.finished_at > current or not (
        contract.created_at <= receipt.finished_at <= contract.expires_at
    ):
        findings.append(VerificationFinding("RECEIPT_OUTSIDE_LIFETIME", "receipt time is invalid"))
    if receipt.external_effects:
        findings.append(
            VerificationFinding("EXTERNAL_EFFECT", "local-only receipt records an effect")
        )
    if not (contract.created_at <= receipt.started_at <= contract.expires_at):
        findings.append(
            VerificationFinding("RECEIPT_START_OUTSIDE_LIFETIME", "receipt start is invalid")
        )
    expected = {criterion.criterion_id for criterion in contract.acceptance_criteria}
    actual = {item.criterion_id for item in receipt.criterion_evidence}
    if actual != expected:
        findings.append(
            VerificationFinding("CRITERIA_MISMATCH", "evidence IDs do not exactly match contract")
        )
    for evidence in receipt.criterion_evidence:
        if not (
            contract.created_at <= evidence.observed_at <= contract.expires_at
            and receipt.started_at <= evidence.observed_at <= receipt.finished_at
            and evidence.observed_at <= current
        ):
            findings.append(
                VerificationFinding("EVIDENCE_OUTSIDE_FRESHNESS_WINDOW", evidence.criterion_id)
            )
        if evidence.status != "PASS":
            findings.append(VerificationFinding("EVIDENCE_NOT_PASS", evidence.criterion_id))
        if evidence.producer == receipt.executor:
            findings.append(VerificationFinding("SELF_PRODUCED_EVIDENCE", evidence.criterion_id))
        if evidence.kind not in {"test", "artifact_digest", "manual_review", "external_verifier"}:
            findings.append(VerificationFinding("NON_INDEPENDENT_EVIDENCE", evidence.criterion_id))
        if evidence.kind == "artifact_digest":
            if evidence.artifact is None:
                findings.append(VerificationFinding("MISSING_ARTIFACT", evidence.criterion_id))
            elif not _matches_any(evidence.artifact, receipt.artifacts):
                findings.append(
                    VerificationFinding("ARTIFACT_NOT_RECEIPT_DECLARED", evidence.criterion_id)
                )
            elif not _matches_any(evidence.artifact, contract.deliverables):
                findings.append(
                    VerificationFinding("ARTIFACT_NOT_DELIVERABLE", evidence.criterion_id)
                )
            elif not _matches_any(evidence.artifact, contract.authority.workspace_refs):
                findings.append(
                    VerificationFinding("ARTIFACT_OUTSIDE_WORKSPACE", evidence.criterion_id)
                )
            elif not _local_artifact_matches(evidence.artifact):
                findings.append(
                    VerificationFinding("ARTIFACT_DIGEST_MISMATCH", evidence.criterion_id)
                )
    return CompletionVerification(passed=not findings, findings=findings)


def verify_approval_binding(
    contract: TaskContract,
    request: ApprovalRequest,
    action: str,
    target: str,
    payload: Any,
    executor: str,
    now: datetime | None = None,
) -> CompletionVerification:
    """Verify an exact requested operation; no fuzzy boundary matching occurs."""
    current = _now(now)
    findings: list[VerificationFinding] = []
    if request.contract_id != contract.contract_id:
        findings.append(VerificationFinding("DRIFT_CONTRACT_ID", "contract ID differs"))
    if request.contract_digest != sha256_digest(contract):
        findings.append(VerificationFinding("DRIFT_CONTRACT_DIGEST", "contract digest differs"))
    for code, given, expected in (
        ("DRIFT_ACTION", action, request.action),
        ("DRIFT_TARGET", target, request.target),
        ("DRIFT_EXECUTOR", executor, request.executor),
        ("DRIFT_PAYLOAD", sha256_digest(payload), request.payload_digest),
    ):
        if given != expected:
            findings.append(VerificationFinding(code, "requested binding differs"))
    if request.expires_at <= current:
        findings.append(VerificationFinding("APPROVAL_EXPIRED", "approval request expired"))
    if current < request.requested_at:
        findings.append(
            VerificationFinding("APPROVAL_NOT_YET_REQUESTED", "approval request is future-dated")
        )
    if not (
        contract.created_at <= request.requested_at
        and request.expires_at <= contract.expires_at
    ):
        findings.append(
            VerificationFinding(
                "APPROVAL_OUTSIDE_CONTRACT_LIFETIME",
                "approval request is outside contract lifetime",
            )
        )
    if request.risk_tier in {"R3", "R4"} and request.retry_allowed:
        findings.append(VerificationFinding("UNSAFE_RETRY", "high-risk request enables retry"))
    if not any(
        boundary.action == action
        and boundary.target == target
        and boundary.payload_digest == request.payload_digest
        for boundary in contract.action_boundaries
    ):
        findings.append(VerificationFinding("DRIFT_BOUNDARY", "no exact action boundary"))
    return CompletionVerification(passed=not findings, findings=findings)
