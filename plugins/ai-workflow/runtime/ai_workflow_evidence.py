"""Launch-intent events and versioned fork/nested runtime evidence producers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

try:
    from .ai_workflow_artifacts import (
        ArtifactError,
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        canonical_json,
        content_id,
        load_artifact,
        read_jsonl,
        verify_content_id,
    )
    from .ai_workflow_declarations import load_route_declaration_locked
    from .ai_workflow_preflight import LAUNCHER_VERSION, compute_install_version
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        ArtifactError,
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        canonical_json,
        content_id,
        load_artifact,
        read_jsonl,
        verify_content_id,
    )
    from ai_workflow_declarations import load_route_declaration_locked
    from ai_workflow_preflight import LAUNCHER_VERSION, compute_install_version

if TYPE_CHECKING:
    from .ai_workflow_dispatch_policy import DispatchPermit


LAUNCH_INTENT_EVENT_TYPE = "LAUNCH_INTENT_RECORDED"
LAUNCH_INTENT_SCHEMA_KIND = "ai-launch-intent-1"
LAUNCH_INTENT_EVENT_FIELDS = frozenset(
    {
        "event_type",
        "event_id",
        "task_id",
        "envelope_hash",
        "permit_id",
        "role",
        "command_sha256",
        "tool_mapping_sha256",
        "route_config_hash",
        "launcher_version",
        "install_version",
        "timestamp_utc",
    }
)
LAUNCH_INTENT_ID_EXCLUDE = frozenset({"event_id"})
RUNTIME_EVIDENCE_V2_SCHEMA_VERSION = "ai-runtime-evidence-2"
RUNTIME_EVIDENCE_V2_LEDGER = "runtime-evidence-v2.jsonl"
RUNTIME_EVIDENCE_V2_FIELDS = frozenset(
    {
        "schema_version",
        "evidence_id",
        "task_id",
        "envelope_hash",
        "event_index",
        "observed_agent_type",
        "native_agent_id",
        "native_thread_id",
        "fork_state",
        "nested_state",
        "recorded_at_utc",
    }
)
RUNTIME_EVIDENCE_ID_EXCLUDE = frozenset({"evidence_id"})
FORK_STATES = frozenset({"VERIFIED_NONE", "VERIFIED_PRESENT", "AUTHORITY_UNAVAILABLE"})
NESTED_STATES = frozenset({"VERIFIED_NONE", "VERIFIED_PRESENT", "AUTHORITY_UNAVAILABLE"})
_IDENTITY_KEYS = ("observed_agent_type", "native_agent_id", "native_thread_id")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        _fail("INVALID_TYPE", f"{field} must be a string")
    if not value.strip():
        _fail("EMPTY_FIELD", f"{field} must not be empty")
    return value


def _hex64(value: object, field: str) -> str:
    digest = _string(value, field)
    if not _HEX64.fullmatch(digest):
        _fail("INVALID_RECORD", f"{field} must be a SHA256 digest")
    return digest


def _sha256_canonical(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _load_task(store: TaskStoreProtocol, task_id: str) -> Mapping[str, object]:
    try:
        return load_artifact(store._require_task(task_id) / "task.json")
    except ArtifactError as exc:
        raise WorkflowError(exc.code, exc.message) from exc


def _envelope_hash(store: TaskStoreProtocol, task_id: str) -> str:
    return artifact_sha256(_load_task(store, task_id))


def _launch_intent_preimage(event: Mapping[str, object]) -> dict[str, object]:
    return {key: event[key] for key in LAUNCH_INTENT_EVENT_FIELDS}


def _evidence_preimage(record: Mapping[str, object]) -> dict[str, object]:
    return {key: record[key] for key in RUNTIME_EVIDENCE_V2_FIELDS}


def compute_launch_intent_id(event: Mapping[str, object]) -> str:
    return content_id(
        LAUNCH_INTENT_SCHEMA_KIND,
        _launch_intent_preimage(event),
        exclude=LAUNCH_INTENT_ID_EXCLUDE,
    )


def verify_launch_intent_id(event: Mapping[str, object]) -> None:
    verify_content_id(
        LAUNCH_INTENT_SCHEMA_KIND,
        _launch_intent_preimage(event),
        exclude=LAUNCH_INTENT_ID_EXCLUDE,
        id_field="event_id",
    )


def compute_evidence_id(record: Mapping[str, object]) -> str:
    return content_id(
        RUNTIME_EVIDENCE_V2_SCHEMA_VERSION,
        _evidence_preimage(record),
        exclude=RUNTIME_EVIDENCE_ID_EXCLUDE,
    )


def verify_evidence_id(record: Mapping[str, object]) -> None:
    verify_content_id(
        RUNTIME_EVIDENCE_V2_SCHEMA_VERSION,
        _evidence_preimage(record),
        exclude=RUNTIME_EVIDENCE_ID_EXCLUDE,
        id_field="evidence_id",
    )


def _has_identity_fields(observed: Mapping[str, object]) -> bool:
    has_agent_type = "observed_agent_type" in observed or "agent_type" in observed
    return (
        has_agent_type
        and "native_agent_id" in observed
        and "native_thread_id" in observed
    )


def _axis_state(observed: Mapping[str, object], key: str) -> str:
    if key not in observed:
        return "AUTHORITY_UNAVAILABLE"
    value = observed[key]
    if not isinstance(value, bool):
        _fail("INVALID_TYPE", f"{key} must be a boolean observation")
    return "VERIFIED_PRESENT" if value else "VERIFIED_NONE"


def derive_fork_nested_states(observed: Mapping[str, object]) -> tuple[str, str]:
    if not isinstance(observed, Mapping):
        _fail("INVALID_TYPE", "observed runtime metadata must be an object")
    if not _has_identity_fields(observed):
        return ("AUTHORITY_UNAVAILABLE", "AUTHORITY_UNAVAILABLE")
    return (_axis_state(observed, "fork"), _axis_state(observed, "nested"))


def _observed_identity(observed: Mapping[str, object]) -> tuple[object, object, object]:
    if "observed_agent_type" in observed:
        agent_type = observed["observed_agent_type"]
    else:
        agent_type = observed.get("agent_type")
    return (
        agent_type,
        observed.get("native_agent_id"),
        observed.get("native_thread_id"),
    )


def validate_runtime_evidence_v2(record: Mapping[str, object]) -> None:
    if not isinstance(record, Mapping):
        _fail("INVALID_TYPE", "runtime evidence record must be an object")
    extra = set(record) - RUNTIME_EVIDENCE_V2_FIELDS
    if extra:
        _fail("UNKNOWN_FIELD", f"unsupported field {sorted(extra)[0]}")
    missing = RUNTIME_EVIDENCE_V2_FIELDS - set(record)
    if missing:
        _fail("INVALID_RECORD", f"missing field {sorted(missing)[0]}")
    if record.get("schema_version") != RUNTIME_EVIDENCE_V2_SCHEMA_VERSION:
        _fail("INVALID_ENUM", "schema_version must be ai-runtime-evidence-2")
    _string(record["task_id"], "task_id")
    _hex64(record["evidence_id"], "evidence_id")
    _hex64(record["envelope_hash"], "envelope_hash")
    event_index = record["event_index"]
    if isinstance(event_index, bool) or not isinstance(event_index, int) or event_index < 0:
        _fail("INVALID_TYPE", "event_index must be a non-negative integer")
    if record.get("fork_state") not in FORK_STATES:
        _fail("INVALID_ENUM", "fork_state is outside the closed set")
    if record.get("nested_state") not in NESTED_STATES:
        _fail("INVALID_ENUM", "nested_state is outside the closed set")
    _string(record["recorded_at_utc"], "recorded_at_utc")
    for field in _IDENTITY_KEYS:
        value = record[field]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            _fail("INVALID_TYPE", f"{field} must be a string or null")


def record_launch_intent(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    permit: DispatchPermit,
    role: str,
    argv: Sequence[str],
    tool_mapping: Mapping[str, object],
) -> None:
    store._assert_lock_held(task_id)
    declaration = load_route_declaration_locked(store, task_id)
    if declaration is None:
        _fail("ROUTE_DECLARATION_MISSING", "route declaration is required before launch")
    permit_id = getattr(permit, "permit_id", None)
    event: dict[str, object] = {
        "event_type": LAUNCH_INTENT_EVENT_TYPE,
        "event_id": "0" * 64,
        "task_id": _string(task_id, "task_id"),
        "envelope_hash": _envelope_hash(store, task_id),
        "permit_id": _hex64(permit_id, "permit_id"),
        "role": _string(role, "role"),
        "command_sha256": _sha256_canonical(list(argv)),
        "tool_mapping_sha256": _sha256_canonical(dict(tool_mapping)),
        "route_config_hash": _hex64(declaration.route_config_hash, "route_config_hash"),
        "launcher_version": LAUNCHER_VERSION,
        "install_version": compute_install_version(),
        "timestamp_utc": _utc_now(),
    }
    event["event_id"] = compute_launch_intent_id(event)
    verify_launch_intent_id(event)
    store.append_event(task_id, event)


def append_runtime_evidence_v2(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    event_index: int,
    observed: Mapping[str, object],
    recorded_at_utc: str,
) -> None:
    fork_state, nested_state = derive_fork_nested_states(observed)
    agent_type, native_agent_id, native_thread_id = _observed_identity(observed)
    record: dict[str, object] = {
        "schema_version": RUNTIME_EVIDENCE_V2_SCHEMA_VERSION,
        "evidence_id": "0" * 64,
        "task_id": _string(task_id, "task_id"),
        "envelope_hash": _envelope_hash(store, task_id),
        "event_index": event_index,
        "observed_agent_type": agent_type,
        "native_agent_id": native_agent_id,
        "native_thread_id": native_thread_id,
        "fork_state": fork_state,
        "nested_state": nested_state,
        "recorded_at_utc": _string(recorded_at_utc, "recorded_at_utc"),
    }
    record["evidence_id"] = compute_evidence_id(record)
    validate_runtime_evidence_v2(record)
    verify_evidence_id(record)
    store.append_task_ledger(task_id, RUNTIME_EVIDENCE_V2_LEDGER, record)


def _read_events(store: TaskStoreProtocol, task_id: str) -> tuple[dict[str, object], ...]:
    try:
        return read_jsonl(store._require_task(task_id) / "events.jsonl", code="EVENTS")
    except WorkflowError as exc:
        if str(exc.code).endswith("_CORRUPT"):
            _fail("EVIDENCE_LEDGER_CORRUPT", exc.message)
        raise


def replay_runtime_evidence_v2(
    store: TaskStoreProtocol, task_id: str
) -> tuple[dict[str, object], ...]:
    try:
        rows = store.read_task_ledger(task_id, RUNTIME_EVIDENCE_V2_LEDGER)
    except WorkflowError as exc:
        if str(exc.code).endswith("_CORRUPT"):
            _fail("EVIDENCE_LEDGER_CORRUPT", exc.message)
        raise
    events = _read_events(store, task_id)
    seen_indexes: set[int] = set()
    loaded: list[dict[str, object]] = []
    for row in rows:
        try:
            if not isinstance(row, Mapping):
                _fail("EVIDENCE_LEDGER_CORRUPT", "runtime evidence record must be an object")
            payload = dict(row)
            if payload.get("task_id") != task_id:
                _fail("EVIDENCE_LEDGER_CORRUPT", "runtime evidence task_id does not match")
            validate_runtime_evidence_v2(payload)
            verify_evidence_id(payload)
            event_index = payload["event_index"]
            if event_index in seen_indexes:
                _fail("EVIDENCE_LEDGER_CORRUPT", "duplicate event_index")
            seen_indexes.add(event_index)
            if not isinstance(event_index, int) or event_index >= len(events):
                _fail("EVIDENCE_LEDGER_CORRUPT", "event_index is outside events.jsonl")
            pointed = events[event_index]
            if pointed.get("event_type") != "RUNTIME_EVIDENCE_RECORDED":
                _fail(
                    "EVIDENCE_LEDGER_CORRUPT",
                    "event_index does not point at RUNTIME_EVIDENCE_RECORDED",
                )
        except WorkflowError as exc:
            if exc.code == "EVIDENCE_LEDGER_CORRUPT":
                raise
            _fail("EVIDENCE_LEDGER_CORRUPT", exc.message)
        loaded.append(payload)
    return tuple(loaded)
