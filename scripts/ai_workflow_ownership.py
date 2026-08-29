"""Ownership registry sidecar and append-only side-effect ledger."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from posixpath import normpath as posix_normpath

try:
    from .ai_workflow_artifacts import (
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        load_artifact,
        read_jsonl,
    )
    from .ai_workflow_authorizations import (
        leases_for_permit,
        load_owner_authorization,
        record_transfer_lease_locked,
    )
    from .ai_workflow_planning import FrozenPlan, normalize_scope, scope_owner_map
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        load_artifact,
        read_jsonl,
    )
    from ai_workflow_authorizations import (
        leases_for_permit,
        load_owner_authorization,
        record_transfer_lease_locked,
    )
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
OWNERSHIP_VIOLATION_EVENT_FIELDS = frozenset(
    {
        "event_type",
        "task_id",
        "envelope_hash",
        "permit_id",
        "role",
        "paths",
        "timestamp_utc",
    }
)


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_envelope_hash(store: TaskStoreProtocol, task_id: str) -> str:
    task = load_artifact(store._require_task(task_id) / "task.json")
    if task.get("task_id") != task_id:
        _fail("INVALID_TASK", "task.json task_id does not match")
    return artifact_sha256(task)


def _lexical_repo_path(path: str) -> str:
    if not isinstance(path, str) or not path or path != path.strip():
        _fail("PLAN_INVALID", "scope must be a non-empty literal repository-relative path")
    collapsed = posix_normpath(path.replace("\\", "/"))
    if collapsed in {"", ".", ".."} or collapsed.startswith("../") or collapsed.startswith("/"):
        _fail("PLAN_INVALID", "scope must be a literal repository-relative path")
    return normalize_scope(collapsed).as_posix()


def _normalize_write_path(store: TaskStoreProtocol, task_id: str, path: str) -> str:
    lexical = _lexical_repo_path(path)
    try:
        task = load_artifact(store._require_task(task_id) / "task.json")
    except (OSError, ValueError, WorkflowError):
        return lexical
    root_value = task.get("source_worktree") or task.get("repository_root")
    if not isinstance(root_value, str) or not root_value.strip():
        root_value = task.get("repository_root")
    if not isinstance(root_value, str) or not root_value.strip():
        return lexical
    root = Path(root_value)
    try:
        resolved = (root / lexical).resolve()
        relative = resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        _fail("PLAN_INVALID", "scope must not escape the repository root")
        raise AssertionError("unreachable")
    return _lexical_repo_path(relative.as_posix())


def _path_covered(path: str, roots: set[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _longest_prefix_owner(registry: OwnershipRegistry, path: str) -> str | None:
    matches = [
        key
        for key in registry.path_owners
        if path == key or path.startswith(key + "/")
    ]
    if not matches:
        return None
    return registry.path_owners[max(matches, key=len)]


def resolve_path_owner(store: TaskStoreProtocol, task_id: str, path: str) -> str:
    registry = load_ownership_registry(store, task_id)
    if registry is None:
        _fail("OWNERSHIP_REGISTRY_MISSING", "ownership registry is not recorded")
        raise AssertionError("unreachable")
    normalized = _normalize_write_path(store, task_id, path)
    owner = _longest_prefix_owner(registry, normalized)
    if owner is None:
        _fail("OWNERSHIP_UNKNOWN", f"no owner for {normalized}")
        raise AssertionError("unreachable")
    return owner


def precheck_write_ownership(
    store: TaskStoreProtocol, task_id: str, role: str, *, paths: tuple[str, ...]
) -> str:
    _string(role, "role")
    if not isinstance(paths, tuple):
        _fail("INVALID_TYPE", "paths must be a tuple")
    if load_ownership_registry(store, task_id) is None:
        return "BLOCKED"
    needs_lease = False
    for path in paths:
        try:
            owner = resolve_path_owner(store, task_id, path)
        except WorkflowError as exc:
            if exc.code in {"OWNERSHIP_UNKNOWN", "PLAN_INVALID"}:
                return "BLOCKED"
            raise
        if owner != role:
            needs_lease = True
    return "LEASE_REQUIRED" if needs_lease else "OWNED"


def claimed_write_paths(plan_scopes: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(plan_scopes, tuple):
        _fail("INVALID_TYPE", "plan_scopes must be a tuple")
    return tuple(_lexical_repo_path(path) for path in plan_scopes)


def require_write_ownership_locked(
    store: TaskStoreProtocol,
    task_id: str,
    role: str,
    *,
    permit_id: str,
    paths: tuple[str, ...],
    authorization_id: str | None = None,
) -> None:
    store._assert_lock_held(task_id)
    role_text = _string(role, "role")
    permit = _string(permit_id, "permit_id")
    if not isinstance(paths, tuple):
        _fail("INVALID_TYPE", "paths must be a tuple")
    unowned: list[str] = []
    for path in paths:
        normalized = _normalize_write_path(store, task_id, path)
        owner = resolve_path_owner(store, task_id, normalized)
        if owner != role_text:
            unowned.append(normalized)
    if not unowned:
        return
    if authorization_id is None:
        _fail(
            "OWNERSHIP_TRANSFER_BLOCKED",
            "write requires an ownership transfer authorization",
        )
    authorization = load_owner_authorization(store, task_id, authorization_id)
    if authorization is None:
        _fail("AUTHORIZATION_UNKNOWN", "authorization is not in this task ledger")
        raise AssertionError("unreachable")
    if authorization.authorization_type != "OWNERSHIP_TRANSFER":
        _fail("AUTHORIZATION_SCOPE_MISMATCH", "authorization is not an ownership transfer")
    if authorization.to_role != role_text:
        _fail("AUTHORIZATION_SCOPE_MISMATCH", "authorization to_role does not match role")
    allowed = tuple(authorization.allowed_paths or ())
    locking = has_ownership_locking_side_effect(store, task_id)
    if locking and not allowed:
        _fail(
            "AUTHORIZATION_SCOPE_MISMATCH",
            "locking side effects require a focused allowed_paths set",
        )
    if locking and not set(unowned).issubset(set(allowed)):
        _fail("AUTHORIZATION_SCOPE_MISMATCH", "claimed paths are not in allowed_paths")
    auth_path = authorization.path or ""
    for path in unowned:
        owner = resolve_path_owner(store, task_id, path)
        if authorization.from_role != owner:
            _fail(
                "AUTHORIZATION_SCOPE_MISMATCH",
                "authorization from_role does not match path owner",
            )
        if not (
            path == auth_path
            or path.startswith(auth_path + "/")
            or path in allowed
        ):
            _fail("AUTHORIZATION_SCOPE_MISMATCH", "path is not bound to the authorization")
    record_transfer_lease_locked(
        store,
        task_id,
        authorization.authorization_id,
        permit_id=permit,
        paths=tuple(unowned),
    )


def verify_actual_write_paths(
    store: TaskStoreProtocol,
    task_id: str,
    role: str,
    *,
    permit_id: str,
    actual_paths: tuple[str, ...],
) -> None:
    if actual_paths is None:
        _fail("ACTUAL_WRITE_PATHS_UNKNOWN", "actual write paths are unknown")
    if not isinstance(actual_paths, tuple):
        _fail("INVALID_TYPE", "actual_paths must be a tuple")
    role_text = _string(role, "role")
    permit = _string(permit_id, "permit_id")
    registry = load_ownership_registry(store, task_id)
    if registry is None:
        _fail("OWNERSHIP_REGISTRY_MISSING", "ownership registry is not recorded")
        raise AssertionError("unreachable")
    lease_roots: set[str] = set()
    for row in leases_for_permit(store, task_id, permit):
        for item in row.get("allowed_paths") or ():
            lease_roots.add(_lexical_repo_path(str(item)))
    out_of_bounds: list[str] = []
    for path in actual_paths:
        if not isinstance(path, str):
            _fail("INVALID_TYPE", "actual_paths items must be strings")
        normalized = _normalize_write_path(store, task_id, path)
        owned = False
        try:
            owned = resolve_path_owner(store, task_id, normalized) == role_text
        except WorkflowError as exc:
            if exc.code != "OWNERSHIP_UNKNOWN":
                raise
        if owned or _path_covered(normalized, lease_roots):
            continue
        out_of_bounds.append(normalized)
    if not out_of_bounds:
        return
    event: dict[str, object] = {
        "event_type": OWNERSHIP_VIOLATION_EVENT_TYPE,
        "task_id": _string(task_id, "task_id"),
        "envelope_hash": _task_envelope_hash(store, task_id),
        "permit_id": permit,
        "role": role_text,
        "paths": sorted(set(out_of_bounds)),
        "timestamp_utc": _utc_now(),
    }
    store.append_event(task_id, event)
    _fail("OWNERSHIP_VIOLATION", "actual write paths exceed the permit allow-set")


def _replay_ownership_violation_events(
    store: TaskStoreProtocol, task_id: str
) -> tuple[dict[str, object], ...]:
    path = store._require_task(task_id) / "events.jsonl"
    try:
        rows = read_jsonl(path, code="OWNERSHIP_VIOLATION_LEDGER")
    except WorkflowError as exc:
        if str(exc.code).endswith("_CORRUPT"):
            _fail("OWNERSHIP_VIOLATION_LEDGER_CORRUPT", exc.message)
        raise
    found: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            _fail("OWNERSHIP_VIOLATION_LEDGER_CORRUPT", "event record must be an object")
        payload = dict(row)
        if payload.get("event_type") != OWNERSHIP_VIOLATION_EVENT_TYPE:
            continue
        if set(payload) != OWNERSHIP_VIOLATION_EVENT_FIELDS:
            _fail(
                "OWNERSHIP_VIOLATION_LEDGER_CORRUPT",
                "violation event fields are not closed",
            )
        if payload.get("task_id") != task_id:
            _fail(
                "OWNERSHIP_VIOLATION_LEDGER_CORRUPT",
                "violation event task_id does not match",
            )
        for field in (
            "event_type",
            "task_id",
            "envelope_hash",
            "permit_id",
            "role",
            "timestamp_utc",
        ):
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                _fail("OWNERSHIP_VIOLATION_LEDGER_CORRUPT", f"{field} must be a string")
        paths = payload.get("paths")
        if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
            _fail("OWNERSHIP_VIOLATION_LEDGER_CORRUPT", "paths must be a string list")
        found.append(payload)
    return tuple(found)


def has_unresolved_ownership_violation_locked(
    store: TaskStoreProtocol, task_id: str
) -> bool:
    store._assert_lock_held(task_id)
    return bool(_replay_ownership_violation_events(store, task_id))


def has_unresolved_ownership_violation(store: TaskStoreProtocol, task_id: str) -> bool:
    with store.lock(task_id):
        return has_unresolved_ownership_violation_locked(store, task_id)
