"""Strict, local-only Phase 1 execution-harness protocol models."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .state import HandoffState

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Text = Annotated[str, Field(min_length=1, max_length=4000)]
Action = Annotated[str, Field(min_length=1, max_length=256)]


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class _TimeBoundModel(ProtocolModel):
    @field_validator(
        "created_at",
        "expires_at",
        "requested_at",
        "started_at",
        "finished_at",
        "observed_at",
        "retrieved_at",
        check_fields=False,
    )
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value


def _local_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.username or parsed.password or parsed.scheme not in {"", "file"}:
        raise ValueError("URI must be a relative or file:// reference without userinfo")
    if parsed.scheme == "file" and parsed.netloc:
        raise ValueError("file URI may not name a host")
    if parsed.scheme == "" and uri.startswith("/"):
        raise ValueError("absolute paths are not allowed")
    return uri


def _safe_values(values: list[str], label: str) -> list[str]:
    if not values or any(not value or value == "*" for value in values):
        raise ValueError(f"{label} must be non-empty and may not contain wildcard")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} values must be unique")
    return values


class ArtifactRef(ProtocolModel):
    artifact_id: Identifier
    uri: Annotated[str, Field(min_length=1, max_length=2048)]
    sha256: Digest
    media_type: Annotated[str, Field(min_length=1, max_length=256)]

    _valid_uri = field_validator("uri")(_local_uri)


class AcceptanceCriterion(ProtocolModel):
    criterion_id: Identifier
    statement: Text
    verification_method: Text


class ActionBoundary(ProtocolModel):
    action_id: Identifier
    action: Action
    target: Annotated[str, Field(min_length=1, max_length=2048)]
    payload_digest: Digest
    authority: Text

    @field_validator("action", "target")
    @classmethod
    def no_wildcard(cls, value: str) -> str:
        if value == "*":
            raise ValueError("wildcards are not permitted")
        return value


class AuthorityBoundary(ProtocolModel):
    allowed_actions: list[Action]
    forbidden_actions: list[Action]
    workspace_refs: list[ArtifactRef]

    @model_validator(mode="after")
    def safe_authority(self) -> AuthorityBoundary:
        _safe_values(self.allowed_actions, "allowed_actions")
        _safe_values(self.forbidden_actions, "forbidden_actions")
        if set(self.allowed_actions) & set(self.forbidden_actions):
            raise ValueError("allowed and forbidden actions must be disjoint")
        required = {"network", "credential_access", "external_write"}
        if not required <= set(self.forbidden_actions):
            raise ValueError(
                "local contract must forbid network, credential_access, external_write"
            )
        if not self.workspace_refs:
            raise ValueError("workspace_refs must be non-empty")
        return self


class ResourceLimits(ProtocolModel):
    max_runtime_seconds: Annotated[int, Field(ge=1, le=86400)]
    max_output_bytes: Annotated[int, Field(ge=1, le=1048576)]


class ContextReference(ProtocolModel):
    reference_id: Identifier
    uri: Annotated[str, Field(min_length=1, max_length=2048)]
    sha256: Digest
    classification: Literal["public", "internal", "confidential"]
    purpose: Text
    retrieved_at: datetime

    _valid_uri = field_validator("uri")(_local_uri)
    _aware_retrieved = field_validator("retrieved_at")(_TimeBoundModel.aware_time.__func__)


class EvidenceItem(ProtocolModel):
    criterion_id: Identifier
    status: Literal["PASS", "FAIL"]
    kind: Literal["test", "artifact_digest", "manual_review", "external_verifier", "self_report"]
    producer: Identifier
    observed_at: datetime
    summary: Text
    artifact: ArtifactRef | None = None

    _aware_observed = field_validator("observed_at")(_TimeBoundModel.aware_time.__func__)

    @model_validator(mode="after")
    def artifact_digest_has_artifact(self) -> EvidenceItem:
        if self.kind == "artifact_digest" and self.artifact is None:
            raise ValueError("artifact_digest evidence needs artifact")
        return self


class ExternalEffect(ProtocolModel):
    effect_id: Identifier
    summary: Text


class TaskContract(_TimeBoundModel):
    contract_id: Identifier
    protocol_version: Literal["v1"]
    created_at: datetime
    expires_at: datetime
    requester: Identifier
    task_title: Text
    goal: Text
    scope_in: list[Text]
    scope_out: list[Text]
    source_refs: list[ArtifactRef]
    deliverables: list[ArtifactRef]
    acceptance_criteria: list[AcceptanceCriterion]
    action_boundaries: list[ActionBoundary]
    authority: AuthorityBoundary
    resource_limits: ResourceLimits
    classification: Literal["public", "internal", "confidential"]

    @model_validator(mode="after")
    def valid_contract(self) -> TaskContract:
        if self.expires_at <= self.created_at:
            raise ValueError("expiry must be after creation")
        for label, values in (("scope_in", self.scope_in), ("scope_out", self.scope_out)):
            if not values:
                raise ValueError(f"{label} must be non-empty")
        for label, values in (
            ("source_refs", self.source_refs),
            ("deliverables", self.deliverables),
            ("acceptance_criteria", self.acceptance_criteria),
            ("action_boundaries", self.action_boundaries),
        ):
            if not values:
                raise ValueError(f"{label} must be non-empty")
        criterion_ids = [item.criterion_id for item in self.acceptance_criteria]
        action_ids = [item.action_id for item in self.action_boundaries]
        if len(criterion_ids) != len(set(criterion_ids)) or len(action_ids) != len(set(action_ids)):
            raise ValueError("criterion and action IDs must be unique")
        if any(
            item.action not in self.authority.allowed_actions for item in self.action_boundaries
        ):
            raise ValueError("boundary action is not allowed")
        return self


class ContextPack(_TimeBoundModel):
    context_id: Identifier
    contract_digest: Digest
    created_at: datetime
    expires_at: datetime
    classification: Literal["public", "internal", "confidential"]
    references: list[ContextReference]

    @model_validator(mode="after")
    def valid_context(self) -> ContextPack:
        levels = {"public": 0, "internal": 1, "confidential": 2}
        if self.expires_at <= self.created_at or not self.references:
            raise ValueError("context requires future expiry and references")
        if any(item.retrieved_at > self.created_at for item in self.references):
            raise ValueError("context reference cannot be retrieved after pack creation")
        if any(
            levels[item.classification] > levels[self.classification] for item in self.references
        ):
            raise ValueError("context reference classification exceeds pack")
        return self


class EvidenceReceipt(_TimeBoundModel):
    receipt_id: Identifier
    contract_id: Identifier
    contract_digest: Digest
    run_id: Identifier
    executor: Identifier
    started_at: datetime
    finished_at: datetime
    execution_state: Literal["verified", "failed"]
    criterion_evidence: list[EvidenceItem]
    artifacts: list[ArtifactRef]
    external_effects: list[ExternalEffect]
    residual_risks: list[Text]
    self_report: Annotated[str, Field(max_length=4000)] | None = None

    @model_validator(mode="after")
    def valid_receipt(self) -> EvidenceReceipt:
        if self.finished_at < self.started_at:
            raise ValueError("receipt finished before start")
        if self.execution_state == "verified" and not self.criterion_evidence:
            raise ValueError("verified receipt needs criterion evidence")
        ids = [item.criterion_id for item in self.criterion_evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("receipt criterion IDs must be unique")
        if self.external_effects:
            raise ValueError("external effects are prohibited in Phase 1")
        return self


class ApprovalRequest(_TimeBoundModel):
    approval_id: Identifier
    contract_id: Identifier
    contract_digest: Digest
    requested_by: Identifier
    executor: Identifier
    action: Action
    target: Annotated[str, Field(min_length=1, max_length=2048)]
    payload_digest: Digest
    requested_at: datetime
    expires_at: datetime
    risk_tier: Literal["R0", "R1", "R2", "R3", "R4"]
    retry_allowed: bool
    single_use: Literal[True]
    idempotency_key: Identifier
    rollback_ref: ArtifactRef | None

    @model_validator(mode="after")
    def valid_approval(self) -> ApprovalRequest:
        if self.expires_at <= self.requested_at or self.action == "*" or self.target == "*":
            raise ValueError("approval expiry/action/target invalid")
        if self.risk_tier in {"R3", "R4"}:
            if self.retry_allowed or self.rollback_ref is None:
                raise ValueError("R3/R4 requires rollback and disallows retry")
            if self.expires_at - self.requested_at > timedelta(minutes=15):
                raise ValueError("R3/R4 expiry cannot exceed 15 minutes")
        return self


class HandoffEnvelope(_TimeBoundModel):
    handoff_id: Identifier
    contract_id: Identifier
    contract_digest: Digest
    source_node: Identifier
    issuer_runtime_id: Identifier
    target_node: Identifier
    state: HandoffState
    created_at: datetime
    expires_at: datetime
    capabilities: list[Action]
    forbidden_actions: list[Action]
    expected_acknowledgement: Literal["claim", "reject", "receipt"]
    context_pack_digest: Digest

    @model_validator(mode="after")
    def valid_handoff(self) -> HandoffEnvelope:
        if self.source_node == self.target_node or self.expires_at <= self.created_at:
            raise ValueError("handoff source/target or expiry invalid")
        _safe_values(self.capabilities, "capabilities")
        _safe_values(self.forbidden_actions, "forbidden_actions")
        if set(self.capabilities) & set(self.forbidden_actions):
            raise ValueError("handoff capabilities and forbidden actions overlap")
        return self
