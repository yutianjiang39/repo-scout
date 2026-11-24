from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .common import identifier, safe_relative, slug


class FindingState(str, Enum):
    CANDIDATE = "CANDIDATE"
    NEEDS_VALIDATION = "NEEDS_VALIDATION"
    CC_VALIDATING = "CC_VALIDATING"
    CONFIRMED = "CONFIRMED"
    PROBABLE = "PROBABLE"
    UNVERIFIED = "UNVERIFIED"
    REJECTED = "REJECTED"
    ALREADY_FIXED = "ALREADY_FIXED"
    DUPLICATE = "DUPLICATE"
    DESIGN_DECISION = "DESIGN_DECISION"
    BLOCKED = "BLOCKED"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED_TO_SUBMIT = "APPROVED_TO_SUBMIT"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    MERGED = "MERGED"
    ACCEPTED = "ACCEPTED"


TRANSITIONS: dict[str, set[str]] = {
    "CANDIDATE": {"NEEDS_VALIDATION", "CC_VALIDATING"},
    "NEEDS_VALIDATION": {"CC_VALIDATING"},
    "CC_VALIDATING": {"CONFIRMED", "PROBABLE", "UNVERIFIED", "REJECTED", "ALREADY_FIXED", "DUPLICATE", "DESIGN_DECISION", "BLOCKED"},
    "PROBABLE": {"CC_VALIDATING"}, "UNVERIFIED": {"CC_VALIDATING"}, "BLOCKED": {"CC_VALIDATING"},
    "CONFIRMED": {"HUMAN_APPROVED", "CC_VALIDATING", "REJECTED"},
    "HUMAN_APPROVED": {"IMPLEMENTING", "READY_FOR_REVIEW", "CC_VALIDATING"},
    "IMPLEMENTING": {"TESTING", "BLOCKED"},
    "TESTING": {"IMPLEMENTING", "READY_FOR_REVIEW", "BLOCKED"},
    "READY_FOR_REVIEW": {"APPROVED_TO_SUBMIT", "IMPLEMENTING", "TESTING", "CC_VALIDATING"},
    "APPROVED_TO_SUBMIT": {"SUBMITTED", "READY_FOR_REVIEW", "TESTING", "CC_VALIDATING"},
    "SUBMITTED": {"UNDER_REVIEW", "CHANGES_REQUESTED", "MERGED", "ACCEPTED", "REJECTED"},
    "UNDER_REVIEW": {"CHANGES_REQUESTED", "MERGED", "ACCEPTED", "REJECTED"},
    "CHANGES_REQUESTED": {"HUMAN_APPROVED", "CC_VALIDATING", "UNDER_REVIEW", "MERGED", "REJECTED"},
    "MERGED": set(), "ACCEPTED": set(), "REJECTED": set(), "ALREADY_FIXED": set(),
    "DUPLICATE": set(), "DESIGN_DECISION": set(),
}


def validate_transition(old: str, new: str) -> None:
    if old != new and new not in TRANSITIONS.get(old, set()):
        raise ValueError(f"Illegal finding transition {old} -> {new}")


class Finding(BaseModel):
    model_config = ConfigDict(extra="allow")
    finding_id: str
    repository: str
    title: str = Field(min_length=5, max_length=240)
    category: str = "correctness"
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    confidence: Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"] = "UNKNOWN"
    evidence_status: Literal["CONFIRMED", "INFERRED", "UNKNOWN"] = "INFERRED"
    file_path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    evidence: list[Any] = Field(default_factory=list)
    trigger_condition: str = ""
    expected_behavior: str = ""
    actual_behavior: str = ""
    impact: str = ""
    proposed_fix: str = ""
    test_plan: list[str] = Field(default_factory=list)
    status: FindingState = FindingState.CANDIDATE
    provenance: Literal["live", "imported", "demo"] = "imported"

    _slug = field_validator("repository")(slug)
    _id = field_validator("finding_id")(identifier)
    _path = field_validator("file_path")(safe_relative)

    @model_validator(mode="after")
    def check_lines(self) -> Finding:
        if self.line_end < self.line_start:
            raise ValueError("line_end precedes line_start")
        return self


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_id: str
    validator: Literal["claude-code"] = "claude-code"
    session_id: str = Field(min_length=1)
    source_sha: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    result: Literal["CONFIRMED", "PROBABLE", "UNVERIFIED", "REJECTED", "ALREADY_FIXED", "DUPLICATE", "DESIGN_DECISION", "BLOCKED"]
    root_cause: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    trigger_condition: str
    actual_behavior: str
    expected_behavior: str
    protection_analysis: str
    design_compatibility: str
    duplicate_assessment: str
    already_fixed_assessment: str
    recommended_action: Literal["PR", "ISSUE", "INVESTIGATE", "DISCARD"]
    impact: str
    regression_risk: str
    baseline_run_id: str | None = None
    failure_signature: str | None = None
    reproducible: bool = False
    non_executable_reason: str | None = None

    @model_validator(mode="after")
    def sufficient(self) -> ValidationReport:
        if self.result == "CONFIRMED":
            for field in ("trigger_condition", "actual_behavior", "expected_behavior", "protection_analysis", "design_compatibility", "duplicate_assessment", "already_fixed_assessment", "impact", "regression_risk"):
                if not getattr(self, field).strip():
                    raise ValueError(f"Confirmed reports need {field}")
            if not self.reproducible and not self.non_executable_reason:
                raise ValueError("Unreproduced findings need an explicit non-executable evidence explanation")
            if self.reproducible and (not self.baseline_run_id or not self.failure_signature):
                raise ValueError("Reproduced findings need an actual baseline run ID and observed failure signature")
        return self


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_id: str
    repository: str
    submission_type: Literal["PR", "ISSUE", "PR_UPDATE"]
    title: str = Field(min_length=5, max_length=240)
    body: str = Field(min_length=20)
    base_branch: str
    base_sha: str
    head_sha: str = ""
    branch: str = ""
    fork: str = ""
    diff_hash: str = ""
    changed_files: list[str] = Field(default_factory=list)
    test_run_ids: list[str] = Field(default_factory=list)
    risk: str = Field(min_length=1)
    prepared_at: str
    review_digest: str | None = None
    ai_assisted: bool = True

    _slug = field_validator("repository")(slug)

