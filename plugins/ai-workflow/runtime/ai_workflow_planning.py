"""Validated bounded-plan scheduling and idempotent dispatch identities.

The planning layer consumes only the strict ``ai-plan-1`` shape and returns
immutable values.  It deliberately does not start a role or alter workflow
state: callers must still pass the existing state-machine and worktree gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
class ConstructionCheck:
    """One mechanically verifiable command or artifact requirement."""

    kind: str
    artifact: str
    command: str | None = None
    expected_exit: int | None = None
    assertion: str | None = None
    sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        if self.kind == "HASH":
            return {"kind": self.kind, "artifact": self.artifact, "sha256": self.sha256}
        return {
            "kind": self.kind,
            "command": self.command,
            "expected_exit": self.expected_exit,
            "assertion": self.assertion,
            "artifact": self.artifact,
        }


@dataclass(frozen=True)
class ConstructionEnvelope:
    """A complete, path-bounded construction contract for a Luna owner."""

    allowed_paths: tuple[str, ...]
    done_when: ConstructionCheck
    evidence: tuple[tuple[str, ConstructionCheck], ...]
    negative_checks: tuple[ConstructionCheck, ...]
    risk_classification: tuple[tuple[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed_paths": list(self.allowed_paths),
            "done_when": self.done_when.to_dict(),
            "evidence": {level: record.to_dict() for level, record in self.evidence},
            "negative_checks": [record.to_dict() for record in self.negative_checks],
            "risk_classification": dict(self.risk_classification),
        }


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
    construction_envelope: ConstructionEnvelope | None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
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
        if self.construction_envelope is not None:
            result["construction_envelope"] = self.construction_envelope.to_dict()
        return result

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


def construction_evidence_argv(
    check: ConstructionCheck, *, error_code: str = "PLAN_INVALID"
) -> tuple[str, ...]:
    """Return the one typed evidence argv bound to its exact artifact.

    Evidence collection runs in the controller process, so a plan never gets
    to choose an executable through ``PATH`` or add unrelated file operands.
    The frozen command string is merely the wire representation of this
    closed fixed-string-search operation.
    """

    if not isinstance(check, ConstructionCheck):
        _fail(error_code, "construction evidence check is invalid")
    try:
        artifact = normalize_scope(check.artifact).as_posix()
        argv = shlex.split(check.command) if isinstance(check.command, str) else []
    except ValueError:
        argv = []
    if (
        check.kind not in {"COMMAND", "TEST"}
        or isinstance(check.expected_exit, bool)
        or check.expected_exit not in {0, 1}
        or not isinstance(check.assertion, str)
        or not check.assertion.strip()
        or len(argv) != 4
        or argv[0] != "/usr/bin/grep"
        or argv[1] != "-F"
        or not argv[2]
        or argv[3] != artifact
    ):
        _fail(error_code, "construction evidence operation is not artifact-bound")
    return tuple(argv)


def _construction_check(value: object, *, expected_kind: str, negative: bool = False) -> ConstructionCheck:
    if not isinstance(value, Mapping):
        _fail("PLAN_INVALID", "construction check must be an object")
    kind = value.get("kind")
    if kind != expected_kind:
        _fail("PLAN_INVALID", f"construction check kind must be {expected_kind}")
    artifact = normalize_scope(value.get("artifact"))
    if kind == "HASH":
        digest = value.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            _fail("PLAN_INVALID", "construction hash check requires a SHA256 digest")
        return ConstructionCheck(kind=kind, artifact=artifact.as_posix(), sha256=digest)
    command = value.get("command")
    assertion = value.get("assertion")
    expected_exit = value.get("expected_exit")
    if (
        not isinstance(command, str)
        or not command.strip()
        or not isinstance(assertion, str)
        or not assertion.strip()
        or isinstance(expected_exit, bool)
        or not isinstance(expected_exit, int)
        or (negative and expected_exit == 0)
        or (not negative and expected_exit != 0)
    ):
        _fail("PLAN_INVALID", "construction command check is not mechanically bound")
    check = ConstructionCheck(
        kind=kind,
        artifact=artifact.as_posix(),
        command=command,
        expected_exit=expected_exit,
        assertion=assertion,
    )
    construction_evidence_argv(check)
    return check


def _construction_envelope(value: object) -> ConstructionEnvelope:
    if not isinstance(value, Mapping):
        _fail("PLAN_INVALID", "construction_envelope must be an object")
    allowed_paths = _scope_strings(value["allowed_paths"], field="construction_envelope.allowed_paths")
    done_when = _construction_check(value["done_when"], expected_kind="TEST")
    negative_raw = value["negative_checks"]
    if not isinstance(negative_raw, list) or len(negative_raw) != 1:
        _fail("PLAN_INVALID", "construction_envelope requires exactly one negative check")
    negative_checks = tuple(
        _construction_check(item, expected_kind="COMMAND", negative=True)
        for item in negative_raw
    )
    evidence = value["evidence"]
    if not isinstance(evidence, Mapping):
        _fail("PLAN_INVALID", "construction_envelope.evidence must be an object")
    levels = [
        ("L0", _construction_check(evidence["L0"], expected_kind="HASH")),
        ("L1", _construction_check(evidence["L1"], expected_kind="COMMAND")),
        ("L2", _construction_check(evidence["L2"], expected_kind="TEST")),
    ]
    if done_when != levels[2][1]:
        _fail("PLAN_INVALID", "done_when must be the controller-executed L2 test")
    classification = value["risk_classification"]
    if not isinstance(classification, Mapping):
        _fail("PLAN_INVALID", "construction_envelope.risk_classification must be an object")
    classification_value = dict(classification)
    if (
        classification_value.get("kind") != "LOCAL_DETERMINISTIC_IMPLEMENTATION"
        or any(
            classification_value.get(field) is not False
            for field in ("security", "authorization", "protocol", "control_plane")
        )
    ):
        _fail("PLAN_INVALID", "luna construction risk classification is not eligible")
    return ConstructionEnvelope(
        allowed_paths=allowed_paths,
        done_when=done_when,
        evidence=tuple(levels),
        negative_checks=negative_checks,
        risk_classification=tuple(sorted(classification_value.items())),
    )


def _required_nonempty_strings(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        _fail("PLAN_INVALID", f"{field} must be a non-empty array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        _fail("PLAN_INVALID", f"{field} must contain non-empty strings")
    if len(value) != len(set(value)):
        _fail("PLAN_INVALID", f"{field} must not contain duplicates")
    return tuple(value)


def _luna_scope_is_forbidden(scope: str) -> bool:
    path = normalize_scope(scope)
    parts = tuple(part.casefold() for part in path.parts)
    if not parts:
        return True
    if parts[0] in {".git", ".superpowers", "logs"}:
        return True
    if len(parts) >= 2 and parts[:2] == ("data", "state"):
        return True
    if parts[0] in {"scripts", "config", "data"} and len(parts) == 1:
        return True
    if parts[0] == "config" and path.name.casefold().startswith("ai_workflow"):
        return True
    return parts[0] == "scripts" and path.name.casefold().startswith("ai_workflow")


def _scope_has_unsafe_filesystem_component(repository_root: object, scope: str) -> bool:
    """Inspect every existing component through no-follow directory FDs."""

    if not isinstance(repository_root, str) or not Path(repository_root).is_absolute():
        return True
    root = Path(repository_root)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return True
    current_fd = root_fd
    try:
        for index, component in enumerate(normalize_scope(scope).parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index < len(normalize_scope(scope).parts) - 1:
                flags |= os.O_DIRECTORY
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                return False
            except OSError:
                return True
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        return False
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _luna_local_task_is_explicit(task: Mapping[str, object], scopes: set[str]) -> bool:
    objective = task.get("objective")
    if not isinstance(objective, str):
        return False
    # Luna is an explicit exception: only concrete local implementation verbs
    # and local source/test/doc/fixture roots qualify.  Uncertain semantics go
    # to Terra without attempting an ever-growing deny-word classifier.
    action = objective.strip().casefold().split(maxsplit=1)[0]
    if action not in {"add", "create", "fix", "implement", "rename", "update", "write"}:
        return False
    allowed_roots = {"src", "tests", "docs", "fixtures", "examples", "scripts"}
    return bool(scopes) and all(normalize_scope(scope).parts[0].casefold() in allowed_roots for scope in scopes)


def _luna_semantics_are_prohibited(task: Mapping[str, object], subtask: Mapping[str, object]) -> bool:
    """Classify protected surfaces from relationships, not only declared flags.

    A Luna envelope is an opt-in exception for deterministic local construction.
    The classification is intentionally conservative: protected *relations* such
    as a subject receiving authority are rejected even when the producer omitted
    the corresponding risk flag.  This complements the explicit structural
    risk classification, bounded path checks, and parent-task risk flags.
    """

    values = (
        task.get("objective"),
        subtask.get("expected_result"),
        subtask.get("first_artifact"),
    )
    text = " ".join(value for value in values if isinstance(value, str)).casefold()
    direct_protected_surfaces = (
        "security",
        "authorization",
        "authentication",
        "principal",
        "access boundar",
        "credential",
        "secret",
        "token",
        "protocol",
        "control plane",
        "routing",
        "workflow",
        "runtime",
        "sandbox",
        "identity",
        "policy",
    )
    if any(marker in text for marker in direct_protected_surfaces):
        return True
    authority_subjects = (
        "operator",
        "user",
        "account",
        "tenant",
        "client",
        "member",
        "group",
        "service",
    )
    authority_actions = (
        "allow",
        "deny",
        "restrict",
        "permit",
        "grant",
        "revoke",
        "access",
        "capabilit",
        "privilege",
        "may change",
        "can change",
        "may operate",
        "can operate",
    )
    if any(subject in text for subject in authority_subjects) and any(
        action in text for action in authority_actions
    ):
        return True
    control_targets = ("production", "deployment", "runtime", "configuration", "service behavior")
    control_actions = ("change", "modify", "enable", "disable", "start", "stop", "route")
    return any(target in text for target in control_targets) and any(
        action in text for action in control_actions
    )


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
        construction_envelope = (
            _construction_envelope(value["construction_envelope"])
            if "construction_envelope" in value
            else None
        )
        for raw_scope in write_scope:
            scope = normalize_scope(raw_scope)
            if not any(_scope_within(scope, allowed) for allowed in allowed_scopes):
                _fail("PLAN_INVALID", "write_scope is outside parent allowed_write_paths")
            if any(scopes_overlap(scope, normalize_scope(blocked)) for blocked in do_not_touch):
                _fail("PLAN_INVALID", "write_scope overlaps do_not_touch")
        if value["owner_role"] == "luna_construction":
            if construction_envelope is None:
                _fail("PLAN_INVALID", "luna_construction requires construction_envelope")
            if task_value["task_type"] != "REMEDIATION" or task_value["risk_flags"]:
                _fail("PLAN_INVALID", "luna_construction is limited to low-risk remediation work")
            if _luna_semantics_are_prohibited(task_value, value):
                _fail("PLAN_INVALID", "luna_construction cannot own a protected semantic surface")
            if not write_scope or not value["verification_commands"]:
                _fail("PLAN_INVALID", "luna_construction requires write_scope and verification_commands")
            if tuple(write_scope) != construction_envelope.allowed_paths:
                _fail("PLAN_INVALID", "luna construction allowed_paths must exactly match write_scope")
            bound_scopes = set(write_scope) | set(read_scope)
            if not _luna_local_task_is_explicit(task_value, bound_scopes):
                _fail("PLAN_INVALID", "luna construction requires an explicitly local task kind and scope")
            if any(_luna_scope_is_forbidden(scope) for scope in bound_scopes):
                _fail("PLAN_INVALID", "luna construction cannot access metadata or control-plane paths")
            if any(
                _scope_has_unsafe_filesystem_component(task_value["repository_root"], scope)
                for scope in bound_scopes
            ):
                _fail("PLAN_INVALID", "luna construction scope contains an unsafe filesystem component")
            checks = (
                construction_envelope.done_when,
                *(record for _, record in construction_envelope.evidence),
                *construction_envelope.negative_checks,
            )
            if any(check.artifact not in bound_scopes for check in checks):
                _fail("PLAN_INVALID", "construction evidence must bind an authorized scope")
        elif construction_envelope is not None:
            _fail("PLAN_INVALID", "construction_envelope is reserved for luna_construction")
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
                construction_envelope=construction_envelope,
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


def require_luna_construction_step(
    plan: object, task: Mapping[str, object], step_id: object
) -> FrozenSubtask:
    """Return exactly one freshly validated Luna construction step or fail closed."""

    if not isinstance(step_id, str) or not step_id.strip():
        _fail("LUNA_ENVELOPE_INVALID", "luna construction step_id must be a non-empty string")
    document = plan.to_dict() if callable(getattr(plan, "to_dict", None)) else plan
    frozen = validate_plan(document, task)
    selected = next((item for item in frozen.tasks if item.id == step_id), None)
    if (
        selected is None
        or selected.owner_role != "luna_construction"
        or selected.construction_envelope is None
    ):
        _fail("LUNA_ENVELOPE_INVALID", "luna construction requires its verified envelope step")
    return selected


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
    *,
    request_sha256: str | None = None,
    route_fields: Mapping[str, object] | None = None,
    role: str | None = None,
) -> str:
    """Hash exactly the five canonical values that define one launch attempt."""

    value = {
        "plan_sha256": _identity_string(plan_sha256, field="plan_sha256"),
        "task_sha256": _identity_string(task_sha256, field="task_sha256"),
        "subtask_id": _identity_string(subtask_id, field="subtask_id"),
        "attempt": attempt,
        "candidate_commit": _identity_string(candidate_commit, field="candidate_commit"),
    }
    if request_sha256 is not None or route_fields is not None or role is not None:
        if request_sha256 is None or route_fields is None or role is None:
            _fail("DISPATCH_IDENTITY_DRIFT", "dispatch authority binding must be complete")
        value.update(
            {
                "request_sha256": _identity_string(request_sha256, field="request_sha256"),
                "route_fields": dict(route_fields),
                "role": _identity_string(role, field="role"),
            }
        )
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
    *,
    store_locked: bool = False,
    request_sha256: str | None = None,
    route_fields: Mapping[str, object] | None = None,
    role: str | None = None,
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
        request_sha256=request_sha256,
        route_fields=route_fields,
        role=role,
    )
    method_name = "_record_dispatch_locked" if store_locked else "record_dispatch"
    method = getattr(store, method_name, None)
    if not callable(method):
        _fail("PLAN_INVALID", "dispatch store does not support the required append-only launch")
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
            **(
                {
                    "request_sha256": request_sha256,
                    "route_fields": dict(route_fields),
                    "role": role,
                }
                if request_sha256 is not None and route_fields is not None and role is not None
                else {}
            ),
        },
    )
    return identity
