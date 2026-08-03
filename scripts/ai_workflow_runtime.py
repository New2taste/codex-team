"""Fail-closed runtime identity, rollout inspection, and literal Codex usage."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from subprocess import run as _run_inspector

try:
    from .ai_workflow_artifacts import RuntimeEvidence, validate_runtime_evidence
except ImportError:  # direct script execution
    from ai_workflow_artifacts import RuntimeEvidence, validate_runtime_evidence


NATIVE_SUBAGENT = "NATIVE_SUBAGENT"
CODEX_EXEC_ROLE_CONTRACT = "CODEX_EXEC_ROLE_CONTRACT"
EXECUTION_SURFACES = frozenset({NATIVE_SUBAGENT, CODEX_EXEC_ROLE_CONTRACT})
NATIVE_METADATA = "NATIVE_METADATA"
LOCAL_ROLLOUT = "LOCAL_ROLLOUT"
EVIDENCE_SOURCES = frozenset({NATIVE_METADATA, LOCAL_ROLLOUT})
IDENTITY_FIELDS = (
    "agent_type",
    "model",
    "reasoning_effort",
    "sandbox_policy",
    "permission_profile",
    "cwd",
)
_NON_AGENT_IDENTITY_FIELDS = IDENTITY_FIELDS[1:]
_IDENTITY_ALIASES = {
    "agent_type": ("agent_type", "observed_agent_type"),
    "model": ("model", "observed_model"),
    "reasoning_effort": ("reasoning_effort", "observed_reasoning_effort"),
    "sandbox_policy": ("sandbox_policy", "observed_sandbox_policy"),
    "permission_profile": ("permission_profile", "observed_permission_profile"),
    "cwd": ("cwd", "observed_cwd"),
}
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
_INSPECTOR_FIELDS = frozenset(
    {
        "thread_id",
        "agent_type",
        "model",
        "reasoning_effort",
        "sandbox_policy",
        "permission_profile",
        "cwd",
    }
)


@dataclass(frozen=True)
class RuntimeRepositorySnapshot:
    """Controller-captured repository state used only for a narrow exception."""

    head: str
    status: tuple[str, ...]


@dataclass(frozen=True)
class RuntimeArtifactSnapshot:
    """Controller-captured artifact hashes; attempt/log paths are never included."""

    digests: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RuntimeObservation:
    """Internal, source-tagged partial observation.

    Provenance is deliberately separate from identity equality.  It is used to
    determine the decisive artifact source after compatible partial values are
    merged, but it cannot hide conflicting values.
    """

    execution_surface: str
    agent_type: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    sandbox_policy: str | None = None
    permission_profile: str | None = None
    cwd: str | None = None
    evidence_sources: tuple[str, ...] = ()
    field_sources: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_surface": self.execution_surface,
            "agent_type": self.agent_type,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "sandbox_policy": self.sandbox_policy,
            "permission_profile": self.permission_profile,
            "cwd": self.cwd,
            "evidence_sources": list(self.evidence_sources),
        }

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]

    def sources_for(self, field: str) -> tuple[str, ...]:
        return dict(self.field_sources).get(field, ())


def _workflow_error(code: str, message: str) -> BaseException:
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
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    if dataclasses.is_dataclass(value):
        result = dataclasses.asdict(value)
        if isinstance(result, dict):
            return result
    _fail("RUNTIME_IDENTITY_MISSING", f"{label} must be an object")
    raise AssertionError("unreachable")


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("RUNTIME_IDENTITY_MISSING", field)
    return value


def _aliases(value: Mapping[str, object], field: str) -> object:
    present = [value[name] for name in _IDENTITY_ALIASES[field] if name in value]
    if not present:
        return None
    first = present[0]
    if any(candidate != first for candidate in present[1:]):
        _fail("RUNTIME_IDENTITY_CONFLICT", field)
    return first


def _source_tags(value: Mapping[str, object]) -> tuple[str, ...]:
    source = value.get("evidence_source")
    tags_value = value.get("evidence_sources")
    if tags_value is None:
        tags = () if source is None else (source,)
    elif isinstance(tags_value, (list, tuple)):
        tags = tuple(tags_value)
        if source is not None and source not in tags:
            _fail("RUNTIME_IDENTITY_CONFLICT", "evidence_source")
    else:
        _fail("RUNTIME_IDENTITY_MISSING", "evidence_sources")
    if not tags or any(tag not in EVIDENCE_SOURCES for tag in tags):
        _fail("RUNTIME_IDENTITY_MISSING", "evidence_source")
    if len(set(tags)) != len(tags):
        _fail("RUNTIME_IDENTITY_CONFLICT", "evidence_sources")
    return tuple(sorted(tags))


def _observation(value: object, *, complete: bool) -> RuntimeObservation:
    if isinstance(value, RuntimeObservation):
        result = value
    else:
        raw = _as_mapping(value, label="runtime observation")
        surface = raw.get("execution_surface")
        if surface not in EXECUTION_SURFACES:
            _fail("RUNTIME_IDENTITY_CONFLICT", "execution_surface")
        tags = _source_tags(raw)
        identities = {field: _aliases(raw, field) for field in IDENTITY_FIELDS}
        if identities["agent_type"] is not None and not isinstance(identities["agent_type"], str):
            _fail("RUNTIME_IDENTITY_MISSING", "agent_type")
        for field in _NON_AGENT_IDENTITY_FIELDS:
            item = identities[field]
            if item is not None and (not isinstance(item, str) or not item.strip()):
                _fail("RUNTIME_IDENTITY_MISSING", field)
        field_sources = tuple(
            (field, tags)
            for field, item in identities.items()
            if item is not None
        )
        result = RuntimeObservation(
            execution_surface=surface,
            agent_type=identities["agent_type"],
            model=identities["model"],
            reasoning_effort=identities["reasoning_effort"],
            sandbox_policy=identities["sandbox_policy"],
            permission_profile=identities["permission_profile"],
            cwd=identities["cwd"],
            evidence_sources=tags,
            field_sources=field_sources,
        )
    if complete and any(getattr(result, field) is None for field in _NON_AGENT_IDENTITY_FIELDS):
        _fail("RUNTIME_IDENTITY_MISSING", "runtime observation is incomplete")
    return result


def _requested_role(requested: Mapping[str, object]) -> str:
    role = requested.get("requested_role", requested.get("role"))
    if not isinstance(role, str) or role not in _RUNTIME_ROLES:
        _fail("RUNTIME_IDENTITY_MISSING", "requested_role")
    return role


def merge_runtime_observations(*observations: object) -> RuntimeObservation:
    """Merge compatible partial observations, retaining a source tag per fact."""

    if not observations:
        _fail("RUNTIME_IDENTITY_MISSING", "runtime observation")
    surface: str | None = None
    values: dict[str, object] = {}
    sources: set[str] = set()
    field_sources: dict[str, set[str]] = {field: set() for field in IDENTITY_FIELDS}
    for candidate in observations:
        observation = _observation(candidate, complete=False)
        if surface is None:
            surface = observation.execution_surface
        elif surface != observation.execution_surface:
            _fail("RUNTIME_IDENTITY_CONFLICT", "execution_surface")
        sources.update(observation.evidence_sources)
        for field in IDENTITY_FIELDS:
            item = getattr(observation, field)
            if item is None:
                continue
            if field in values and values[field] != item:
                _fail("RUNTIME_IDENTITY_CONFLICT", field)
            values[field] = item
            field_sources[field].update(observation.sources_for(field))
    if surface is None:
        _fail("RUNTIME_IDENTITY_MISSING", "execution_surface")
    result = RuntimeObservation(
        execution_surface=surface,
        agent_type=values.get("agent_type"),
        model=values.get("model"),
        reasoning_effort=values.get("reasoning_effort"),
        sandbox_policy=values.get("sandbox_policy"),
        permission_profile=values.get("permission_profile"),
        cwd=values.get("cwd"),
        evidence_sources=tuple(sorted(sources)),
        field_sources=tuple(
            (field, tuple(sorted(tags))) for field, tags in field_sources.items() if tags
        ),
    )
    return _observation(result, complete=True)


def _permission_rank(value: object) -> int | None:
    return _PERMISSION_RANKS.get(value) if isinstance(value, str) else None


def _trusted_repository_snapshot(value: object) -> bool:
    return (
        isinstance(value, RuntimeRepositorySnapshot)
        and isinstance(value.head, str)
        and bool(value.head)
        and isinstance(value.status, tuple)
        and all(isinstance(item, str) for item in value.status)
    )


def _trusted_artifact_snapshot(value: object) -> bool:
    return (
        isinstance(value, RuntimeArtifactSnapshot)
        and bool(value.digests)
        and all(
            isinstance(path, str)
            and path
            and "attempt" not in path
            and "log" not in path
            and isinstance(digest, str)
            and len(digest) == 64
            for path, digest in value.digests
        )
    )


def _broadened_reviewer_is_proven(
    requested: Mapping[str, object], observed: Mapping[str, object]
) -> bool:
    if _requested_role(requested) != "sol_reviewer":
        return False
    if requested.get("hard_read_only") is not False:
        return False
    if _aliases(requested, "sandbox_policy") != "read-only":
        return False
    if _aliases(requested, "permission_profile") != "read-only":
        return False
    if observed.get("controller_prompt_forbids_writes") is not True:
        return False
    before_repo = observed.get("before_repository_snapshot")
    after_repo = observed.get("after_repository_snapshot")
    before_artifacts = observed.get("before_artifact_snapshot")
    after_artifacts = observed.get("after_artifact_snapshot")
    if not all(
        (
            _trusted_repository_snapshot(before_repo),
            _trusted_repository_snapshot(after_repo),
            _trusted_artifact_snapshot(before_artifacts),
            _trusted_artifact_snapshot(after_artifacts),
        )
    ):
        return False
    return before_repo == after_repo and before_artifacts == after_artifacts


def permission_is_within_contract(requested: object, observed: object) -> bool:
    requested_value = _as_mapping(requested, label="runtime contract")
    observed_value = _as_mapping(observed, label="runtime observation")
    expected_sandbox = _permission_rank(_aliases(requested_value, "sandbox_policy"))
    actual_sandbox = _permission_rank(_aliases(observed_value, "sandbox_policy"))
    expected_profile = _permission_rank(_aliases(requested_value, "permission_profile"))
    actual_profile = _permission_rank(_aliases(observed_value, "permission_profile"))
    if None in (expected_sandbox, actual_sandbox, expected_profile, actual_profile):
        return False
    if actual_sandbox <= expected_sandbox and actual_profile <= expected_profile:
        return True
    if actual_sandbox > 1 or actual_profile > 1:
        return False
    return _broadened_reviewer_is_proven(requested_value, observed_value)


def _decisive_source(observed: RuntimeObservation) -> str:
    if observed.execution_surface == CODEX_EXEC_ROLE_CONTRACT:
        return LOCAL_ROLLOUT
    if NATIVE_METADATA not in observed.evidence_sources:
        _fail("RUNTIME_IDENTITY_CONFLICT", "native identity lacks native metadata")
    # Native metadata may be completed only for model and reasoning effort.
    if all(NATIVE_METADATA in observed.sources_for(field) for field in ("model", "reasoning_effort")):
        return NATIVE_METADATA
    if LOCAL_ROLLOUT not in observed.evidence_sources:
        _fail("RUNTIME_IDENTITY_MISSING", "native metadata omitted model or reasoning_effort")
    return LOCAL_ROLLOUT


def _verify_source_contract(observed: RuntimeObservation) -> str:
    if observed.execution_surface == CODEX_EXEC_ROLE_CONTRACT:
        if observed.evidence_sources != (LOCAL_ROLLOUT,):
            _fail("RUNTIME_IDENTITY_CONFLICT", "exec requires only local rollout evidence")
        return _decisive_source(observed)
    if NATIVE_METADATA not in observed.evidence_sources:
        _fail("RUNTIME_IDENTITY_CONFLICT", "native identity requires native metadata")
    for field in ("agent_type", "sandbox_policy", "permission_profile", "cwd"):
        if NATIVE_METADATA not in observed.sources_for(field):
            _fail("RUNTIME_IDENTITY_CONFLICT", f"native metadata must provide {field}")
    return _decisive_source(observed)


def verify_runtime_identity(requested: object, observed: object) -> RuntimeEvidence:
    """Verify identity values and source policy without trusting requested copies."""

    requested_value = _as_mapping(requested, label="runtime contract")
    # Evaluate every canonical/observed alias pair even when a direct key exists.
    expected = {field: _aliases(requested_value, field) for field in IDENTITY_FIELDS}
    expected_surface = requested_value.get("execution_surface")
    if expected_surface not in EXECUTION_SURFACES:
        _fail("RUNTIME_IDENTITY_MISSING", "execution_surface")
    role = _requested_role(requested_value)
    observed_raw = _as_mapping(observed, label="runtime observation")
    actual = _observation(observed, complete=True)
    if actual.execution_surface != expected_surface:
        _fail("RUNTIME_IDENTITY_CONFLICT", "execution_surface")
    if expected_surface == NATIVE_SUBAGENT:
        if not isinstance(expected["agent_type"], str) or not expected["agent_type"].strip():
            _fail("RUNTIME_IDENTITY_MISSING", "agent_type")
        if not isinstance(actual.agent_type, str) or not actual.agent_type.strip():
            _fail("RUNTIME_IDENTITY_MISSING", "agent_type")
        if actual.agent_type != expected["agent_type"]:
            _fail("RUNTIME_IDENTITY_CONFLICT", "agent_type")
    else:
        if expected["agent_type"] is not None or actual.agent_type is not None:
            _fail("RUNTIME_IDENTITY_CONFLICT", "exec is not a custom agent")
    for field in ("model", "reasoning_effort", "cwd"):
        if actual[field] != _string(expected[field], field):
            _fail("RUNTIME_IDENTITY_CONFLICT", field)
    if not permission_is_within_contract(requested_value, observed_raw):
        _fail("RUNTIME_PERMISSION_MISMATCH", "effective permission exceeds contract")
    decisive_source = _verify_source_contract(actual)
    evidence = RuntimeEvidence(
        attempt_id=_string(requested_value.get("attempt_id"), "attempt_id"),
        requested_role=role,
        execution_surface=actual.execution_surface,
        observed_agent_type=actual.agent_type,
        observed_model=_string(actual.model, "model"),
        observed_reasoning_effort=_string(actual.reasoning_effort, "reasoning_effort"),
        observed_sandbox_policy=_string(actual.sandbox_policy, "sandbox_policy"),
        observed_permission_profile=_string(actual.permission_profile, "permission_profile"),
        observed_cwd=_string(actual.cwd, "cwd"),
        evidence_source=decisive_source,
        observed_at_utc=datetime.now(timezone.utc).isoformat(),
        verification_status="VERIFIED",
        failure_reasons=(),
    )
    validate_runtime_evidence(evidence)
    return evidence


def extract_codex_usage(events: Iterable[object]) -> dict[str, int | None]:
    usage: Mapping[str, object] | None = None
    for event in events:
        if isinstance(event, Mapping) and event.get("type") == "turn.completed":
            candidate = event.get("usage")
            usage = candidate if isinstance(candidate, Mapping) else None
    return {
        field: value
        if isinstance(value := (usage.get(field) if usage is not None else None), int)
        and not isinstance(value, bool)
        and value >= 0
        else None
        for field in USAGE_FIELDS
    }


def parse_codex_jsonl(value: object) -> list[dict[str, object]]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return []
    events: list[dict[str, object]] = []
    for line in value.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def extract_codex_thread_id(events: Iterable[object]) -> str:
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
    if len(identifiers) != 1:
        _fail("RUNTIME_EVIDENCE_MISSING", "exactly one fresh thread.started ID is required")
    return next(iter(identifiers))


def inspect_agent_runtime(
    sessions_dir: Path, thread_id: str, execution_surface: str, inspector_path: Path
) -> dict[str, object]:
    """Call the allowlisted inspector and independently validate its output."""

    sessions = Path(sessions_dir)
    if execution_surface not in EXECUTION_SURFACES or not sessions.is_absolute():
        _fail("RUNTIME_EVIDENCE_MISSING", "an absolute runtime sessions directory is required")
    completed = _run_inspector(
        ["sh", str(Path(inspector_path)), "--sessions-dir", str(sessions), thread_id],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        _fail("RUNTIME_EVIDENCE_MISSING", "runtime rollout inspection failed")
    try:
        value = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _workflow_error("RUNTIME_EVIDENCE_INVALID", "runtime inspector emitted invalid JSON") from exc
    if not isinstance(value, Mapping) or set(value) != _INSPECTOR_FIELDS:
        _fail("RUNTIME_EVIDENCE_INVALID", "runtime inspector emitted an invalid allowlist")
    if value.get("thread_id") != thread_id:
        _fail("RUNTIME_EVIDENCE_INVALID", "runtime inspector thread ID does not match")
    if execution_surface == CODEX_EXEC_ROLE_CONTRACT:
        if value.get("agent_type") is not None:
            _fail("RUNTIME_IDENTITY_CONFLICT", "exec inspector claimed a custom agent")
    elif not isinstance(value.get("agent_type"), str) or not value["agent_type"].strip():
        _fail("RUNTIME_EVIDENCE_INVALID", "native inspector omitted agent_type")
    for field in _NON_AGENT_IDENTITY_FIELDS:
        if not isinstance(value.get(field), str) or not value[field].strip():
            _fail("RUNTIME_EVIDENCE_INVALID", f"runtime inspector omitted {field}")
    return {
        "execution_surface": execution_surface,
        "agent_type": value["agent_type"],
        "model": value["model"],
        "reasoning_effort": value["reasoning_effort"],
        "sandbox_policy": value["sandbox_policy"],
        "permission_profile": value["permission_profile"],
        "cwd": value["cwd"],
        "evidence_source": LOCAL_ROLLOUT,
    }


def codex_exec_contract(
    *, attempt_id: str, requested_role: str, model: str, reasoning_effort: str, sandbox_policy: str, cwd: str
) -> dict[str, object]:
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
        "hard_read_only": sandbox_policy == "read-only",
    }


def codex_exec_observation(
    *, before_repository_snapshot: RuntimeRepositorySnapshot, after_repository_snapshot: RuntimeRepositorySnapshot,
    before_artifact_snapshot: RuntimeArtifactSnapshot, after_artifact_snapshot: RuntimeArtifactSnapshot,
    controller_prompt_forbids_writes: bool,
) -> dict[str, object]:
    """Return only controller facts; role config is never copied into observed identity."""

    return {
        "execution_surface": CODEX_EXEC_ROLE_CONTRACT,
        "agent_type": None,
        "evidence_source": LOCAL_ROLLOUT,
        "before_repository_snapshot": before_repository_snapshot,
        "after_repository_snapshot": after_repository_snapshot,
        "before_artifact_snapshot": before_artifact_snapshot,
        "after_artifact_snapshot": after_artifact_snapshot,
        "controller_prompt_forbids_writes": controller_prompt_forbids_writes,
    }


def runtime_repository_snapshot(snapshot: object) -> RuntimeRepositorySnapshot:
    head = getattr(snapshot, "head", None)
    status = getattr(snapshot, "status", None)
    if not isinstance(head, str) or not isinstance(status, tuple):
        _fail("RUNTIME_EVIDENCE_INVALID", "controller repository snapshot is invalid")
    return RuntimeRepositorySnapshot(head=head, status=status)


def runtime_artifact_snapshot(paths: Mapping[str, Path]) -> RuntimeArtifactSnapshot:
    records: list[tuple[str, str]] = []
    for name, path in sorted(paths.items()):
        if "attempt" in name or "log" in name:
            _fail("RUNTIME_EVIDENCE_INVALID", "attempt and log paths cannot be artifact snapshots")
        try:
            records.append((name, hashlib.sha256(Path(path).read_bytes()).hexdigest()))
        except OSError as exc:
            raise _workflow_error("RUNTIME_EVIDENCE_INVALID", "cannot capture artifact snapshot") from exc
    if not records:
        _fail("RUNTIME_EVIDENCE_INVALID", "controller artifact snapshot is empty")
    return RuntimeArtifactSnapshot(tuple(records))


def write_runtime_evidence(store: object, task_id: str, evidence: object) -> Path:
    evidence_value = _as_mapping(evidence, label="runtime evidence")
    validate_runtime_evidence(evidence_value)
    if evidence_value.get("verification_status") != "VERIFIED":
        _fail("RUNTIME_EVIDENCE_INVALID", "only verified evidence may be promoted")
    surface = evidence_value.get("execution_surface")
    source = evidence_value.get("evidence_source")
    if surface == CODEX_EXEC_ROLE_CONTRACT and source != LOCAL_ROLLOUT:
        _fail("RUNTIME_IDENTITY_CONFLICT", "exec evidence requires local rollout")
    if surface == NATIVE_SUBAGENT and source not in EVIDENCE_SOURCES:
        _fail("RUNTIME_IDENTITY_CONFLICT", "native evidence has an invalid source")
    attempt_id = _string(evidence_value.get("attempt_id"), "attempt_id")
    require_task = getattr(store, "_require_task", None)
    lock = getattr(store, "lock", None)
    if not callable(require_task) or not callable(lock):
        _fail("RUNTIME_EVIDENCE_INVALID", "store does not provide workflow locking")
    path = Path(require_task(task_id)) / "runtime-evidence.jsonl"
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
