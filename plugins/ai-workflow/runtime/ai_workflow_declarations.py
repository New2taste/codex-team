"""Host-authored route declaration sidecar with unique-create and crash recovery."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

try:
    from .ai_workflow_artifacts import (
        ROLES,
        ROUTES,
        ArtifactError,
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        canonical_json,
        load_artifact,
        validate_route_decision,
    )
    from .ai_workflow_routing import RuntimeRouteDecision
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        ROLES,
        ROUTES,
        ArtifactError,
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        canonical_json,
        load_artifact,
        validate_route_decision,
    )
    from ai_workflow_routing import RuntimeRouteDecision


ROUTE_DECLARATION_SCHEMA_VERSION = "ai-route-declaration-1"
ROUTER_VERSION = "deterministic-router-1"
ROUTE_DECLARED_EVENT_TYPE = "ROUTE_DECLARED"
DECLARATION_FILENAME = "route-declaration.json"
ROUTE_DECLARATION_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "envelope_hash",
        "router_version",
        "route_config_hash",
        "selected_route",
        "allowed_roles",
        "active_roles",
        "rule_ids",
        "reason_codes",
        "max_dispatches",
        "allowed_transitions",
        "declared_at_utc",
    }
)
TRANSITION_FIELDS = frozenset({"from_role", "to_role"})


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        _fail("INVALID_TYPE", f"{field} must be a string")
    if not value.strip():
        _fail("EMPTY_FIELD", f"{field} must not be empty")
    return value


def _string_array(value: object, field: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list):
        _fail("INVALID_TYPE", f"{field} must be an array")
    if not allow_empty and not value:
        _fail("EMPTY_ARRAY", f"{field} must not be empty")
    if any(not isinstance(item, str) for item in value):
        _fail("INVALID_TYPE", f"{field} items must be strings")
    if any(not item.strip() for item in value):
        _fail("EMPTY_FIELD", f"{field} items must not be empty")
    if len(value) != len(set(value)):
        _fail("DUPLICATE_ITEM", f"{field} must not contain duplicates")
    return list(value)


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("INVALID_TYPE", f"{field} must be a non-negative integer")
    if value < 0:
        _fail("INVALID_TYPE", f"{field} must be a non-negative integer")
    return value


def _role(value: object, field: str) -> str:
    role = _string(value, field)
    if role not in ROLES:
        _fail("INVALID_ENUM", f"{field} is not supported")
    return role


@dataclass(frozen=True)
class RouteDeclaration:
    schema_version: str
    task_id: str
    envelope_hash: str
    router_version: str
    route_config_hash: str
    selected_route: str
    allowed_roles: tuple[str, ...]
    active_roles: tuple[str, ...]
    rule_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    max_dispatches: int
    allowed_transitions: tuple[Mapping[str, str], ...]
    declared_at_utc: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "envelope_hash": self.envelope_hash,
            "router_version": self.router_version,
            "route_config_hash": self.route_config_hash,
            "selected_route": self.selected_route,
            "allowed_roles": list(self.allowed_roles),
            "active_roles": list(self.active_roles),
            "rule_ids": list(self.rule_ids),
            "reason_codes": list(self.reason_codes),
            "max_dispatches": self.max_dispatches,
            "allowed_transitions": [dict(item) for item in self.allowed_transitions],
            "declared_at_utc": self.declared_at_utc,
        }


def validate_route_declaration(value: Mapping[str, object]) -> None:
    if not isinstance(value, Mapping):
        _fail("INVALID_TYPE", "route declaration must be an object")
    payload = dict(value)
    unknown = sorted(set(payload) - ROUTE_DECLARATION_FIELDS)
    if unknown:
        _fail("UNKNOWN_FIELD", f"unsupported field {unknown[0]}")
    missing = sorted(ROUTE_DECLARATION_FIELDS - set(payload))
    if missing:
        _fail("MISSING_FIELD", f"missing field {missing[0]}")
    if payload.get("schema_version") != ROUTE_DECLARATION_SCHEMA_VERSION:
        _fail("SCHEMA_VERSION", f"schema_version must be {ROUTE_DECLARATION_SCHEMA_VERSION}")
    _string(payload["task_id"], "task_id")
    _string(payload["envelope_hash"], "envelope_hash")
    if payload.get("router_version") != ROUTER_VERSION:
        _fail("INVALID_ENUM", f"router_version must be {ROUTER_VERSION}")
    _string(payload["route_config_hash"], "route_config_hash")
    selected = _string(payload["selected_route"], "selected_route")
    if selected not in ROUTES:
        _fail("INVALID_ENUM", "selected_route is not supported")
    allowed_roles = _string_array(payload["allowed_roles"], "allowed_roles", allow_empty=False)
    for index, role in enumerate(allowed_roles):
        _role(role, f"allowed_roles[{index}]")
    active_roles = _string_array(payload["active_roles"], "active_roles")
    allowed_set = set(allowed_roles)
    for index, role in enumerate(active_roles):
        _role(role, f"active_roles[{index}]")
        if role not in allowed_set:
            _fail("INVALID_ENUM", "active_roles must be a subset of allowed_roles")
    _string_array(payload["rule_ids"], "rule_ids")
    _string_array(payload["reason_codes"], "reason_codes")
    _non_negative_int(payload["max_dispatches"], "max_dispatches")
    transitions = payload["allowed_transitions"]
    if not isinstance(transitions, list):
        _fail("INVALID_TYPE", "allowed_transitions must be an array")
    for index, item in enumerate(transitions):
        if not isinstance(item, Mapping):
            _fail("INVALID_TYPE", f"allowed_transitions[{index}] must be an object")
        transition = dict(item)
        unknown_fields = sorted(set(transition) - TRANSITION_FIELDS)
        if unknown_fields:
            _fail(
                "UNKNOWN_FIELD",
                f"allowed_transitions[{index}] has unsupported field {unknown_fields[0]}",
            )
        missing_fields = sorted(TRANSITION_FIELDS - set(transition))
        if missing_fields:
            _fail(
                "MISSING_FIELD",
                f"allowed_transitions[{index}] is missing field {missing_fields[0]}",
            )
        _role(transition["from_role"], f"allowed_transitions[{index}].from_role")
        _role(transition["to_role"], f"allowed_transitions[{index}].to_role")
    _string(payload["declared_at_utc"], "declared_at_utc")


def compute_route_config_hash(config: Mapping[str, object]) -> str:
    if not isinstance(config, Mapping):
        _fail("INVALID_TYPE", "route config must be an object")
    return hashlib.sha256(canonical_json(dict(config)).encode("utf-8")).hexdigest()


def build_route_declaration(
    *,
    decision: RuntimeRouteDecision,
    route_config_hash: str,
    allowed_roles: tuple[str, ...],
    active_roles: tuple[str, ...],
    rule_ids: tuple[str, ...],
    reason_codes: tuple[str, ...],
    max_dispatches: int,
    allowed_transitions: tuple[Mapping[str, str], ...],
) -> RouteDeclaration:
    if not isinstance(decision, RuntimeRouteDecision):
        _fail("INVALID_TYPE", "decision must be a runtime route decision")
    declaration = RouteDeclaration(
        schema_version=ROUTE_DECLARATION_SCHEMA_VERSION,
        task_id=decision.task_id,
        envelope_hash=decision.task_sha256,
        router_version=ROUTER_VERSION,
        route_config_hash=route_config_hash,
        selected_route=decision.route,
        allowed_roles=tuple(allowed_roles),
        active_roles=tuple(active_roles),
        rule_ids=tuple(rule_ids),
        reason_codes=tuple(reason_codes),
        max_dispatches=max_dispatches,
        allowed_transitions=tuple(dict(item) for item in allowed_transitions),
        declared_at_utc=decision.decided_at_utc,
    )
    validate_route_declaration(declaration.to_dict())
    return declaration


def _declaration_from_mapping(value: Mapping[str, object]) -> RouteDeclaration:
    validate_route_declaration(value)
    transitions = value["allowed_transitions"]
    if not isinstance(transitions, list):
        _fail("INVALID_TYPE", "allowed_transitions must be an array")
    return RouteDeclaration(
        schema_version=_string(value["schema_version"], "schema_version"),
        task_id=_string(value["task_id"], "task_id"),
        envelope_hash=_string(value["envelope_hash"], "envelope_hash"),
        router_version=_string(value["router_version"], "router_version"),
        route_config_hash=_string(value["route_config_hash"], "route_config_hash"),
        selected_route=_string(value["selected_route"], "selected_route"),
        allowed_roles=tuple(
            _string_array(value["allowed_roles"], "allowed_roles", allow_empty=False)
        ),
        active_roles=tuple(_string_array(value["active_roles"], "active_roles")),
        rule_ids=tuple(_string_array(value["rule_ids"], "rule_ids")),
        reason_codes=tuple(_string_array(value["reason_codes"], "reason_codes")),
        max_dispatches=_non_negative_int(value["max_dispatches"], "max_dispatches"),
        allowed_transitions=tuple(dict(item) for item in transitions),
        declared_at_utc=_string(value["declared_at_utc"], "declared_at_utc"),
    )


def _event_from_declaration_bytes(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(
            "ROUTE_DECLARATION_CORRUPT", "route declaration is not valid JSON"
        ) from exc
    if not isinstance(value, Mapping):
        _fail("ROUTE_DECLARATION_CORRUPT", "route declaration must be an object")
    validate_route_declaration(value)
    return {
        "event_type": ROUTE_DECLARED_EVENT_TYPE,
        "task_id": value["task_id"],
        "envelope_hash": value["envelope_hash"],
        "selected_route": value["selected_route"],
        "declaration_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _read_route_declaration_bytes(store: TaskStoreProtocol, task_id: str) -> bytes | None:
    try:
        return (store._require_task(task_id) / "route-declaration.json").read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise WorkflowError(
            "ROUTE_DECLARATION_CORRUPT", "cannot read route declaration"
        ) from exc


def recover_route_declaration_event(store: TaskStoreProtocol, task_id: str) -> bool:
    store._assert_lock_held(task_id)
    raw = _read_route_declaration_bytes(store, task_id)
    events = store.read_task_ledger(task_id, "events.jsonl")
    has_event = any(
        event.get("event_type") == ROUTE_DECLARED_EVENT_TYPE for event in events
    )
    if raw is not None and not has_event:
        store.append_event(task_id, _event_from_declaration_bytes(raw))
        return True
    if has_event and raw is None:
        _fail(
            "ROUTE_DECLARATION_CORRUPT",
            "ROUTE_DECLARED event exists without declaration file",
        )
    return False


def load_route_declaration_locked(
    store: TaskStoreProtocol, task_id: str
) -> RouteDeclaration | None:
    store._assert_lock_held(task_id)
    recover_route_declaration_event(store, task_id)
    raw = _read_route_declaration_bytes(store, task_id)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError(
            "ROUTE_DECLARATION_CORRUPT", "route declaration is not valid JSON"
        ) from exc
    if not isinstance(value, Mapping):
        _fail("ROUTE_DECLARATION_CORRUPT", "route declaration must be an object")
    return _declaration_from_mapping(value)


def load_route_declaration(
    store: TaskStoreProtocol, task_id: str
) -> RouteDeclaration | None:
    try:
        with store.lock(task_id):
            return load_route_declaration_locked(store, task_id)
    except WorkflowError as exc:
        if exc.code == "TASK_NOT_FOUND":
            return None
        raise


def record_route_declaration(
    store: TaskStoreProtocol, task_id: str, declaration: RouteDeclaration
) -> Path:
    store._assert_lock_held(task_id)
    payload = declaration.to_dict()
    validate_route_declaration(payload)
    if declaration.task_id != task_id:
        _fail("ROUTE_DECLARATION_MISMATCH", "declaration task_id does not match task")
    task_dir = store._require_task(task_id)
    try:
        task = load_artifact(task_dir / "task.json")
    except ArtifactError as exc:
        raise WorkflowError(
            "ROUTE_DECLARATION_MISMATCH", "cannot read stored task envelope"
        ) from exc
    try:
        decision = load_artifact(task_dir / "route-decision.json")
    except ArtifactError as exc:
        raise WorkflowError(
            "ROUTE_DECLARATION_MISMATCH", "cannot read stored route decision"
        ) from exc
    validate_route_decision(decision)
    envelope = declaration.envelope_hash
    if artifact_sha256(task) != envelope or decision.get("task_sha256") != envelope:
        _fail(
            "ROUTE_DECLARATION_MISMATCH",
            "declaration envelope_hash does not match stored task and route decision",
        )
    if store.read_task_ledger(task_id, "dispatches.jsonl") or store.read_task_ledger(
        task_id, "dispatch-permits.jsonl"
    ):
        _fail("ROUTE_DECLARATION_LATE", "route declaration cannot follow dispatch records")
    path = store.write_task_artifact_once(
        task_id,
        DECLARATION_FILENAME,
        payload,
        conflict_code="ROUTE_DECLARATION_CONFLICT",
    )
    file_bytes = (canonical_json(payload) + "\n").encode("utf-8")
    store.append_event(
        task_id,
        {
            "event_type": ROUTE_DECLARED_EVENT_TYPE,
            "task_id": declaration.task_id,
            "envelope_hash": declaration.envelope_hash,
            "selected_route": declaration.selected_route,
            "declaration_sha256": hashlib.sha256(file_bytes).hexdigest(),
        },
    )
    return path


def ensure_route_declaration(
    store: TaskStoreProtocol, task_id: str, declaration: RouteDeclaration
) -> RouteDeclaration:
    store._assert_lock_held(task_id)
    existing = load_route_declaration_locked(store, task_id)
    if existing is None:
        record_route_declaration(store, task_id, declaration)
        loaded = load_route_declaration_locked(store, task_id)
        if loaded is None:
            _fail("ROUTE_DECLARATION_CORRUPT", "declaration missing after record")
        return loaded
    if canonical_json(existing.to_dict()) != canonical_json(declaration.to_dict()):
        _fail(
            "ROUTE_DECLARATION_CONFLICT",
            "route declaration differs from the frozen artifact",
        )
    return existing
