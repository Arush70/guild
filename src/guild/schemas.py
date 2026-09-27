"""Output contracts for each role's final JSON reply.

A role's reply is parsed with `extract_json`, then validated against its schema. Validation
errors are fed back to the model verbatim so it can repair its reply (bounded retries). Roles
without a schema (user-defined ones) are accepted as any JSON object.

Lenient by design: unknown keys are ignored, common near-misses are coerced (a string where a
list is expected becomes a one-item list; "true"/"yes" become booleans).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


def _as_list(v: Any) -> Any:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return v


def _as_bool(v: Any) -> Any:
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "1", "pass", "passed"}
    return v


class PlanTask(_Lenient):
    id: str = ""
    title: str
    description: str = ""
    files: list[str] = Field(default_factory=list)
    done_when: str = ""
    depends_on: list[str] = Field(default_factory=list)

    _lists = field_validator("files", "depends_on", mode="before")(_as_list)


class PlanOutput(_Lenient):
    roadmap: list[str] = Field(default_factory=list)
    tasks: list[PlanTask] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    questions_for_owner: list[str] = Field(default_factory=list)

    _lists = field_validator("roadmap", "risks", "questions_for_owner", mode="before")(_as_list)

    @field_validator("tasks", mode="after")
    @classmethod
    def _ids(cls, tasks: list[PlanTask]) -> list[PlanTask]:
        for i, t in enumerate(tasks):
            if not t.id:
                t.id = f"T{i + 1}"
        return tasks


class LeadReview(_Lenient):
    decision: Literal["ACCEPT", "REVISE", "REPLAN"]
    notes: str = ""
    improvements: list[str] = Field(default_factory=list)

    _lists = field_validator("improvements", mode="before")(_as_list)

    @field_validator("decision", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return v.strip().upper() if isinstance(v, str) else v


class EngineerOutput(_Lenient):
    status: Literal["done", "blocked"]
    summary: str = ""
    files_changed: list[str] = Field(default_factory=list)
    tests_run: str = ""
    notes_for_reviewer: str = ""

    _lists = field_validator("files_changed", mode="before")(_as_list)

    @field_validator("status", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip().lower()
            if v in {"complete", "completed", "success", "ok", "finished"}:
                return "done"
        return v


class Finding(_Lenient):
    severity: str = "medium"
    file: str = ""
    issue: str = ""
    suggestion: str = ""
    evidence: str = ""
    fix: str = ""

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v: Any) -> Any:
        return str(v).strip().lower() if v is not None else "medium"


class ReviewOutput(_Lenient):
    verdict: Literal["approve", "request_changes", "block"]
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""

    @field_validator("findings", mode="before")
    @classmethod
    def _findings(cls, v: Any) -> Any:
        items = _as_list(v)
        return [{"issue": x} if isinstance(x, str) else x for x in items] if isinstance(items, list) else items

    @field_validator("verdict", mode="before")
    @classmethod
    def _norm(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip().lower().replace(" ", "_").replace("-", "_")
            if v in {"approved", "pass", "ok", "lgtm"}:
                return "approve"
            if v in {"changes_requested", "request_change", "reject", "rejected", "needs_changes"}:
                return "request_changes"
            if v in {"blocked", "critical"}:
                return "block"
        return v


class VerifierOutput(_Lenient):
    passed: bool
    tests_run: int = 0
    failures: list[str] = Field(default_factory=list)
    output_tail: str = ""

    _lists = field_validator("failures", mode="before")(_as_list)
    _bool = field_validator("passed", mode="before")(_as_bool)


class DocsOutput(_Lenient):
    files_changed: list[str] = Field(default_factory=list)
    summary: str = ""

    _lists = field_validator("files_changed", mode="before")(_as_list)


class Alternative(_Lenient):
    option: str = ""
    pros: str = ""
    cons: str = ""


class ResearchOutput(_Lenient):
    recommendation: str
    alternatives: list[Alternative] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)

    _lists = field_validator("alternatives", "sources", mode="before")(_as_list)


# role name -> (job -> schema). "default" applies when the job isn't listed.
SCHEMAS: dict[str, dict[str, type[BaseModel]]] = {
    "lead": {"plan": PlanOutput, "review": LeadReview, "default": LeadReview},
    "engineer": {"default": EngineerOutput},
    "critic": {"default": ReviewOutput},
    "security": {"default": ReviewOutput},
    "performance": {"default": ReviewOutput},
    "verifier": {"default": VerifierOutput},
    "docs": {"default": DocsOutput},
    "researcher": {"default": ResearchOutput},
    "kaggle": {"default": PlanOutput},
    # "assistant" deliberately absent: free-form prose
}


def schema_for(role: str, job: str = "default") -> type[BaseModel] | None:
    by_job = SCHEMAS.get(role)
    if not by_job:
        return None
    return by_job.get(job) or by_job.get("default")


def validate(role: str, job: str, data: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return (normalised dict, None) on success or (None, human-readable error) on failure."""
    schema = schema_for(role, job)
    if schema is None:
        return data, None
    try:
        return schema.model_validate(data).model_dump(), None
    except ValidationError as e:
        lines = []
        for err in e.errors()[:6]:
            loc = ".".join(str(x) for x in err["loc"]) or "(root)"
            lines.append(f"- {loc}: {err['msg']}")
        return None, "\n".join(lines)
