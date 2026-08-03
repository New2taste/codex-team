"""Fail-closed runtime identity and measured Codex usage evidence.

This module intentionally distinguishes a native subagent from a ``codex
exec`` role contract.  A command-line invocation can prove its requested
model, effort, sandbox, and working directory; it cannot prove that it was a
native custom agent, so its observed agent type is always ``None``.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from .ai_workflow_artifacts import RuntimeEvidence, validate_runtime_evidence
except ImportError:  # direct script execution
    from ai_workflow_artifacts import RuntimeEvidence, validate_runtime_evidence


NATIVE_SUBAGENT = "NATIVE_SUBAGENT"
CODEX_EXEC_ROLE_CONTRACT = "CODEX_EXEC_ROLE_CONTRACT"
EXECUTION_SURFACES = frozenset({NATIVE_SUBAGENT, CODEX_EXEC_ROLE_CONTRACT})
IDENTITY_FIELDS = (
    "agent_type",
    "model",
    "reasoning_effort",
    "sandbox_policy",
    "permission_profile",
    "cwd",
)
USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")
_RUNTIME_ROLES = frozenset({"luna", "terra", "sol_planner", "sol_reviewer", "sol_xhigh"})
_PERMISSION_RANKS = {
    "read-only": 0,
    "read": 0,
    "workspace-write": 1,
    "workspace_write": 1,
    "workspace": 1,
    "danger-full-access": 2,
    "full-access": 2,
    "full_access": 2,
}


@dataclass(frozen=True)
class RuntimeObservation:
    """The narrow, allowlisted facts used for runtime verification."""

    execution_surface: str
    agent_type: str | None
    model: str
    reasoning_effort: str
    sandbox_policy: str
    permission_profile: str
    cwd: str
    evidence_source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_surface": self.execution_surface,
            "agent_type": self.agent_type,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "sandbox_policy": self.sandbox_policy,
            "permission_profile": self.permission_profile,
            "cwd": self.cwd,
            "evidence_source": self.evidence_source,
        }

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]


def _workflow_error(code: str, message: str) -> BaseException:
    """Construct the public error lazily, avoiding an import cycle."""

    try:
        from .ai_workflow import WorkflowError
    except (ImportError, ModuleNotFoundError):
        from ai_workflow import WorkflowError
    return WorkflowError(code, message)


def _fail(code: str, message: str) -> None:
    raise _workflow_error(code, message)


def _as_mapping(value: object, *, label: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    if dataclasses.is_dataclass(value):
        converted = dataclasses.asdict(value)
        if isinstance(converted, dict):
            return converted
    _fail("RUNTIME_IDENTITY_MISSING", f"{label} must be an object")
    raise AssertionError("unreachable")


def _value(value: Mapping[str, object], field: str, *aliases: str) -> object:
    for key in (field, *aliases):
        if key in value:
            return value[key]
    return None


def _has(value: Mapping[str, object], field: str, *aliases: str) -> bool:
    return any(key in value for key in (field, *aliases))


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("RUNTIME_IDENTITY_MISSING", field)
    return value


def _observation(value: object) -> tuple[dict[str, object], RuntimeObservation]:
    raw = _as_mapping(value, label="runtime observation")
    surface = _value(raw, "execution_surface")
    if surface not in EXECUTION_SURFACES:
        _fail("RUNTIME_IDENTITY_CONFLICT", "execution_surface")
    agent_type = _value(raw, "agent_type", "observed_agent_type")
    observation = RuntimeObservation(
        execution_surface=surface,
        agent_type=agent_type if isinstance(agent_type, str) else None,
        model=_string(_value(raw, "model", "observed_model"), "model"),
        reasoning_effort=_string(
            _value(raw, "reasoning_effort", "observed_reasoning_effort"),
            "reasoning_effort",
        ),
        sandbox_policy=_string(
            _value(raw, "sandbox_policy", "observed_sandbox_policy"), "sandbox_policy"
        ),
        permission_profile=_string(
            _value(raw, "permission_profile", "observed_permission_profile"),
            "permission_profile",
        ),
        cwd=_string(_value(raw, "cwd", "observed_cwd"), "cwd"),
        evidence_source=_string(_value(raw, "evidence_source"), "evidence_source"),
    )
    return raw, observation


def _requested_role(requested: Mapping[str, object]) -> str:
    role = _value(requested, "requested_role", "role")
    if not isinstance(role, str) or role not in _RUNTIME_ROLES:
        _fail("RUNTIME_IDENTITY_MISSING", "requested_role")
    return role


def _snapshot_value(value: Mapping[str, object], name: str) -> object:
    aliases = {
        "before_repository_snapshot": ("before_repo_snapshot",),
        "after_repository_snapshot": ("after_repo_snapshot",),
        "before_artifact_snapshot": (),
        "after_artifact_snapshot": (),
    }
    snapshot = _value(value, name, *aliases[name])
    if dataclasses.is_dataclass(snapshot):
        return dataclasses.asdict(snapshot)
    return snapshot


def _broadened_review_is_proven(
    requested: Mapping[str, object], observed: Mapping[str, object]
) -> bool:
    if requested.get("hard_read_only") is not False:
        return False
    if observed.get("prompt_forbids_writes") is not True:
        return False
    before_repository = _snapshot_value(observed, "before_repository_snapshot")
    after_repository = _snapshot_value(observed, "after_repository_snapshot")
    before_artifact = _snapshot_value(observed, "before_artifact_snapshot")
    after_artifact = _snapshot_value(observed, "after_artifact_snapshot")
    if any(
        snapshot is None
        for snapshot in (
            before_repository,
            after_repository,
            before_artifact,
            after_artifact,
        )
    ):
        return False
    return before_repository == after_repository and before_artifact == after_artifact


def _permission_rank(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    return _PERMISSION_RANKS.get(value)


def permission_is_within_contract(requested: object, observed: object) -> bool:
    """Return whether observed sandbox and permission bounds do not broaden.

    Unknown policy labels are intentionally not ordered.  Treating a new label
    as harmless would allow a future, broader permission to bypass this gate.
    """

    requested_value = _as_mapping(requested, label="runtime contract")
    observed_value = _as_mapping(observed, label="runtime observation")
    expected_sandbox = _permission_rank(_value(requested_value, "sandbox_policy", "sandbox"))
    actual_sandbox = _permission_rank(
        _value(observed_value, "sandbox_policy", "observed_sandbox_policy")
    )
    expected_profile = _permission_rank(_value(requested_value, "permission_profile"))
    actual_profile = _permission_rank(
        _value(observed_value, "permission_profile", "observed_permission_profile")
    )
    if None in (expected_sandbox, actual_sandbox, expected_profile, actual_profile):
        return False
    if actual_sandbox <= expected_sandbox and actual_profile <= expected_profile:
        return True
    return _broadened_review_is_proven(requested_value, observed_value)


def merge_runtime_observations(*observations: object) -> RuntimeObservation:
    """Merge independently supplied observations only when every fact agrees."""

    if not observations:
        _fail("RUNTIME_IDENTITY_MISSING", "runtime observation")
    merged: dict[str, object] = {}
    for candidate in observations:
        _, observation = _observation(candidate)
        for field, value in observation.to_dict().items():
            if field in merged and merged[field] != value:
                _fail("RUNTIME_IDENTITY_CONFLICT", field)
            merged[field] = value
    _, result = _observation(merged)
    return result


def _runtime_evidence(
    requested: Mapping[str, object], observed: RuntimeObservation
) -> RuntimeEvidence:
    attempt_id = _string(_value(requested, "attempt_id"), "attempt_id")
    evidence = RuntimeEvidence(
        attempt_id=attempt_id,
        requested_role=_requested_role(requested),
        execution_surface=observed.execution_surface,
        observed_agent_type=observed.agent_type,
        observed_model=observed.model,
        observed_reasoning_effort=observed.reasoning_effort,
        observed_sandbox_policy=observed.sandbox_policy,
        observed_permission_profile=observed.permission_profile,
        observed_cwd=observed.cwd,
        evidence_source=observed.evidence_source,
        observed_at_utc=datetime.now(timezone.utc).isoformat(),
        verification_status="VERIFIED",
        failure_reasons=(),
    )
    validate_runtime_evidence(evidence)
    return evidence


def verify_runtime_identity(requested: object, observed: object) -> RuntimeEvidence:
    """Verify an allowlisted observation against one requested role contract.

    ``CODEX_EXEC_ROLE_CONTRACT`` never verifies a native agent identity.  It
    proves only the exact role contract passed to the CLI and deliberately
    records a null ``observed_agent_type``.
    """

    requested_value = _as_mapping(requested, label="runtime contract")
    observed_raw, observed_value = _observation(observed)
    expected_surface = _value(requested_value, "execution_surface")
    if expected_surface not in EXECUTION_SURFACES:
        _fail("RUNTIME_IDENTITY_MISSING", "execution_surface")
    if observed_value.execution_surface != expected_surface:
        _fail("RUNTIME_IDENTITY_CONFLICT", "execution_surface")
    _requested_role(requested_value)

    expected_source = _value(requested_value, "evidence_source")
    if expected_source is None:
        expected_source = (
            "NATIVE_METADATA"
            if expected_surface == NATIVE_SUBAGENT
            else "LOCAL_ROLLOUT"
        )
    if observed_value.evidence_source != expected_source:
        _fail("RUNTIME_IDENTITY_CONFLICT", "evidence_source")

    if expected_surface == NATIVE_SUBAGENT:
        expected_agent_type = _string(_value(requested_value, "agent_type"), "agent_type")
        if not _has(observed_raw, "agent_type", "observed_agent_type") or not isinstance(
            observed_value.agent_type, str
        ) or not observed_value.agent_type.strip():
            _fail("RUNTIME_IDENTITY_MISSING", "agent_type")
        if observed_value.agent_type != expected_agent_type:
            _fail("RUNTIME_IDENTITY_CONFLICT", "agent_type")
        if observed_value.evidence_source != "NATIVE_METADATA":
            _fail("RUNTIME_IDENTITY_CONFLICT", "evidence_source")
    else:
        if _value(requested_value, "agent_type") is not None:
            _fail("RUNTIME_IDENTITY_CONFLICT", "exec contract is not a custom agent")
        if not _has(observed_raw, "agent_type", "observed_agent_type"):
            _fail("RUNTIME_IDENTITY_MISSING", "agent_type")
        if _value(observed_raw, "agent_type", "observed_agent_type") is not None:
            _fail("RUNTIME_IDENTITY_CONFLICT", "exec is not a custom agent")
        if observed_value.evidence_source != "LOCAL_ROLLOUT":
            _fail("RUNTIME_IDENTITY_CONFLICT", "evidence_source")

    comparisons = (
        ("model", observed_value.model, _value(requested_value, "model")),
        (
            "reasoning_effort",
            observed_value.reasoning_effort,
            _value(requested_value, "reasoning_effort"),
        ),
        ("cwd", observed_value.cwd, _value(requested_value, "cwd")),
    )
    for field, actual, expected in comparisons:
        if actual != _string(expected, field):
            _fail("RUNTIME_IDENTITY_CONFLICT", field)
    if not permission_is_within_contract(requested_value, observed_raw):
        _fail("RUNTIME_PERMISSION_MISMATCH", "effective permission exceeds contract")
    return _runtime_evidence(requested_value, observed_value)


def extract_codex_usage(events: Iterable[object]) -> dict[str, int | None]:
    """Return only literal usage from the final ``turn.completed`` event.

    This function neither totals arbitrary events nor turns lengths, times, or
    model names into a token estimate.  A missing or malformed field remains
    unavailable as ``None``.
    """

    usage: Mapping[str, object] | None = None
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "turn.completed":
            continue
        candidate = event.get("usage")
        usage = candidate if isinstance(candidate, Mapping) else None
    result: dict[str, int | None] = {}
    for field in USAGE_FIELDS:
        value = usage.get(field) if usage is not None else None
        result[field] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    return result


def parse_codex_jsonl(value: object) -> list[dict[str, object]]:
    """Parse only JSON object lines from a single command's stdout.

    Invalid lines are not treated as runtime evidence; callers still require a
    fresh ``thread.started`` record before accepting the role result.
    """

    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return []
    events: list[dict[str, object]] = []
    for line in value.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def extract_codex_thread_id(events: Iterable[object]) -> str:
    """Require exactly one valid current-thread ID from current CLI JSONL."""

    identifiers: set[str] = set()
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "thread.started":
            continue
        thread_id = event.get("thread_id")
        if not isinstance(thread_id, str):
            _fail("RUNTIME_EVIDENCE_MISSING", "thread.started lacks thread_id")
        try:
            parsed = uuid.UUID(thread_id)
        except (TypeError, ValueError) as exc:
            raise _workflow_error("RUNTIME_EVIDENCE_MISSING", "thread_id is not a UUID") from exc
        if str(parsed) != thread_id.lower():
            _fail("RUNTIME_EVIDENCE_MISSING", "thread_id is not canonical")
        identifiers.add(thread_id)
    if not identifiers:
        _fail("RUNTIME_EVIDENCE_MISSING", "fresh thread.started evidence is required")
    if len(identifiers) != 1:
        _fail("RUNTIME_EVIDENCE_STALE", "multiple thread IDs appeared in one attempt")
    return next(iter(identifiers))


def codex_exec_contract(
    *,
    attempt_id: str,
    requested_role: str,
    model: str,
    reasoning_effort: str,
    sandbox_policy: str,
    cwd: str,
) -> dict[str, object]:
    """Build the explicit non-native contract used by a ``codex exec`` run."""

    return {
        "attempt_id": attempt_id,
        "requested_role": requested_role,
        "execution_surface": CODEX_EXEC_ROLE_CONTRACT,
        "agent_type": None,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "sandbox_policy": sandbox_policy,
        "permission_profile": sandbox_policy,
        "cwd": cwd,
        "evidence_source": "LOCAL_ROLLOUT",
        "hard_read_only": sandbox_policy == "read-only",
    }


def codex_exec_observation(
    requested: Mapping[str, object],
    *,
    before_repository_snapshot: object,
    after_repository_snapshot: object,
    prompt_forbids_writes: bool,
) -> dict[str, object]:
    """Record fixed exec parameters without asserting a native agent type."""

    return {
        "execution_surface": CODEX_EXEC_ROLE_CONTRACT,
        "agent_type": None,
        "model": _value(requested, "model"),
        "reasoning_effort": _value(requested, "reasoning_effort"),
        "sandbox_policy": _value(requested, "sandbox_policy", "sandbox"),
        "permission_profile": _value(requested, "permission_profile"),
        "cwd": _value(requested, "cwd"),
        "evidence_source": "LOCAL_ROLLOUT",
        "before_repository_snapshot": before_repository_snapshot,
        "after_repository_snapshot": after_repository_snapshot,
        "prompt_forbids_writes": prompt_forbids_writes,
    }


def write_runtime_evidence(store: object, task_id: str, evidence: object) -> Path:
    """Append verified evidence once; a duplicate attempt ID is stale evidence."""

    evidence_value = _as_mapping(evidence, label="runtime evidence")
    validate_runtime_evidence(evidence_value)
    if evidence_value.get("verification_status") != "VERIFIED":
        _fail("RUNTIME_EVIDENCE_INVALID", "only verified evidence may be promoted")
    attempt_id = _string(evidence_value.get("attempt_id"), "attempt_id")
    require_task = getattr(store, "_require_task", None)
    lock = getattr(store, "lock", None)
    if not callable(require_task) or not callable(lock):
        _fail("RUNTIME_EVIDENCE_INVALID", "store does not provide workflow locking")
    task_dir = Path(require_task(task_id))
    path = task_dir / "runtime-evidence.jsonl"
    with lock(task_id):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        except OSError as exc:
            raise _workflow_error("RUNTIME_EVIDENCE_INVALID", "cannot read runtime evidence") from exc
        for line in lines:
            try:
                prior = json.loads(line)
            except json.JSONDecodeError as exc:
                raise _workflow_error("RUNTIME_EVIDENCE_INVALID", "runtime evidence ledger is invalid") from exc
            if not isinstance(prior, Mapping) or not isinstance(prior.get("attempt_id"), str):
                _fail("RUNTIME_EVIDENCE_INVALID", "runtime evidence ledger is invalid")
            if prior["attempt_id"] == attempt_id:
                _fail("RUNTIME_EVIDENCE_STALE", "attempt ID already has runtime evidence")
        try:
            from .ai_workflow import append_jsonl
        except (ImportError, ModuleNotFoundError):
            from ai_workflow import append_jsonl
        append_jsonl(path, evidence_value)
    return path
