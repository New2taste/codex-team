"""Dispatch-permit state machine with single-transaction locked primitives."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    from .ai_workflow_artifacts import (
        ArtifactError,
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        canonical_json,
        load_artifact,
        read_jsonl,
        validate_route_decision,
    )
    from .ai_workflow_declarations import (
        RouteDeclaration,
        build_route_declaration,
        compute_route_config_hash,
        ensure_route_declaration,
        load_route_declaration_locked,
    )
    from .ai_workflow_ownership import has_unresolved_ownership_violation_locked
    from .ai_workflow_preflight import require_role_preflighted_locked
    from .ai_workflow_routing import RuntimeRouteDecision
    from .ai_workflow_side_effects import (
        derive_effectful_roles,
        record_external_side_effect_locked,
    )
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        ArtifactError,
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        canonical_json,
        load_artifact,
        read_jsonl,
        validate_route_decision,
    )
    from ai_workflow_declarations import (
        RouteDeclaration,
        build_route_declaration,
        compute_route_config_hash,
        ensure_route_declaration,
        load_route_declaration_locked,
    )
    from ai_workflow_ownership import has_unresolved_ownership_violation_locked
    from ai_workflow_preflight import require_role_preflighted_locked
    from ai_workflow_routing import RuntimeRouteDecision
    from ai_workflow_side_effects import (
        derive_effectful_roles,
        record_external_side_effect_locked,
    )


DISPATCH_PERMIT_LEDGER = "dispatch-permits.jsonl"
DISPATCH_PERMIT_SCHEMA_VERSION = "ai-dispatch-permit-1"
DISPATCH_PERMIT_FIELDS = frozenset(
    {
        "schema_version",
        "seq",
        "permit_id",
        "task_id",
        "role",
        "state",
        "reason",
        "recorded_at_utc",
    }
)
PERMIT_STATES = frozenset({"RESERVED", "STARTED", "RELEASED_BEFORE_START"})
PERMIT_TERMINAL_STATES = frozenset({"STARTED", "RELEASED_BEFORE_START"})
ROLE_ACTIVATED_EVENT_TYPE = "ROLE_ACTIVATED"
_ACTIVE_BUDGET_STATES = frozenset({"RESERVED", "STARTED"})


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        _fail("INVALID_TYPE", f"{field} must be a string")
    if not value.strip():
        _fail("EMPTY_FIELD", f"{field} must not be empty")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _positive_seq(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "seq must be a positive integer")
    return value


@dataclass(frozen=True)
class DispatchPermit:
    permit_id: str
    task_id: str
    role: str
    reservation_seq: int


def derive_dispatch_identity(*, task_sha256: str, role: str, attempt_id: str) -> str:
    payload = {
        "attempt_id": _string(attempt_id, "attempt_id"),
        "role": _string(role, "role"),
        "task_sha256": _string(task_sha256, "task_sha256"),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def derive_assignment_dispatch_identity(
    *, task_sha256: str, assignment_id: str, attempt_id: str
) -> str:
    payload = {
        "assignment_id": _string(assignment_id, "assignment_id"),
        "attempt_id": _string(attempt_id, "attempt_id"),
        "task_sha256": _string(task_sha256, "task_sha256"),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def permit_latest_states(records: tuple[dict[str, object], ...]) -> dict[str, str]:
    latest: dict[str, str] = {}
    for record in records:
        permit_id = record.get("permit_id")
        state = record.get("state")
        if isinstance(permit_id, str) and isinstance(state, str):
            latest[permit_id] = state
    return latest


def replay_permit_ledger(
    store: TaskStoreProtocol, task_id: str
) -> tuple[dict[str, object], ...]:
    try:
        rows = store.read_task_ledger(task_id, DISPATCH_PERMIT_LEDGER)
    except WorkflowError as exc:
        if str(exc.code).endswith("_CORRUPT"):
            _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", exc.message)
        raise
    latest: dict[str, str] = {}
    seen_seq: set[int] = set()
    loaded: list[dict[str, object]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "permit record must be an object")
        payload = dict(row)
        if set(payload) != DISPATCH_PERMIT_FIELDS:
            _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "permit record fields are not closed")
        if payload.get("schema_version") != DISPATCH_PERMIT_SCHEMA_VERSION:
            _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "permit schema_version is invalid")
        if payload.get("task_id") != task_id:
            _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "permit record task_id does not match")
        seq = _positive_seq(payload.get("seq"))
        if seq in seen_seq or seq != index:
            _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "permit seq is not contiguous from 1")
        seen_seq.add(seq)
        permit_id = _string(payload.get("permit_id"), "permit_id")
        _string(payload.get("role"), "role")
        _string(payload.get("recorded_at_utc"), "recorded_at_utc")
        state = payload.get("state")
        if state not in PERMIT_STATES:
            _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "permit state is not supported")
        reason = payload.get("reason")
        if not isinstance(reason, str):
            _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "reason must be a string")
        if state == "RELEASED_BEFORE_START":
            if not reason.strip():
                _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "release reason must not be empty")
        elif reason:
            _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "reason must be empty unless released")
        previous = latest.get(permit_id)
        if previous is None:
            if state != "RESERVED":
                _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "illegal permit state transition")
        elif previous == "RESERVED":
            if state not in {"STARTED", "RELEASED_BEFORE_START"}:
                _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "illegal permit state transition")
        else:
            _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", "illegal permit state transition")
        latest[permit_id] = str(state)
        loaded.append(payload)
    return tuple(loaded)


def derive_active_roles(
    store: TaskStoreProtocol, task_id: str, declaration: RouteDeclaration
) -> frozenset[str]:
    active = set(declaration.active_roles)
    allowed = {
        (item["from_role"], item["to_role"]) for item in declaration.allowed_transitions
    }
    path = store._require_task(task_id) / "events.jsonl"
    try:
        rows = read_jsonl(path, code="EVENTS")
    except WorkflowError as exc:
        if str(exc.code).endswith("_CORRUPT"):
            _fail("DISPATCH_PERMIT_LEDGER_CORRUPT", exc.message)
        raise
    for row in rows:
        if row.get("event_type") != ROLE_ACTIVATED_EVENT_TYPE:
            continue
        from_role = row.get("from_role")
        to_role = row.get("to_role")
        if not isinstance(from_role, str) or not isinstance(to_role, str):
            continue
        if from_role in active and (from_role, to_role) in allowed:
            active.add(to_role)
    return frozenset(active)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail("INVALID_TYPE", f"{field} must be an object")
    return value


def _build_declaration_from_decision(
    decision: RuntimeRouteDecision, config: Mapping[str, object]
) -> RouteDeclaration:
    payload = _mapping(config, "config")
    roles = tuple(dict.fromkeys((*decision.roles, *decision.effective_roles)))
    if not roles:
        _fail("ROLE_NOT_ALLOWED", "route decision has no roles")
    active = tuple(decision.effective_roles) if decision.effective_roles else tuple(decision.roles)
    policy_cfg = payload.get("policy")
    retries = 0
    if isinstance(policy_cfg, Mapping):
        raw_retries = policy_cfg.get("max_technical_retries", 0)
        if isinstance(raw_retries, int) and not isinstance(raw_retries, bool) and raw_retries >= 0:
            retries = raw_retries
    reason_codes_raw = payload.get("reason_codes", ())
    if isinstance(reason_codes_raw, (list, tuple)):
        reason_codes = tuple(str(item) for item in reason_codes_raw)
    else:
        reason_codes = ()
    transitions_raw = payload.get("allowed_transitions", ())
    transitions: list[Mapping[str, str]] = []
    if isinstance(transitions_raw, (list, tuple)):
        for item in transitions_raw:
            if isinstance(item, Mapping):
                transitions.append(dict(item))
    extra_roles: list[str] = []
    if "sol_reviewer" in roles and "sol_xhigh" not in roles:
        extra_roles.append("sol_xhigh")
        transitions.append({"from_role": "sol_reviewer", "to_role": "sol_xhigh"})
    if "sol_planner" in roles and "sol_xhigh_planner" not in roles:
        extra_roles.append("sol_xhigh_planner")
        transitions.append({"from_role": "sol_planner", "to_role": "sol_xhigh_planner"})
    if extra_roles:
        roles = tuple(dict.fromkeys((*roles, *extra_roles)))
    max_dispatches = len(roles) * (1 + retries)
    return build_route_declaration(
        decision=decision,
        route_config_hash=compute_route_config_hash(payload),
        allowed_roles=roles,
        active_roles=active,
        rule_ids=(decision.rule_id,),
        reason_codes=reason_codes,
        max_dispatches=max_dispatches,
        allowed_transitions=tuple(transitions),
    )


def ensure_declaration_for_task(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    decision: RuntimeRouteDecision,
    config: Mapping[str, object],
) -> RouteDeclaration:
    store._assert_lock_held(task_id)
    load_route_declaration_locked(store, task_id)
    built = _build_declaration_from_decision(decision, config)
    return ensure_route_declaration(store, task_id, built)


def activate_role(
    store: TaskStoreProtocol, task_id: str, *, from_role: str, to_role: str
) -> None:
    store._assert_lock_held(task_id)
    source = _string(from_role, "from_role")
    target = _string(to_role, "to_role")
    declaration = load_route_declaration_locked(store, task_id)
    if declaration is None:
        _fail("ROUTE_DECLARATION_MISSING", "route declaration is missing")
        raise AssertionError("unreachable")
    allowed = {
        (item["from_role"], item["to_role"]) for item in declaration.allowed_transitions
    }
    if (source, target) not in allowed:
        _fail("ROUTE_TRANSITION_BLOCKED", "role transition is not allowed")
    store.append_event(
        task_id,
        {
            "event_type": ROLE_ACTIVATED_EVENT_TYPE,
            "task_id": task_id,
            "from_role": source,
            "to_role": target,
        },
    )


def _assert_envelope(
    store: TaskStoreProtocol, task_id: str, declaration: RouteDeclaration
) -> None:
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


def _append_permit_record(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    permit_id: str,
    role: str,
    state: str,
    reason: str = "",
) -> int:
    records = replay_permit_ledger(store, task_id)
    seq = len(records) + 1
    record = {
        "schema_version": DISPATCH_PERMIT_SCHEMA_VERSION,
        "seq": seq,
        "permit_id": permit_id,
        "task_id": task_id,
        "role": role,
        "state": state,
        "reason": reason if state == "RELEASED_BEFORE_START" else "",
        "recorded_at_utc": _utc_now(),
    }
    store.append_task_ledger(task_id, DISPATCH_PERMIT_LEDGER, record)
    return seq


def require_dispatch_permit_locked(
    store: TaskStoreProtocol,
    task_id: str,
    role: str,
    *,
    dispatch_identity: str,
    config: Mapping[str, object],
) -> DispatchPermit:
    store._assert_lock_held(task_id)
    identity = _string(dispatch_identity, "dispatch_identity")
    role_text = _string(role, "role")
    declaration = load_route_declaration_locked(store, task_id)
    if declaration is None:
        _fail("ROUTE_DECLARATION_MISSING", "route declaration is missing")
        raise AssertionError("unreachable")
    _assert_envelope(store, task_id, declaration)
    if has_unresolved_ownership_violation_locked(store, task_id):
        _fail(
            "DISPATCH_BLOCKED_OWNERSHIP_VIOLATION",
            "unresolved ownership violation blocks dispatch",
        )
    if role_text not in declaration.allowed_roles:
        _fail("ROLE_NOT_ALLOWED", f"role {role_text} is not allowed")
    if role_text not in derive_active_roles(store, task_id, declaration):
        _fail("ROUTE_TRANSITION_BLOCKED", f"role {role_text} is not active")
    require_role_preflighted_locked(store, task_id, role_text)
    records = replay_permit_ledger(store, task_id)
    latest = permit_latest_states(records)
    current = latest.get(identity)
    if current == "RESERVED":
        _fail("DISPATCH_PERMIT_UNCLAIMED", "dispatch identity is reserved but unclaimed")
    if current == "STARTED":
        _fail("DISPATCH_PERMIT_ALREADY_STARTED", "dispatch identity already started")
    if current == "RELEASED_BEFORE_START":
        _fail("DISPATCH_IDENTITY_RETIRED", "dispatch identity is permanently retired")
    active_count = sum(1 for state in latest.values() if state in _ACTIVE_BUDGET_STATES)
    if active_count >= declaration.max_dispatches:
        _fail("ROUTE_BUDGET_EXCEEDED", "dispatch permit budget is exhausted")
    seq = _append_permit_record(
        store, task_id, permit_id=identity, role=role_text, state="RESERVED"
    )
    if role_text in derive_effectful_roles(config):
        record_external_side_effect_locked(
            store, task_id, role=role_text, permit_id=identity
        )
    return DispatchPermit(
        permit_id=identity,
        task_id=task_id,
        role=role_text,
        reservation_seq=seq,
    )


def require_dispatch_permit(
    store: TaskStoreProtocol,
    task_id: str,
    role: str,
    *,
    dispatch_identity: str,
    config: Mapping[str, object],
) -> DispatchPermit:
    with store.lock(task_id):
        return require_dispatch_permit_locked(
            store,
            task_id,
            role,
            dispatch_identity=dispatch_identity,
            config=config,
        )


def precheck_dispatch_permit_locked(
    store: TaskStoreProtocol, task_id: str, role: str, *, config: Mapping[str, object]
) -> None:
    store._assert_lock_held(task_id)
    _mapping(config, "config")
    role_text = _string(role, "role")
    declaration = load_route_declaration_locked(store, task_id)
    if declaration is None:
        _fail("ROUTE_DECLARATION_MISSING", "route declaration is missing")
        raise AssertionError("unreachable")
    _assert_envelope(store, task_id, declaration)
    if has_unresolved_ownership_violation_locked(store, task_id):
        _fail(
            "DISPATCH_BLOCKED_OWNERSHIP_VIOLATION",
            "unresolved ownership violation blocks dispatch",
        )
    if role_text not in declaration.allowed_roles:
        _fail("ROLE_NOT_ALLOWED", f"role {role_text} is not allowed")
    if role_text not in derive_active_roles(store, task_id, declaration):
        _fail("ROUTE_TRANSITION_BLOCKED", f"role {role_text} is not active")
    require_role_preflighted_locked(store, task_id, role_text)


def precheck_dispatch_permit(
    store: TaskStoreProtocol, task_id: str, role: str, *, config: Mapping[str, object]
) -> None:
    with store.lock(task_id):
        precheck_dispatch_permit_locked(store, task_id, role, config=config)


def release_permit_before_start_locked(
    store: TaskStoreProtocol, task_id: str, permit: DispatchPermit, *, reason: str
) -> None:
    store._assert_lock_held(task_id)
    if permit.task_id != task_id:
        _fail("DISPATCH_PERMIT_STATE_ILLEGAL", "permit task_id does not match")
    reason_text = _string(reason, "reason")
    records = replay_permit_ledger(store, task_id)
    latest = permit_latest_states(records)
    state = latest.get(permit.permit_id)
    if state == "STARTED":
        _fail("DISPATCH_PERMIT_STATE_ILLEGAL", "started permits cannot be released")
    if state == "RELEASED_BEFORE_START":
        _fail("DISPATCH_IDENTITY_RETIRED", "dispatch identity is permanently retired")
    if state != "RESERVED":
        _fail("DISPATCH_PERMIT_STATE_ILLEGAL", "permit is not reserved")
    _append_permit_record(
        store,
        task_id,
        permit_id=permit.permit_id,
        role=permit.role,
        state="RELEASED_BEFORE_START",
        reason=reason_text,
    )


def release_permit_before_start(
    store: TaskStoreProtocol, permit: DispatchPermit, *, reason: str
) -> None:
    with store.lock(permit.task_id):
        release_permit_before_start_locked(
            store, permit.task_id, permit, reason=reason
        )


def release_permit_if_never_spawned(
    store: TaskStoreProtocol, permit: DispatchPermit, *, spawned: bool, reason: str
) -> None:
    if spawned:
        return
    release_permit_before_start(store, permit, reason=reason)


def claim_permit_start_locked(
    store: TaskStoreProtocol, task_id: str, permit: DispatchPermit
) -> None:
    store._assert_lock_held(task_id)
    if permit.task_id != task_id:
        _fail("DISPATCH_PERMIT_STATE_ILLEGAL", "permit task_id does not match")
    records = replay_permit_ledger(store, task_id)
    latest = permit_latest_states(records)
    state = latest.get(permit.permit_id)
    if state != "RESERVED":
        _fail("DISPATCH_PERMIT_STATE_ILLEGAL", "permit is not reserved")
    _append_permit_record(
        store,
        task_id,
        permit_id=permit.permit_id,
        role=permit.role,
        state="STARTED",
    )
