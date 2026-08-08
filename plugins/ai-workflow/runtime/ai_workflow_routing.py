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


def _roles_for(route_name: str, legacy_role_chain: tuple[str, ...]) -> tuple[str, ...]:
    if route_name in {"direct", "blocked"}:
        return ()
    if route_name == "sol_only":
        return ("sol_planner",)
    if route_name == "delegated":
        return legacy_role_chain
    _fail("ROUTE_INPUT_INVALID", "route is not supported")
    raise AssertionError("unreachable")


def _rule_id_for(route_name: str, risky: bool) -> str:
    if route_name == "direct":
        return "SIMPLE_DIRECT_ROUTE"
    if route_name == "sol_only":
        return "HIGH_RISK_READ_ONLY_ROUTE" if risky else "PLANNING_ONLY_ROUTE"
    if route_name == "delegated":
        return "HIGH_RISK_WRITE_DELEGATED_ROUTE" if risky else "DECOMPOSABLE_DELEGATED_ROUTE"
    return "ROUTE_BLOCKED"


def decide_route(
    task: Mapping[str, object],
    request: object,
    mode: str,
    *,
    legacy_router: Callable[[Mapping[str, object]], tuple[str, ...]] | None = None,
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
    current_legacy_roles = (
        legacy_router(task_value) if legacy_router is not None else legacy_roles(task_value)
    )
    if mode == "legacy":
        selected = "delegated"
        rule_id = "LEGACY_TASK_TYPE_ROUTE"
    else:
        risky = bool(task_value["risk_flags"]) or request_value["work_class"] == "HIGH_CONSEQUENCE"
        if risky and request_value["execution_need"] == "WRITE" and not request_value["decomposable"]:
            _fail(
                "ROUTE_UNDECIDABLE",
                "high-consequence write lacks bounded decomposition",
            )
        if risky:
            selected = "sol_only" if request_value["execution_need"] != "WRITE" else "delegated"
        elif request_value["work_class"] == "PLANNING_ONLY":
            selected = "sol_only"
        elif request_value["work_class"] == "SIMPLE":
            selected = "direct"
        elif request_value["work_class"] in {"BOUNDED", "MULTI_STAGE"} and request_value["decomposable"]:
            selected = "delegated"
        else:
            selected = "blocked"
        rule_id = _rule_id_for(selected, risky)
    roles = _roles_for(selected, current_legacy_roles)
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
        _atomic_write_json(path, wire)
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
