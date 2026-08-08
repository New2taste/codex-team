"""Validated bounded-plan scheduling and idempotent dispatch identities.

The planning layer consumes only the strict ``ai-plan-1`` shape and returns
immutable values.  It deliberately does not start a role or alter workflow
state: callers must still pass the existing state-machine and worktree gates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

try:
    from .ai_workflow_artifacts import artifact_sha256, validate_plan_shape
except ImportError:  # direct script execution
    from ai_workflow_artifacts import artifact_sha256, validate_plan_shape


def _workflow_error(code: str, message: str) -> BaseException:
    """Construct the public exception without creating an import cycle."""

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
    _fail("PLAN_INVALID", f"{name} must be an object")
    raise AssertionError("unreachable")


def normalize_scope(path: str) -> PurePosixPath:
    """Return one literal, normalized repository-relative POSIX path."""

    if not isinstance(path, str) or not path or path != path.strip():
        _fail("PLAN_INVALID", "scope must be a non-empty literal repository-relative path")
    if path.startswith("/") or "\\" in path or "\x00" in path:
        _fail("PLAN_INVALID", "scope must be a literal repository-relative path")
    if any(char in path for char in "*?[]"):
        _fail("PLAN_INVALID", "scope must not contain a glob")
    pieces = path.split("/")
    if any(piece in {"", ".", ".."} for piece in pieces):
        _fail("PLAN_INVALID", "scope must be normalized and cannot traverse")
    value = PurePosixPath(path)
    if value.is_absolute() or ".." in value.parts or "." in value.parts or value.as_posix() != path:
        _fail("PLAN_INVALID", "scope must be normalized and cannot traverse")
    return value


def scopes_overlap(left: PurePosixPath, right: PurePosixPath) -> bool:
    """Return whether two literal repository scopes have a prefix collision."""

    return left == right or left in right.parents or right in left.parents


def _scope_strings(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail("PLAN_INVALID", f"{field} must be an array")
    paths = tuple(normalize_scope(item) for item in value)
    normalized = tuple(path.as_posix() for path in paths)
    if len(normalized) != len(set(normalized)):
        _fail("PLAN_INVALID", f"{field} has duplicate normalized paths")
    return normalized


def _scope_within(scope: PurePosixPath, allowed: PurePosixPath) -> bool:
    return scope == allowed or allowed in scope.parents


@dataclass(frozen=True)
class FrozenSubtask:
    """A validated subtask whose scopes and dependencies cannot be mutated."""

    id: str
    owner_role: str
    read_scope: tuple[str, ...]
    write_scope: tuple[str, ...]
    do_not_touch: tuple[str, ...]
    depends_on: tuple[str, ...]
    expected_result: str
    verification_commands: tuple[str, ...]
    first_artifact: str
    evidence_level: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "owner_role": self.owner_role,
            "read_scope": list(self.read_scope),
            "write_scope": list(self.write_scope),
            "do_not_touch": list(self.do_not_touch),
            "depends_on": list(self.depends_on),
            "expected_result": self.expected_result,
            "verification_commands": list(self.verification_commands),
            "first_artifact": self.first_artifact,
            "evidence_level": self.evidence_level,
        }

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]


@dataclass(frozen=True)
class FrozenPlan:
    """A shape- and policy-validated plan bound to its parent task envelope."""

    schema_version: str
    plan_id: str
    task_id: str
    goal: str
    done_when: tuple[str, ...]
    tasks: tuple[FrozenSubtask, ...]
    stages: tuple[tuple[str, ...], ...]
    plan_sha256: str
    task_sha256: str
    base_commit: str | None
    candidate_commit: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "goal": self.goal,
            "done_when": list(self.done_when),
            "tasks": [task.to_dict() for task in self.tasks],
            "stages": [list(stage) for stage in self.stages],
        }

    def __getitem__(self, key: str) -> object:
        if key in {"plan_sha256", "task_sha256", "base_commit", "candidate_commit"}:
            return getattr(self, key)
        return self.to_dict()[key]


def _validate_parent_task(task: Mapping[str, object]) -> None:
    try:
        from .ai_workflow import validate_task
    except (ImportError, ModuleNotFoundError):
        from ai_workflow import validate_task
    validate_task(task)


def _cycle_check(tasks_by_id: Mapping[str, FrozenSubtask]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            _fail("PLAN_CYCLE", f"dependency cycle includes {identifier}")
        if identifier in visited:
            return
        visiting.add(identifier)
        for dependency in tasks_by_id[identifier].depends_on:
            visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for identifier in sorted(tasks_by_id):
        visit(identifier)


def _validate_stages(
    stages: tuple[tuple[str, ...], ...], tasks_by_id: Mapping[str, FrozenSubtask]
) -> None:
    stage_by_id: dict[str, int] = {}
    for stage_index, stage in enumerate(stages):
        for identifier in stage:
            if identifier not in tasks_by_id:
                _fail("PLAN_INVALID", f"stage references unknown task {identifier}")
            if identifier in stage_by_id:
                _fail("PLAN_INVALID", f"task {identifier} appears in more than one stage")
            stage_by_id[identifier] = stage_index
    missing = sorted(set(tasks_by_id) - set(stage_by_id))
    if missing:
        _fail("PLAN_INVALID", f"task {missing[0]} does not belong to a stage")
    for identifier, task in tasks_by_id.items():
        for dependency in task.depends_on:
            if stage_by_id[dependency] >= stage_by_id[identifier]:
                _fail("PLAN_INVALID", "dependencies must be in an earlier stage")


def _validate_write_ownership(tasks: tuple[FrozenSubtask, ...]) -> None:
    claims: list[tuple[str, PurePosixPath]] = []
    exact_owners: dict[str, str] = {}
    for task in tasks:
        for raw_scope in task.write_scope:
            scope = normalize_scope(raw_scope)
            prior_owner = exact_owners.get(raw_scope)
            if prior_owner is not None and prior_owner != task.id:
                _fail("OWNER_CONFLICT", f"write scope {raw_scope} has more than one owner")
            exact_owners[raw_scope] = task.id
            for existing_owner, existing_scope in claims:
                if existing_owner != task.id and scopes_overlap(scope, existing_scope):
                    _fail("SCOPE_OVERLAP", "write scopes of different tasks overlap")
            claims.append((task.id, scope))


def validate_plan(plan: object, task: Mapping[str, object]) -> FrozenPlan:
    """Validate, normalize, and freeze a bounded plan against one parent task."""

    task_value = _mapping(task, name="parent task")
    _validate_parent_task(task_value)
    try:
        validate_plan_shape(plan)
    except Exception as exc:
        if getattr(exc, "code", None) is not None:
            _fail("PLAN_INVALID", getattr(exc, "message", "invalid plan artifact"))
        raise
    plan_value = _mapping(plan, name="plan")
    if plan_value["task_id"] != task_value["task_id"]:
        _fail("PLAN_INVALID", "plan task_id does not match parent task")

    allowed_scopes = tuple(
        normalize_scope(item) for item in task_value["allowed_write_paths"]
    )
    frozen_tasks: list[FrozenSubtask] = []
    task_ids: set[str] = set()
    for raw_task in plan_value["tasks"]:
        if not isinstance(raw_task, Mapping):
            _fail("PLAN_INVALID", "plan task must be an object")
        value = dict(raw_task)
        identifier = value["id"]
        if identifier in task_ids:
            _fail("PLAN_INVALID", f"duplicate plan task id {identifier}")
        task_ids.add(identifier)
        read_scope = _scope_strings(value["read_scope"], field="read_scope")
        write_scope = _scope_strings(value["write_scope"], field="write_scope")
        do_not_touch = _scope_strings(value["do_not_touch"], field="do_not_touch")
        for raw_scope in write_scope:
            scope = normalize_scope(raw_scope)
            if not any(_scope_within(scope, allowed) for allowed in allowed_scopes):
                _fail("PLAN_INVALID", "write_scope is outside parent allowed_write_paths")
            if any(scopes_overlap(scope, normalize_scope(blocked)) for blocked in do_not_touch):
                _fail("PLAN_INVALID", "write_scope overlaps do_not_touch")
        frozen_tasks.append(
            FrozenSubtask(
                id=identifier,
                owner_role=value["owner_role"],
                read_scope=read_scope,
                write_scope=write_scope,
                do_not_touch=do_not_touch,
                depends_on=tuple(value["depends_on"]),
                expected_result=value["expected_result"],
                verification_commands=tuple(value["verification_commands"]),
                first_artifact=value["first_artifact"],
                evidence_level=value["evidence_level"],
            )
        )

    tasks_tuple = tuple(frozen_tasks)
    tasks_by_id = {item.id: item for item in tasks_tuple}
    for item in tasks_tuple:
        for dependency in item.depends_on:
            if dependency not in tasks_by_id:
                _fail("PLAN_INVALID", f"task {item.id} depends on an unknown task")
    _cycle_check(tasks_by_id)
    stages = tuple(tuple(stage) for stage in plan_value["stages"])
    _validate_stages(stages, tasks_by_id)
    _validate_write_ownership(tasks_tuple)
    return FrozenPlan(
        schema_version=plan_value["schema_version"],
        plan_id=plan_value["plan_id"],
        task_id=plan_value["task_id"],
        goal=plan_value["goal"],
        done_when=tuple(plan_value["done_when"]),
        tasks=tasks_tuple,
        stages=stages,
        plan_sha256=artifact_sha256(plan_value),
        task_sha256=artifact_sha256(task_value),
        base_commit=task_value["base_commit"],
        candidate_commit=task_value["candidate_commit"],
    )


def scope_owner_map(plan: FrozenPlan) -> dict[str, str]:
    """Return the stable subtask owner for every validated write scope."""

    if not isinstance(plan, FrozenPlan):
        _fail("PLAN_INVALID", "scope ownership requires a frozen plan")
    owners: dict[str, str] = {}
    for task in plan.tasks:
        for scope in task.write_scope:
            existing = owners.get(scope)
            if existing is not None and existing != task.id:
                _fail("OWNER_CONFLICT", f"write scope {scope} has more than one owner")
            owners[scope] = task.id
    return owners


def _known_task_ids(value: object, *, name: str, permitted: set[str]) -> set[str]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        _fail("PLAN_INVALID", f"{name} must be a collection of task ids")
    raw_identifiers = tuple(value)
    identifiers = set(raw_identifiers)
    if len(identifiers) != len(raw_identifiers):
        _fail("PLAN_INVALID", f"{name} must not repeat task ids")
    if any(not isinstance(identifier, str) for identifier in identifiers):
        _fail("PLAN_INVALID", f"{name} must contain task ids")
    unknown = sorted(identifiers - permitted)
    if unknown:
        _fail("PLAN_INVALID", f"{name} contains unknown task {unknown[0]}")
    return identifiers


def ready_batch(
    plan: FrozenPlan,
    completed: Iterable[str],
    dispatched: Iterable[str],
    capacity: int,
) -> tuple[str, ...]:
    """Choose a stable, capacity-bounded batch from the first incomplete stage."""

    if not isinstance(plan, FrozenPlan):
        _fail("PLAN_INVALID", "ready batches require a frozen plan")
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
        _fail("CAPACITY_UNAVAILABLE", "capacity must be a non-negative integer")
    permitted = {task.id for task in plan.tasks}
    completed_ids = _known_task_ids(completed, name="completed", permitted=permitted)
    dispatched_ids = _known_task_ids(dispatched, name="dispatched", permitted=permitted)
    if capacity == 0:
        return ()
    current_stage = next(
        (stage for stage in plan.stages if not set(stage).issubset(completed_ids)), None
    )
    if current_stage is None:
        return ()
    tasks_by_id = {task.id: task for task in plan.tasks}
    ready = sorted(
        identifier
        for identifier in current_stage
        if set(tasks_by_id[identifier].depends_on).issubset(completed_ids)
        and identifier not in completed_ids
        and identifier not in dispatched_ids
    )
    return tuple(ready[:capacity])


def _identity_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        _fail("DISPATCH_IDENTITY_DRIFT", f"{field} must be a non-empty string")
    return value


def dispatch_id(
    plan_sha256: str,
    task_sha256: str,
    subtask_id: str,
    attempt: int,
    candidate_commit: str,
) -> str:
    """Hash exactly the five canonical values that define one launch attempt."""

    value = {
        "plan_sha256": _identity_string(plan_sha256, field="plan_sha256"),
        "task_sha256": _identity_string(task_sha256, field="task_sha256"),
        "subtask_id": _identity_string(subtask_id, field="subtask_id"),
        "attempt": attempt,
        "candidate_commit": _identity_string(candidate_commit, field="candidate_commit"),
    }
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        _fail("DISPATCH_IDENTITY_DRIFT", "attempt must be a positive integer")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scope_sha256(task: FrozenSubtask) -> str:
    return artifact_sha256(
        {
            "read_scope": list(task.read_scope),
            "write_scope": list(task.write_scope),
            "do_not_touch": list(task.do_not_touch),
        }
    )


def _stored_task(store: object, task_id: str) -> dict[str, object]:
    require_task = getattr(store, "_require_task", None)
    if not callable(require_task):
        _fail("PLAN_INVALID", "dispatch requires the workflow append-only store")
    try:
        value = json.loads((require_task(task_id) / "task.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _workflow_error("DISPATCH_IDENTITY_DRIFT", "cannot read stored dispatch task") from exc
    if not isinstance(value, Mapping):
        _fail("DISPATCH_IDENTITY_DRIFT", "stored dispatch task must be an object")
    return dict(value)


def record_dispatch(
    store: object,
    task_id: str,
    plan: FrozenPlan,
    subtask_id: str,
    attempt: int,
    candidate_commit: str,
) -> str:
    """Append one immutable dispatch record and return its canonical identity."""

    if not isinstance(plan, FrozenPlan):
        _fail("PLAN_INVALID", "dispatch requires a frozen plan")
    if task_id != plan.task_id:
        _fail("DISPATCH_IDENTITY_DRIFT", "dispatch task_id does not match frozen plan")
    stored_task = _stored_task(store, task_id)
    if artifact_sha256(stored_task) != plan.task_sha256:
        _fail("DISPATCH_IDENTITY_DRIFT", "stored task does not match frozen plan")
    revalidated_plan = validate_plan(plan.to_dict(), stored_task)
    if (
        revalidated_plan.plan_sha256 != plan.plan_sha256
        or revalidated_plan.candidate_commit != plan.candidate_commit
        or revalidated_plan.base_commit != plan.base_commit
    ):
        _fail("DISPATCH_IDENTITY_DRIFT", "frozen plan identity does not match its document")
    if not isinstance(plan.candidate_commit, str) or not plan.candidate_commit:
        _fail("DISPATCH_IDENTITY_DRIFT", "frozen plan requires a candidate_commit")
    if candidate_commit != plan.candidate_commit:
        _fail("DISPATCH_IDENTITY_DRIFT", "candidate_commit does not match frozen parent task")
    task_by_id = {task.id: task for task in plan.tasks}
    selected = task_by_id.get(subtask_id)
    if selected is None:
        _fail("PLAN_INVALID", "dispatch references an unknown subtask")
    identity = dispatch_id(
        plan.plan_sha256,
        plan.task_sha256,
        subtask_id,
        attempt,
        candidate_commit,
    )
    method = getattr(store, "record_dispatch", None)
    if not callable(method):
        _fail("PLAN_INVALID", "dispatch store does not support append-only records")
    method(
        task_id,
        identity,
        {
            "event_type": "DISPATCH_RECORDED",
            "owner_task_id": selected.id,
            "owner_role": selected.owner_role,
            "plan_sha256": plan.plan_sha256,
            "task_sha256": plan.task_sha256,
            "scope_sha256": _scope_sha256(selected),
            "subtask_id": selected.id,
            "attempt": attempt,
            "candidate_commit": candidate_commit,
        },
    )
    return identity
