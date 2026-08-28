"""Ownership registry sidecar and append-only side-effect ledger."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

try:
    from .ai_workflow_artifacts import TaskStoreProtocol, WorkflowError
    from .ai_workflow_planning import FrozenPlan, normalize_scope, scope_owner_map
except ImportError:  # direct script execution
    from ai_workflow_artifacts import TaskStoreProtocol, WorkflowError
    from ai_workflow_planning import FrozenPlan, normalize_scope, scope_owner_map


OWNERSHIP_REGISTRY_SCHEMA_VERSION = "ai-ownership-registry-1"
OWNERSHIP_REGISTRY_FILENAME = "ownership-registry.json"
OWNERSHIP_REGISTRY_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "envelope_hash",
        "path_owners",
        "registered_at_utc",
    }
)
SIDE_EFFECT_SCHEMA_VERSION = "ai-side-effect-1"
SIDE_EFFECT_LEDGER = "side-effects.jsonl"
SIDE_EFFECT_RECORDED_EVENT_TYPE = "SIDE_EFFECT_RECORDED"
SIDE_EFFECT_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "role",
        "path",
        "effect_kind",
        "permit_id",
        "producer",
        "producer_ref",
        "command_sha256s",
    }
)
SIDE_EFFECT_REQUIRED_FIELDS = frozenset(
    {"schema_version", "task_id", "role", "path", "effect_kind"}
)
EFFECT_KINDS = frozenset(
    {
        "CONTROL_PLANE_ARTIFACT",
        "OWNED_WRITE",
        "UNTRACKED_WRITE",
        "COMMAND_GENERATED",
        "EXTERNAL",
        "UNOBSERVED_ASSUMED_PRESENT",
    }
)
LOCKING_EFFECT_KINDS = frozenset(
    {
        "OWNED_WRITE",
        "UNTRACKED_WRITE",
        "COMMAND_GENERATED",
        "EXTERNAL",
        "UNOBSERVED_ASSUMED_PRESENT",
    }
)
OWNERSHIP_VIOLATION_EVENT_TYPE = "OWNERSHIP_VIOLATION_RECORDED"


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        _fail("INVALID_TYPE", f"{field} must be a string")
    if not value.strip():
        _fail("EMPTY_FIELD", f"{field} must not be empty")
    return value


@dataclass(frozen=True)
class OwnershipRegistry:
    schema_version: str
    task_id: str
    envelope_hash: str
    path_owners: dict[str, str]
    registered_at_utc: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "envelope_hash": self.envelope_hash,
            "path_owners": dict(self.path_owners),
            "registered_at_utc": self.registered_at_utc,
        }


def validate_ownership_registry(value: Mapping[str, object]) -> None:
    if not isinstance(value, Mapping):
        _fail("INVALID_TYPE", "ownership registry must be an object")
    payload = dict(value)
    unknown = sorted(set(payload) - OWNERSHIP_REGISTRY_FIELDS)
    if unknown:
        _fail("UNKNOWN_FIELD", f"unsupported field {unknown[0]}")
    missing = sorted(OWNERSHIP_REGISTRY_FIELDS - set(payload))
    if missing:
        _fail("MISSING_FIELD", f"missing field {missing[0]}")
    if payload.get("schema_version") != OWNERSHIP_REGISTRY_SCHEMA_VERSION:
        _fail(
            "SCHEMA_VERSION",
            f"schema_version must be {OWNERSHIP_REGISTRY_SCHEMA_VERSION}",
        )
    _string(payload["task_id"], "task_id")
    _string(payload["envelope_hash"], "envelope_hash")
    _string(payload["registered_at_utc"], "registered_at_utc")
    owners = payload["path_owners"]
    if not isinstance(owners, Mapping):
        _fail("INVALID_TYPE", "path_owners must be an object")
    for path, owner in owners.items():
        _string(path, "path_owners")
        _string(owner, "path_owners")
        normalize_scope(path)


def _registry_from_mapping(value: Mapping[str, object]) -> OwnershipRegistry:
    validate_ownership_registry(value)
    owners = value["path_owners"]
    if not isinstance(owners, Mapping):
        _fail("INVALID_TYPE", "path_owners must be an object")
    return OwnershipRegistry(
        schema_version=_string(value["schema_version"], "schema_version"),
        task_id=_string(value["task_id"], "task_id"),
        envelope_hash=_string(value["envelope_hash"], "envelope_hash"),
        path_owners={
            normalize_scope(str(path)).as_posix(): _string(owner, "path_owners")
            for path, owner in owners.items()
        },
        registered_at_utc=_string(value["registered_at_utc"], "registered_at_utc"),
    )


def build_ownership_registry(
    *,
    task_id: str,
    envelope_hash: str,
    plan: FrozenPlan,
    registered_at_utc: str,
) -> OwnershipRegistry:
    owners = scope_owner_map(plan)
    registry = OwnershipRegistry(
        schema_version=OWNERSHIP_REGISTRY_SCHEMA_VERSION,
        task_id=_string(task_id, "task_id"),
        envelope_hash=_string(envelope_hash, "envelope_hash"),
        path_owners={
            normalize_scope(path).as_posix(): owner for path, owner in owners.items()
        },
        registered_at_utc=_string(registered_at_utc, "registered_at_utc"),
    )
    validate_ownership_registry(registry.to_dict())
    return registry


def record_ownership_registry(
    store: TaskStoreProtocol, task_id: str, registry: OwnershipRegistry
) -> Path:
    store._assert_lock_held(task_id)
    payload = registry.to_dict()
    validate_ownership_registry(payload)
    if registry.task_id != task_id:
        _fail("OWNERSHIP_REGISTRY_MISMATCH", "registry task_id does not match task")
    return store.write_task_artifact_once(
        task_id,
        OWNERSHIP_REGISTRY_FILENAME,
        payload,
        conflict_code="OWNERSHIP_REGISTRY_CONFLICT",
    )


def load_ownership_registry(
    store: TaskStoreProtocol, task_id: str
) -> OwnershipRegistry | None:
    try:
        raw = (store._require_task(task_id) / OWNERSHIP_REGISTRY_FILENAME).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkflowError(
            "OWNERSHIP_REGISTRY_CORRUPT", "cannot read ownership registry"
        ) from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(
            "OWNERSHIP_REGISTRY_CORRUPT", "ownership registry is not valid JSON"
        ) from exc
    if not isinstance(value, Mapping):
        _fail("OWNERSHIP_REGISTRY_CORRUPT", "ownership registry must be an object")
    return _registry_from_mapping(value)


def record_side_effect_locked(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    role: str,
    path: str,
    effect_kind: str,
    permit_id: str | None = None,
    extra: Mapping[str, object] | None = None,
) -> None:
    store._assert_lock_held(task_id)
    if effect_kind not in EFFECT_KINDS:
        _fail("INVALID_ENUM", "effect_kind is not supported")
    record: dict[str, object] = {
        "schema_version": SIDE_EFFECT_SCHEMA_VERSION,
        "task_id": _string(task_id, "task_id"),
        "role": _string(role, "role"),
        "path": normalize_scope(path).as_posix(),
        "effect_kind": effect_kind,
    }
    if permit_id is not None:
        record["permit_id"] = _string(permit_id, "permit_id")
    if extra is not None:
        if not isinstance(extra, Mapping):
            _fail("INVALID_TYPE", "extra must be an object")
        for key, value in extra.items():
            if key in SIDE_EFFECT_REQUIRED_FIELDS or key not in SIDE_EFFECT_FIELDS:
                _fail("UNKNOWN_FIELD", f"unsupported field {key}")
            if key in record:
                _fail("UNKNOWN_FIELD", f"unsupported field {key}")
            record[key] = value
    store.append_task_ledger(task_id, SIDE_EFFECT_LEDGER, record)
    event: dict[str, object] = {
        "event_type": SIDE_EFFECT_RECORDED_EVENT_TYPE,
        "task_id": record["task_id"],
        "role": record["role"],
        "path": record["path"],
        "effect_kind": record["effect_kind"],
    }
    if "permit_id" in record:
        event["permit_id"] = record["permit_id"]
    store.append_event(task_id, event)


def record_side_effect(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    role: str,
    path: str,
    effect_kind: str,
    permit_id: str | None = None,
    extra: Mapping[str, object] | None = None,
) -> None:
    with store.lock(task_id):
        record_side_effect_locked(
            store,
            task_id,
            role=role,
            path=path,
            effect_kind=effect_kind,
            permit_id=permit_id,
            extra=extra,
        )


def load_side_effects(
    store: TaskStoreProtocol, task_id: str
) -> tuple[dict[str, object], ...]:
    try:
        rows = store.read_task_ledger(task_id, SIDE_EFFECT_LEDGER)
    except WorkflowError as exc:
        if str(exc.code).endswith("_CORRUPT"):
            _fail("SIDE_EFFECT_LEDGER_CORRUPT", exc.message)
        raise
    loaded: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            _fail("SIDE_EFFECT_LEDGER_CORRUPT", "side-effect record must be an object")
        payload = dict(row)
        if payload.get("task_id") != task_id:
            _fail("SIDE_EFFECT_LEDGER_CORRUPT", "side-effect record task_id does not match")
        if payload.get("effect_kind") not in EFFECT_KINDS:
            _fail("SIDE_EFFECT_LEDGER_CORRUPT", "effect_kind is not supported")
        loaded.append(payload)
    return tuple(loaded)


def has_ownership_locking_side_effect(store: TaskStoreProtocol, task_id: str) -> bool:
    return any(
        row.get("effect_kind") in LOCKING_EFFECT_KINDS
        for row in load_side_effects(store, task_id)
    )
