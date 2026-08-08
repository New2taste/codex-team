"""Versioned, closed-set artifacts used by the trusted workflow control plane.

The artifact module deliberately contains no routing or scheduling policy.  It
owns only the wire contracts shared by those later layers and the small amount
of deterministic validation needed before an artifact can be handed to them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ArtifactError(RuntimeError):
    """Raised when an artifact cannot be read as a JSON object."""

    def __init__(self, code: str, message: str):
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


def _raise(code: str, message: str) -> None:
    """Raise the public workflow exception when the CLI module is available.

    ``ai_workflow`` imports this module after defining ``WorkflowError``.  A
    lazy import avoids a module cycle while preserving the existing public
    exception type for all validators.  Direct imports still have a useful,
    self-contained ``ArtifactError`` fallback.
    """

    try:
        from .ai_workflow import WorkflowError
    except (ImportError, ModuleNotFoundError):
        try:
            from ai_workflow import WorkflowError
        except (ImportError, ModuleNotFoundError):
            raise ArtifactError(code, message)
    raise WorkflowError(code, message)


ROUTE_WORK_CLASSES = frozenset(
    {"SIMPLE", "PLANNING_ONLY", "BOUNDED", "MULTI_STAGE", "HIGH_CONSEQUENCE"}
)
ROUTE_EXECUTION_NEEDS = frozenset({"NONE", "READ_ONLY", "WRITE"})
REASON_CODES = frozenset(
    {
        "PROMPT_SUFFICIENT",
        "PLAN_IS_DELIVERABLE",
        "SOURCE_INSPECTION_REQUIRED",
        "STATE_CHANGE_REQUIRED",
        "DEPENDENT_STEPS_REQUIRED",
        "INDEPENDENT_WORKSTREAMS_PRESENT",
        "MATERIAL_CONSEQUENCE_PRESENT",
    }
)
RISK_FLAGS = frozenset(
    {
        "CONSTITUTION",
        "PIT",
        "SURVIVORSHIP_BIAS",
        "PUBLIC_SCHEMA",
        "APPEND_ONLY",
        "SECURITY",
        "DATA_CONTAMINATION",
        "PUBLIC_API",
        "CROSS_CARD_CONTRACT",
    }
)
ROUTES = frozenset({"direct", "sol_only", "delegated", "blocked"})
ROUTING_MODES = frozenset({"legacy", "shadow", "enforced"})
EXECUTION_SURFACES = frozenset({"NATIVE_SUBAGENT", "CODEX_EXEC_ROLE_CONTRACT"})
EVIDENCE_CLASSES = frozenset(
    {"measured", "sample_validated_projection", "unavailable"}
)
ROLES = frozenset(
    {
        "host",
        "luna",
        "luna_construction",
        "terra",
        "sol_planner",
        "sol_reviewer",
        "sol_xhigh",
        "sol_medium_supervisor",
        "terra_xhigh",
        "sol_medium_reviewer",
        "sol_xhigh_planner",
    }
)
RUNTIME_EVIDENCE_SOURCES = frozenset({"NATIVE_METADATA", "LOCAL_ROLLOUT"})
RUNTIME_STATUSES = frozenset({"VERIFIED", "FAILED"})


ROUTE_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "work_class",
        "execution_need",
        "decomposable",
        "risk_flags",
        "reason_codes",
    }
)
ROUTE_DECISION_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "route",
        "rule_id",
        "task_sha256",
        "request_sha256",
        "decided_at_utc",
        "routing_mode",
        "evidence_class",
    }
)
PLAN_FIELDS = frozenset(
    {"schema_version", "plan_id", "task_id", "goal", "done_when", "tasks", "stages"}
)
PLAN_TASK_FIELDS = frozenset(
    {
        "id",
        "owner_role",
        "read_scope",
        "write_scope",
        "do_not_touch",
        "depends_on",
        "expected_result",
        "verification_commands",
        "first_artifact",
        "evidence_level",
        "construction_envelope",
    }
)
PLAN_TASK_REQUIRED_FIELDS = PLAN_TASK_FIELDS - {"construction_envelope"}
CONSTRUCTION_ENVELOPE_FIELDS = frozenset(
    {"allowed_paths", "done_when", "evidence", "negative_checks", "risk_classification"}
)
CONSTRUCTION_EVIDENCE_LEVELS = frozenset({"L0", "L1", "L2"})
CONSTRUCTION_CHECK_FIELDS = frozenset(
    {"kind", "command", "expected_exit", "assertion", "artifact"}
)
CONSTRUCTION_HASH_CHECK_FIELDS = frozenset({"kind", "artifact", "sha256"})
CONSTRUCTION_RISK_CLASSIFICATION_FIELDS = frozenset(
    {"kind", "security", "authorization", "protocol", "control_plane"}
)
CONSTRUCTION_NOOP_COMMANDS = frozenset({"true", ":", "/usr/bin/true"})
CONSTRUCTION_PLACEHOLDERS = frozenset({"done", "evidence", "none", "pass", "true"})
RUNTIME_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "requested_role",
        "execution_surface",
        "observed_agent_type",
        "observed_model",
        "observed_reasoning_effort",
        "observed_sandbox_policy",
        "observed_permission_profile",
        "observed_cwd",
        "evidence_source",
        "observed_at_utc",
        "verification_status",
        "failure_reasons",
    }
)
COST_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "route",
        "role",
        "execution_surface",
        "duration_seconds",
        "prompt_bytes",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "retry_kind",
        "verification_seconds",
        "quality_outcome",
        "paired_case_id",
        "evidence_class",
        "rate_snapshot_id",
    }
)


def _dict_value(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    if dataclasses.is_dataclass(value):
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            result = to_dict()
        else:
            result = dataclasses.asdict(value)
        if isinstance(result, dict):
            return result
    _raise("INVALID_ARTIFACT", "artifact must be an object")
    raise AssertionError("unreachable")


def _check_fields(value: object, expected: frozenset[str], version: str) -> dict[str, object]:
    result = _dict_value(value)
    unknown = sorted(set(result) - expected)
    if unknown:
        _raise("UNKNOWN_FIELD", f"unsupported field {unknown[0]}")
    missing = sorted(expected - set(result))
    if missing:
        _raise("MISSING_FIELD", f"missing field {missing[0]}")
    if result.get("schema_version") != version:
        _raise("SCHEMA_VERSION", f"schema_version must be {version}")
    return result


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        _raise("INVALID_TYPE", f"{field} must be a string")
    if not value.strip():
        _raise("EMPTY_FIELD", f"{field} must not be empty")
    return value


def _string_array(value: object, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list):
        _raise("INVALID_TYPE", f"{field} must be an array")
    if not allow_empty and not value:
        _raise("EMPTY_ARRAY", f"{field} must not be empty")
    if any(not isinstance(item, str) for item in value):
        _raise("INVALID_TYPE", f"{field} items must be strings")
    if any(not item.strip() for item in value):
        _raise("EMPTY_FIELD", f"{field} items must not be empty")
    if len(value) != len(set(value)):
        _raise("DUPLICATE_ITEM", f"{field} must not contain duplicates")
    return list(value)


def _enum(value: object, field: str, values: frozenset[str]) -> str:
    if not isinstance(value, str):
        _raise("INVALID_TYPE", f"{field} must be a string")
    if value not in values:
        _raise("INVALID_ENUM", f"{field} is not supported")
    return value


def _finite_number(value: object, field: str, *, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _raise("COST_EVIDENCE_INVALID", f"{field} must be a finite non-negative number")
    if not math.isfinite(value) or value < 0:
        _raise("COST_EVIDENCE_INVALID", f"{field} must be a finite non-negative number")
    if integer and not isinstance(value, int):
        _raise("COST_EVIDENCE_INVALID", f"{field} must be an integer")
    return value


def _construction_string(value: object, field: str) -> str:
    result = _string(value, field)
    if result.casefold() in CONSTRUCTION_PLACEHOLDERS:
        _raise("PLAN_INVALID", f"{field} must not be a placeholder")
    return result


def _construction_command(value: object, field: str) -> str:
    result = _construction_string(value, field)
    if result.strip() in CONSTRUCTION_NOOP_COMMANDS or "\x00" in result or "\n" in result:
        _raise("PLAN_INVALID", f"{field} must be a runnable non-noop command")
    return result


def _construction_exit(value: object, field: str, *, expected_zero: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _raise("PLAN_INVALID", f"{field} must be an integer exit status")
    if (expected_zero and value != 0) or (not expected_zero and value == 0):
        _raise("PLAN_INVALID", f"{field} has an invalid expected exit status")
    return value


def _construction_object(value: object, field: str, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _raise("INVALID_TYPE", f"{field} must be an object")
    result = dict(value)
    unknown = sorted(set(result) - fields)
    missing = sorted(fields - set(result))
    if unknown:
        _raise("UNKNOWN_FIELD", f"{field} has unsupported field {unknown[0]}")
    if missing:
        _raise("MISSING_FIELD", f"{field} is missing field {missing[0]}")
    return result


def _construction_check(value: object, field: str, *, kind: str, expected_zero: bool) -> None:
    check = _construction_object(value, field, CONSTRUCTION_CHECK_FIELDS)
    if check["kind"] != kind:
        _raise("PLAN_INVALID", f"{field}.kind must be {kind}")
    _construction_command(check["command"], f"{field}.command")
    _construction_exit(check["expected_exit"], f"{field}.expected_exit", expected_zero=expected_zero)
    _construction_string(check["assertion"], f"{field}.assertion")
    _string(check["artifact"], f"{field}.artifact")


def _construction_hash_check(value: object, field: str) -> None:
    check = _construction_object(value, field, CONSTRUCTION_HASH_CHECK_FIELDS)
    if check["kind"] != "HASH":
        _raise("PLAN_INVALID", f"{field}.kind must be HASH")
    _string(check["artifact"], f"{field}.artifact")
    digest = _string(check["sha256"], f"{field}.sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        _raise("PLAN_INVALID", f"{field}.sha256 must be a SHA256 digest")


@dataclass(frozen=True)
class RouteRequest:
    schema_version: str = "ai-route-request-1"
    task_id: str = ""
    work_class: str = ""
    execution_need: str = ""
    decomposable: bool = False
    risk_flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "work_class": self.work_class,
            "execution_need": self.execution_need,
            "decomposable": self.decomposable,
            "risk_flags": list(self.risk_flags),
            "reason_codes": list(self.reason_codes),
        }

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]


@dataclass(frozen=True)
class RouteDecision:
    schema_version: str = "ai-route-decision-1"
    task_id: str = ""
    route: str = ""
    rule_id: str = ""
    task_sha256: str = ""
    request_sha256: str = ""
    decided_at_utc: str = ""
    routing_mode: str = ""
    evidence_class: str = ""

    @property
    def mode(self) -> str:
        return self.routing_mode

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "route": self.route,
            "rule_id": self.rule_id,
            "task_sha256": self.task_sha256,
            "request_sha256": self.request_sha256,
            "decided_at_utc": self.decided_at_utc,
            "routing_mode": self.routing_mode,
            "evidence_class": self.evidence_class,
        }

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]


@dataclass(frozen=True)
class PlanArtifact:
    schema_version: str = "ai-plan-1"
    plan_id: str = ""
    task_id: str = ""
    goal: str = ""
    done_when: tuple[str, ...] = ()
    tasks: tuple[dict[str, object], ...] = ()
    stages: tuple[tuple[str, ...], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "goal": self.goal,
            "done_when": list(self.done_when),
            "tasks": [dict(task) for task in self.tasks],
            "stages": [list(stage) for stage in self.stages],
        }

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]


@dataclass(frozen=True)
class RuntimeEvidence:
    schema_version: str = "runtime-evidence-1"
    attempt_id: str = ""
    requested_role: str = ""
    execution_surface: str = ""
    observed_agent_type: str | None = None
    observed_model: str = ""
    observed_reasoning_effort: str = ""
    observed_sandbox_policy: str = ""
    observed_permission_profile: str = ""
    observed_cwd: str = ""
    evidence_source: str = ""
    observed_at_utc: str = ""
    verification_status: str = ""
    failure_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "requested_role": self.requested_role,
            "execution_surface": self.execution_surface,
            "observed_agent_type": self.observed_agent_type,
            "observed_model": self.observed_model,
            "observed_reasoning_effort": self.observed_reasoning_effort,
            "observed_sandbox_policy": self.observed_sandbox_policy,
            "observed_permission_profile": self.observed_permission_profile,
            "observed_cwd": self.observed_cwd,
            "evidence_source": self.evidence_source,
            "observed_at_utc": self.observed_at_utc,
            "verification_status": self.verification_status,
            "failure_reasons": list(self.failure_reasons),
        }

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]


@dataclass(frozen=True)
class CostEvidence:
    schema_version: str = "cost-evidence-1"
    route: str = ""
    role: str = ""
    execution_surface: str = ""
    duration_seconds: int | float = 0
    prompt_bytes: int = 0
    input_tokens: int | float | None = None
    cached_input_tokens: int | float | None = None
    output_tokens: int | float | None = None
    retry_kind: str = "none"
    verification_seconds: int | float = 0
    quality_outcome: str = ""
    paired_case_id: str | None = None
    evidence_class: str = "unavailable"
    rate_snapshot_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "route": self.route,
            "role": self.role,
            "execution_surface": self.execution_surface,
            "duration_seconds": self.duration_seconds,
            "prompt_bytes": self.prompt_bytes,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "retry_kind": self.retry_kind,
            "verification_seconds": self.verification_seconds,
            "quality_outcome": self.quality_outcome,
            "paired_case_id": self.paired_case_id,
            "evidence_class": self.evidence_class,
            "rate_snapshot_id": self.rate_snapshot_id,
        }

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]


def artifact_sha256(value: Mapping[str, object]) -> str:
    """Hash canonical compact JSON with sorted keys and UTF-8 encoding."""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_artifact(path: Path) -> dict[str, object]:
    """Load an artifact document without silently accepting scalar/array JSON."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError("ARTIFACT_READ_ERROR", str(exc)) from exc
    if not isinstance(value, dict):
        raise ArtifactError("INVALID_ARTIFACT", "artifact must be an object")
    return value


def validate_route_request(value: object, task: Mapping[str, object]) -> None:
    request = _check_fields(value, ROUTE_REQUEST_FIELDS, "ai-route-request-1")
    task_value = _dict_value(task)
    task_id = _string(request["task_id"], "task_id")
    if task_id != task_value.get("task_id"):
        _raise("ROUTE_CONFLICT", "route request task_id does not match task")
    _string(task_id, "task.task_id")
    _enum(request["work_class"], "work_class", ROUTE_WORK_CLASSES)
    _enum(request["execution_need"], "execution_need", ROUTE_EXECUTION_NEEDS)
    if not isinstance(request["decomposable"], bool):
        _raise("INVALID_TYPE", "decomposable must be a boolean")
    request_flags = _string_array(request["risk_flags"], "risk_flags")
    if any(flag not in RISK_FLAGS for flag in request_flags):
        _raise("INVALID_ENUM", "risk_flags contains an unsupported value")
    task_flags = task_value.get("risk_flags")
    if not isinstance(task_flags, list):
        _raise("INVALID_TYPE", "task.risk_flags must be an array")
    if any(not isinstance(flag, str) or flag not in RISK_FLAGS for flag in task_flags):
        _raise("INVALID_ENUM", "task.risk_flags contains an unsupported value")
    if request_flags != task_flags:
        _raise("ROUTE_CONFLICT", "route request risk_flags do not match task")
    reasons = _string_array(request["reason_codes"], "reason_codes", allow_empty=False)
    if any(reason not in REASON_CODES for reason in reasons):
        _raise("INVALID_ENUM", "reason_codes contains an unsupported value")


def validate_route_decision(value: object) -> None:
    decision = _check_fields(value, ROUTE_DECISION_FIELDS, "ai-route-decision-1")
    _string(decision["task_id"], "task_id")
    _enum(decision["route"], "route", ROUTES)
    _string(decision["rule_id"], "rule_id")
    _string(decision["task_sha256"], "task_sha256")
    _string(decision["request_sha256"], "request_sha256")
    _string(decision["decided_at_utc"], "decided_at_utc")
    _enum(decision["routing_mode"], "routing_mode", ROUTING_MODES)
    _enum(decision["evidence_class"], "evidence_class", EVIDENCE_CLASSES)


def validate_plan_shape(value: object) -> None:
    plan = _check_fields(value, PLAN_FIELDS, "ai-plan-1")
    for field in ("plan_id", "task_id", "goal"):
        _string(plan[field], field)
    _string_array(plan["done_when"], "done_when", allow_empty=False)
    tasks = plan["tasks"]
    if not isinstance(tasks, list):
        _raise("INVALID_TYPE", "tasks must be an array")
    if not tasks:
        _raise("EMPTY_ARRAY", "tasks must not be empty")
    for index, item in enumerate(tasks):
        if not isinstance(item, Mapping):
            _raise("INVALID_TYPE", f"tasks[{index}] must be an object")
        task = dict(item)
        unknown = sorted(set(item) - PLAN_TASK_FIELDS)
        missing = sorted(PLAN_TASK_REQUIRED_FIELDS - set(item))
        if unknown:
            _raise("UNKNOWN_FIELD", f"tasks[{index}] has unsupported field {unknown[0]}")
        if missing:
            _raise("MISSING_FIELD", f"tasks[{index}] is missing field {missing[0]}")
        _string(task["id"], f"tasks[{index}].id")
        _enum(task["owner_role"], f"tasks[{index}].owner_role", ROLES)
        for field in ("read_scope", "write_scope", "do_not_touch", "depends_on", "verification_commands"):
            _string_array(task[field], f"tasks[{index}].{field}")
        _string(task["expected_result"], f"tasks[{index}].expected_result")
        _string(task["first_artifact"], f"tasks[{index}].first_artifact")
        _enum(task["evidence_level"], f"tasks[{index}].evidence_level", frozenset({"L0", "L1", "L2"}))
        if "construction_envelope" in task:
            envelope = task["construction_envelope"]
            if not isinstance(envelope, Mapping):
                _raise("INVALID_TYPE", f"tasks[{index}].construction_envelope must be an object")
            unknown_envelope = sorted(set(envelope) - CONSTRUCTION_ENVELOPE_FIELDS)
            missing_envelope = sorted(CONSTRUCTION_ENVELOPE_FIELDS - set(envelope))
            if unknown_envelope:
                _raise(
                    "UNKNOWN_FIELD",
                    f"tasks[{index}].construction_envelope has unsupported field {unknown_envelope[0]}",
                )
            if missing_envelope:
                _raise(
                    "MISSING_FIELD",
                    f"tasks[{index}].construction_envelope is missing field {missing_envelope[0]}",
                )
            _string_array(envelope["allowed_paths"], f"tasks[{index}].construction_envelope.allowed_paths", allow_empty=False)
            _construction_check(
                envelope["done_when"],
                f"tasks[{index}].construction_envelope.done_when",
                kind="TEST",
                expected_zero=True,
            )
            negative_checks = envelope["negative_checks"]
            if not isinstance(negative_checks, list) or not negative_checks:
                _raise("PLAN_INVALID", f"tasks[{index}].construction_envelope.negative_checks must be a non-empty array")
            for check_index, check in enumerate(negative_checks):
                _construction_check(
                    check,
                    f"tasks[{index}].construction_envelope.negative_checks[{check_index}]",
                    kind="COMMAND",
                    expected_zero=False,
                )
            evidence = envelope["evidence"]
            if not isinstance(evidence, Mapping):
                _raise("INVALID_TYPE", f"tasks[{index}].construction_envelope.evidence must be an object")
            unknown_evidence = sorted(set(evidence) - CONSTRUCTION_EVIDENCE_LEVELS)
            missing_evidence = sorted(CONSTRUCTION_EVIDENCE_LEVELS - set(evidence))
            if unknown_evidence:
                _raise(
                    "UNKNOWN_FIELD",
                    f"tasks[{index}].construction_envelope.evidence has unsupported field {unknown_evidence[0]}",
                )
            if missing_evidence:
                _raise(
                    "MISSING_FIELD",
                    f"tasks[{index}].construction_envelope.evidence is missing field {missing_evidence[0]}",
                )
            _construction_hash_check(
                evidence["L0"], f"tasks[{index}].construction_envelope.evidence.L0"
            )
            _construction_check(
                evidence["L1"],
                f"tasks[{index}].construction_envelope.evidence.L1",
                kind="COMMAND",
                expected_zero=True,
            )
            _construction_check(
                evidence["L2"],
                f"tasks[{index}].construction_envelope.evidence.L2",
                kind="TEST",
                expected_zero=True,
            )
            classification = _construction_object(
                envelope["risk_classification"],
                f"tasks[{index}].construction_envelope.risk_classification",
                CONSTRUCTION_RISK_CLASSIFICATION_FIELDS,
            )
            if classification["kind"] != "LOCAL_DETERMINISTIC_IMPLEMENTATION":
                _raise("PLAN_INVALID", "luna construction requires a local deterministic classification")
            for field in ("security", "authorization", "protocol", "control_plane"):
                if classification[field] is not False:
                    _raise("PLAN_INVALID", f"luna construction cannot declare {field}")
    stages = plan["stages"]
    if not isinstance(stages, list):
        _raise("INVALID_TYPE", "stages must be an array")
    if not stages:
        _raise("EMPTY_ARRAY", "stages must not be empty")
    for index, stage in enumerate(stages):
        _string_array(stage, f"stages[{index}]", allow_empty=False)


def validate_runtime_evidence(value: object) -> None:
    evidence = _check_fields(value, RUNTIME_EVIDENCE_FIELDS, "runtime-evidence-1")
    for field in (
        "attempt_id",
        "observed_model",
        "observed_reasoning_effort",
        "observed_sandbox_policy",
        "observed_permission_profile",
        "observed_cwd",
        "observed_at_utc",
    ):
        _string(evidence[field], field)
    _enum(evidence["requested_role"], "requested_role", ROLES - {"host"})
    surface = _enum(evidence["execution_surface"], "execution_surface", EXECUTION_SURFACES)
    observed_type = evidence["observed_agent_type"]
    if surface == "NATIVE_SUBAGENT":
        if not isinstance(observed_type, str) or not observed_type.strip():
            _raise("RUNTIME_IDENTITY_MISSING", "native subagent requires observed_agent_type")
    elif observed_type is not None:
        _raise("RUNTIME_IDENTITY_CONFLICT", "exec role contract must have null observed_agent_type")
    _enum(evidence["evidence_source"], "evidence_source", RUNTIME_EVIDENCE_SOURCES)
    status = _enum(evidence["verification_status"], "verification_status", RUNTIME_STATUSES)
    reasons = _string_array(evidence["failure_reasons"], "failure_reasons")
    if status == "FAILED" and not reasons:
        _raise("RUNTIME_IDENTITY_MISSING", "failed runtime evidence requires failure_reasons")


def validate_cost_evidence(value: object) -> None:
    evidence = _check_fields(value, COST_EVIDENCE_FIELDS, "cost-evidence-1")
    _enum(evidence["route"], "route", ROUTES)
    _enum(evidence["role"], "role", ROLES - {"host"})
    _enum(evidence["execution_surface"], "execution_surface", EXECUTION_SURFACES)
    _finite_number(evidence["duration_seconds"], "duration_seconds")
    _finite_number(evidence["prompt_bytes"], "prompt_bytes", integer=True)
    for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
        if evidence[field] is not None:
            _finite_number(evidence[field], field)
    _string(evidence["retry_kind"], "retry_kind")
    _finite_number(evidence["verification_seconds"], "verification_seconds")
    _string(evidence["quality_outcome"], "quality_outcome")
    for field in ("paired_case_id", "rate_snapshot_id"):
        if evidence[field] is not None:
            _string(evidence[field], field)
    evidence_class = _enum(evidence["evidence_class"], "evidence_class", EVIDENCE_CLASSES)
    token_values = tuple(
        evidence[field]
        for field in ("input_tokens", "cached_input_tokens", "output_tokens")
    )
    if evidence_class == "measured" and any(value is None for value in token_values):
        _raise("COST_EVIDENCE_INVALID", "measured evidence requires all token fields")
    if evidence_class == "sample_validated_projection" and evidence["rate_snapshot_id"] is None:
        _raise("COST_EVIDENCE_INVALID", "projection evidence requires rate_snapshot_id")
