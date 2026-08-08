"""Deterministic, closed-set route selection for the workflow control plane.

This module deliberately keeps policy-only compatibility metadata out of the
``ai-route-decision-1`` wire artifact.  ``RuntimeRouteDecision`` exposes the
selected and effective role chains to callers, while ``to_dict()`` exposes
only the frozen Task 1 decision document for persistence.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .ai_workflow_artifacts import (
        ROUTING_MODES,
        RouteDecision,
        artifact_sha256,
        validate_route_decision,
        validate_route_request,
    )
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        ROUTING_MODES,
        RouteDecision,
        artifact_sha256,
        validate_route_decision,
        validate_route_request,
    )


ROLE_POLICIES = frozenset({"legacy", "terra_os"})
# This is an in-memory policy marker only.  It is deliberately not a route
# artifact value: selecting it requires verified local owner-decision context,
# which the public routing facade does not yet receive.
OWNER_AUTHORIZED_LARGE_PROJECT_ROUTE = "owner_authorized_large_project"


def _workflow_error(code: str, message: str) -> BaseException:
    """Create the public workflow exception without a module import cycle."""

    try:
        from .ai_workflow import WorkflowError
    except (ImportError, ModuleNotFoundError):
        from ai_workflow import WorkflowError
    return WorkflowError(code, message)


def _fail(code: str, message: str) -> None:
    raise _workflow_error(code, message)


def _mapping(value: object, *, name: str) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    _fail("INVALID_ARTIFACT", f"{name} must be an object")
    raise AssertionError("unreachable")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RuntimeRouteDecision:
    """Route result plus runtime-only compatibility information.

    The nested ``RouteDecision`` remains the only persisted representation so
    Task 1's strict ``ai-route-decision-1`` schema cannot grow implicitly.
    """

    wire: RouteDecision
    roles: tuple[str, ...]
    shadow_route: str | None
    effective_roles: tuple[str, ...]

    @property
    def schema_version(self) -> str:
        return self.wire.schema_version

    @property
    def task_id(self) -> str:
        return self.wire.task_id

    @property
    def route(self) -> str:
        return self.wire.route

    @property
    def rule_id(self) -> str:
        return self.wire.rule_id

    @property
    def task_sha256(self) -> str:
        return self.wire.task_sha256

    @property
    def request_sha256(self) -> str:
        return self.wire.request_sha256

    @property
    def decided_at_utc(self) -> str:
        return self.wire.decided_at_utc

    @property
    def routing_mode(self) -> str:
        return self.wire.routing_mode

    @property
    def mode(self) -> str:
        return self.wire.mode

    @property
    def evidence_class(self) -> str:
        return self.wire.evidence_class

    @property
    def effective_route(self) -> str:
        """Return the route which an executor must follow in this mode."""

        if self.routing_mode == "shadow":
            return "delegated"
        return self.route

    def to_dict(self) -> dict[str, object]:
        """Return only the strict Task 1 wire schema document."""

        return self.wire.to_dict()

    def __getitem__(self, key: str) -> object:
        if key in {"roles", "shadow_route", "effective_roles", "effective_route"}:
            return getattr(self, key)
        return self.to_dict()[key]


def _fallback_legacy_roles(task: Mapping[str, object]) -> tuple[str, ...]:
    """Mirror the frozen legacy route when this module is used standalone."""

    task_type = task.get("task_type")
    risk_flags = task.get("risk_flags")
    if task_type == "PLAN":
        return ("luna", "sol_planner")
    if task_type == "ACCEPTANCE":
        return ("luna", "sol_reviewer")
    if risk_flags:
        return ("sol_planner", "terra", "luna", "sol_reviewer")
    return ("terra", "luna", "sol_reviewer")


def legacy_roles(task: Mapping[str, object]) -> tuple[str, ...]:
    """Return the unchanged legacy role chain for a validated task."""

    try:
        from .ai_workflow import route
    except (ImportError, ModuleNotFoundError):
        try:
            from ai_workflow import route
        except (ImportError, ModuleNotFoundError):
            return _fallback_legacy_roles(task)
    return route(task)


def _checked_role_policy(value: object) -> str:
    if not isinstance(value, str) or value not in ROLE_POLICIES:
        _fail("ROLE_POLICY_INVALID", "unknown role policy")
    return value


def resolve_role_policy(config: object, override: object = None) -> str:
    """Resolve the configured policy or reject missing and unreviewed values."""

    if override is not None:
        return _checked_role_policy(override)
    if not isinstance(config, Mapping):
        _fail("ROLE_POLICY_INVALID", "workflow configuration must be an object")
    routing = config.get("routing")
    if not isinstance(routing, Mapping):
        _fail("ROLE_POLICY_INVALID", "routing policy configuration is required")
    return _checked_role_policy(routing.get("role_policy"))


def _roles_for(route_name: str, legacy_role_chain: tuple[str, ...]) -> tuple[str, ...]:
    if route_name in {"direct", "blocked"}:
        return ()
    if route_name == "sol_only":
        return ("sol_planner",)
    if route_name == "delegated":
        return legacy_role_chain
    _fail("ROUTE_INPUT_INVALID", "route is not supported")
    raise AssertionError("unreachable")


def terra_os_read_only_role(task: Mapping[str, object]) -> str:
    """Return the task-typed Terra xhigh role for a non-writing route."""

    task_type = task.get("task_type")
    if task_type == "ACCEPTANCE":
        return "terra_xhigh_reviewer"
    if task_type in {"PLAN", "REMEDIATION"}:
        return "terra_xhigh_planner"
    _fail("ROUTE_INPUT_INVALID", "task type has no terra_os read-only role")
    raise AssertionError("unreachable")


def roles_for_policy(
    task: Mapping[str, object],
    request: Mapping[str, object],
    route_name: str,
    policy: str,
    *,
    construction_plan: object | None = None,
    construction_step_id: object = None,
) -> tuple[str, ...]:
    """Return the closed runtime role chain for a selected policy route.

    ``OWNER_AUTHORIZED_LARGE_PROJECT_ROUTE`` models only a chain *after* a
    caller has independently verified local owner authorization.  It never
    appears in the frozen route-decision wire schema, and ``decide_route``
    deliberately cannot select it without that context.
    """

    policy_value = _checked_role_policy(policy)
    if policy_value == "legacy":
        return _roles_for(route_name, legacy_roles(task))
    if route_name in {"direct", "blocked"}:
        return ()
    if route_name == "sol_only":
        return (terra_os_read_only_role(task),)
    if route_name == "delegated":
        if _has_verified_luna_construction_envelope(
            task, request, construction_plan, construction_step_id
        ):
            return ("luna_construction",)
        return ("terra_xhigh",)
    if route_name == OWNER_AUTHORIZED_LARGE_PROJECT_ROUTE:
        if request.get("execution_need") != "WRITE":
            _fail("ROUTE_INPUT_INVALID", "large-project authorization requires a write route")
        return (
            "sol_xhigh_planner",
            "terra_xhigh",
        )
    _fail("ROUTE_INPUT_INVALID", "route is not supported")
    raise AssertionError("unreachable")


def _has_verified_luna_construction_envelope(
    task: Mapping[str, object],
    request: Mapping[str, object],
    plan: object,
    step_id: object,
) -> bool:
    """Accept Luna only from a locally revalidated bounded construction step.

    The frozen route request has no owner or envelope fields.  Treating one as
    an authority source would permit a caller to grow Luna's scope by changing
    route JSON, so the only positive branch here is a fresh plan validation.
    Every other input is a closed fallback to Terra xhigh.
    """

    if (
        request.get("work_class") != "BOUNDED"
        or request.get("execution_need") != "WRITE"
        or request.get("decomposable") is not True
        or task.get("task_type") != "REMEDIATION"
        or task.get("risk_flags")
        or request.get("risk_flags")
        or not isinstance(step_id, str)
        or not step_id.strip()
        or plan is None
    ):
        return False
    try:
        try:
            from .ai_workflow_planning import require_luna_construction_step
        except (ImportError, ModuleNotFoundError):
            from ai_workflow_planning import require_luna_construction_step
        selected = require_luna_construction_step(plan, task, step_id)
    except Exception:
        return False
    return bool(
        selected.owner_role == "luna_construction" and selected.construction_envelope is not None
    )


def _rule_id_for(
    route_name: str, risky: bool, work_class: str, execution_need: str
) -> str:
    if route_name == "direct":
        return "SIMPLE_DIRECT_ROUTE"
    if route_name == "sol_only":
        if risky:
            return "HIGH_RISK_READ_ONLY_ROUTE"
        if work_class == "PLANNING_ONLY":
            return "PLANNING_ONLY_ROUTE"
        if execution_need == "READ_ONLY":
            return "DECOMPOSABLE_READ_ONLY_ROUTE"
        return "DECOMPOSABLE_SOL_ONLY_ROUTE"
    if route_name == "delegated":
        return "HIGH_RISK_WRITE_DELEGATED_ROUTE" if risky else "DECOMPOSABLE_DELEGATED_ROUTE"
    return "ROUTE_BLOCKED"


def decide_route(
    task: Mapping[str, object],
    request: object,
    mode: str,
    *,
    legacy_router: Callable[[Mapping[str, object]], tuple[str, ...]] | None = None,
    role_policy: str = "terra_os",
    construction_plan: object | None = None,
    construction_step_id: object = None,
) -> RuntimeRouteDecision:
    """Select one closed-set route without starting a model.

    ``legacy_router`` is an integration seam used by the public facade to
    bind this module to the existing ``route(task)`` implementation.  It is
    intentionally optional so the focused module remains directly usable.
    """

    task_value = _mapping(task, name="task")
    request_value = _mapping(request, name="route request")
    validate_route_request(request_value, task_value)
    if not isinstance(mode, str) or mode not in ROUTING_MODES:
        _fail("ROUTE_INPUT_INVALID", "unknown routing mode")
    policy = _checked_role_policy(role_policy)
    current_legacy_roles = (
        legacy_router(task_value) if legacy_router is not None else legacy_roles(task_value)
    )
    if mode == "legacy":
        selected = "delegated"
        rule_id = "LEGACY_TASK_TYPE_ROUTE"
    else:
        risky = bool(task_value["risk_flags"]) or request_value["work_class"] == "HIGH_CONSEQUENCE"
        bounded_or_multi_stage = request_value["work_class"] in {"BOUNDED", "MULTI_STAGE"}
        if bounded_or_multi_stage and not request_value["decomposable"]:
            selected = "blocked"
        elif risky and request_value["execution_need"] == "WRITE" and not request_value["decomposable"]:
            _fail(
                "ROUTE_UNDECIDABLE",
                "high-consequence write lacks bounded decomposition",
            )
        elif risky:
            selected = "sol_only" if request_value["execution_need"] != "WRITE" else "delegated"
        elif request_value["work_class"] == "PLANNING_ONLY":
            selected = "sol_only"
        elif request_value["work_class"] == "SIMPLE":
            selected = "direct"
        elif bounded_or_multi_stage:
            if request_value["execution_need"] == "WRITE":
                selected = "delegated"
            else:
                selected = "sol_only"
        else:
            selected = "blocked"
        rule_id = _rule_id_for(
            selected,
            risky,
            str(request_value["work_class"]),
            str(request_value["execution_need"]),
        )
    roles = (
        _roles_for(selected, current_legacy_roles)
        if mode == "legacy"
        else roles_for_policy(
            task_value,
            request_value,
            selected,
            policy,
            construction_plan=construction_plan,
            construction_step_id=construction_step_id,
        )
    )
    wire = RouteDecision(
        task_id=str(task_value["task_id"]),
        route=selected,
        rule_id=rule_id,
        task_sha256=artifact_sha256(task_value),
        request_sha256=artifact_sha256(request_value),
        decided_at_utc=_utc_timestamp(),
        routing_mode=mode,
        evidence_class="unavailable",
    )
    validate_route_decision(wire.to_dict())
    return RuntimeRouteDecision(
        wire=wire,
        roles=roles,
        shadow_route=selected if mode == "shadow" else None,
        effective_roles=current_legacy_roles if mode == "shadow" else roles,
    )


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    """Write through a same-directory temporary file and atomically replace."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(_canonical_json(value))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        _fail("ATOMIC_WRITE_FAILED", f"cannot write {target.name}")
        raise AssertionError("unreachable") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _write_json_once(path: Path, value: Mapping[str, object]) -> None:
    """Create one immutable authority artifact without an exists/write race."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        _fail("ROUTE_ALREADY_FROZEN", "route decision is immutable after first persistence")
    except OSError as exc:
        raise _workflow_error("ATOMIC_WRITE_FAILED", f"cannot create {target.name}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(_canonical_json(value))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _stored_task_sha256(task_dir: Path) -> str:
    try:
        value = json.loads((Path(task_dir) / "task.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail("TASK_READ_ERROR", "cannot read stored task for route decision")
        raise AssertionError("unreachable") from exc
    if not isinstance(value, Mapping):
        _fail("INVALID_TASK", "stored task must be an object")
    return artifact_sha256(value)


def record_route_decision(
    store: Any,
    task_id: str,
    decision: RuntimeRouteDecision,
) -> Path:
    """Atomically persist a strict decision and append a hash-bound event."""

    if not isinstance(task_id, str) or not task_id:
        _fail("INVALID_TASK_ID", "task_id must be a non-empty string")
    if not isinstance(decision, RuntimeRouteDecision):
        _fail("ROUTE_INPUT_INVALID", "route decision must be a runtime route decision")
    if decision.task_id != task_id:
        _fail("ROUTE_CONFLICT", "route decision task_id does not match persistence target")
    wire = decision.to_dict()
    validate_route_decision(wire)
    with store.lock(task_id):
        task_dir = store._require_task(task_id)
        if _stored_task_sha256(task_dir) != decision.task_sha256:
            _fail("ROUTE_TASK_MISMATCH", "route decision task hash does not match stored task")
        path = Path(task_dir) / "route-decision.json"
        _write_json_once(path, wire)
        store.append_event(
            task_id,
            {
                "event_type": "ROUTE_DECIDED",
                "timestamp_utc": decision.decided_at_utc,
                "task_sha256": decision.task_sha256,
                "request_sha256": decision.request_sha256,
                "route": decision.route,
                "routing_mode": decision.routing_mode,
                "rule_id": decision.rule_id,
            },
        )
    return path
