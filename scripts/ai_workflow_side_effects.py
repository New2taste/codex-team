"""Host-observed filesystem and command side effects."""

from __future__ import annotations

import contextlib
import hashlib
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    from .ai_workflow_artifacts import TaskStoreProtocol, WorkflowError, canonical_json
    from .ai_workflow_candidate_state import (
        GIT_DIR_PREFIX,
        RUNTIME_SESSIONS_PREFIX,
        STATE_ROOT_PREFIX,
        candidate_exclusions,
        scan_candidate_manifest,
    )
    from .ai_workflow_ownership import (
        load_ownership_registry,
        record_side_effect,
        record_side_effect_locked,
    )
except ImportError:  # direct script execution
    from ai_workflow_artifacts import TaskStoreProtocol, WorkflowError, canonical_json
    from ai_workflow_candidate_state import (
        GIT_DIR_PREFIX,
        RUNTIME_SESSIONS_PREFIX,
        STATE_ROOT_PREFIX,
        candidate_exclusions,
        scan_candidate_manifest,
    )
    from ai_workflow_ownership import (
        load_ownership_registry,
        record_side_effect,
        record_side_effect_locked,
    )


COMMAND_PRODUCERS = frozenset({"ROLLOUT_TOOL_EVENTS", "CONSTRUCTION_FROZEN_STEP"})
EFFECTFUL_ROLE_SANDBOXES = frozenset({"workspace-write", "assignment-scoped-write"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_PLANE_PREFIXES = (
    GIT_DIR_PREFIX,
    STATE_ROOT_PREFIX,
    RUNTIME_SESSIONS_PREFIX,
)
_COMMAND_ITEM_TYPES = frozenset({"command_execution"})
_REAL_SUBPROCESS_RUN = subprocess.run


@contextlib.contextmanager
def _unmocked_subprocess_run():
    """Keep git observation on the real subprocess when Codex launch is stubbed."""

    current = subprocess.run
    subprocess.run = _REAL_SUBPROCESS_RUN
    try:
        yield
    finally:
        subprocess.run = current


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        _fail("INVALID_TYPE", f"{field} must be a string")
    if not value.strip():
        _fail("EMPTY_FIELD", f"{field} must not be empty")
    return value


@dataclass(frozen=True)
class FSEntry:
    path: str
    mode: str
    kind: str
    content_sha256: str


@dataclass(frozen=True)
class FSSnapshot:
    root: Path
    entries: tuple[FSEntry, ...]
    head: str


@dataclass(frozen=True)
class FSChange:
    path: str
    change_kind: str
    entry_after: FSEntry | None


@dataclass(frozen=True)
class CommandExecution:
    command_sha256: str
    producer: str
    producer_ref: str


def observation_exclusions(repo: Path) -> tuple[PurePosixPath, ...]:
    resolved = Path(repo).resolve()
    return candidate_exclusions(resolved, resolved / STATE_ROOT_PREFIX)


def _git_head(repo: Path) -> str:
    with _unmocked_subprocess_run():
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
        )
    if result.returncode != 0:
        _fail("CANDIDATE_REPO_INVALID", "cannot read repository HEAD")
    head = result.stdout.strip()
    if not re.fullmatch(r"^[0-9a-f]{40}$", head):
        _fail("CANDIDATE_REPO_INVALID", "HEAD is not a 40-character commit")
    return head


def capture_fs_snapshot(repo: Path, *, exclusions: tuple[PurePosixPath, ...]) -> FSSnapshot:
    resolved = Path(repo).resolve()
    with _unmocked_subprocess_run():
        manifest = scan_candidate_manifest(resolved, exclusions=exclusions)
    return FSSnapshot(
        root=resolved,
        entries=tuple(
            FSEntry(
                path=entry.path,
                mode=entry.mode,
                kind=entry.kind,
                content_sha256=entry.content_sha256,
            )
            for entry in manifest
        ),
        head=_git_head(resolved),
    )


def diff_fs_snapshots(before: FSSnapshot, after: FSSnapshot) -> tuple[FSChange, ...]:
    before_map = {entry.path: entry for entry in before.entries}
    after_map = {entry.path: entry for entry in after.entries}
    changes: list[FSChange] = []
    for path in sorted(set(before_map) | set(after_map)):
        left = before_map.get(path)
        right = after_map.get(path)
        if left is None:
            changes.append(FSChange(path=path, change_kind="ADDED", entry_after=right))
        elif right is None:
            changes.append(FSChange(path=path, change_kind="DELETED", entry_after=None))
        elif left != right:
            changes.append(FSChange(path=path, change_kind="MODIFIED", entry_after=right))
    return tuple(changes)


def _under_prefix(path: str, prefix: PurePosixPath) -> bool:
    posix = PurePosixPath(path)
    return posix == prefix or prefix in posix.parents


def _scopes_overlap(left: PurePosixPath, right: PurePosixPath) -> bool:
    return left == right or left in right.parents or right in left.parents


def classify_side_effect(change: FSChange, *, path_owners: Mapping[str, str]) -> str:
    if any(_under_prefix(change.path, prefix) for prefix in _CONTROL_PLANE_PREFIXES):
        return "CONTROL_PLANE_ARTIFACT"
    posix = PurePosixPath(change.path)
    for owned in path_owners:
        if _scopes_overlap(posix, PurePosixPath(owned)):
            return "OWNED_WRITE"
    return "UNTRACKED_WRITE"


def _command_item(event: Mapping[str, object]) -> Mapping[str, object] | None:
    item = event.get("item")
    if isinstance(item, Mapping) and item.get("type") in _COMMAND_ITEM_TYPES:
        return item
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        nested = payload.get("item")
        if isinstance(nested, Mapping) and nested.get("type") in _COMMAND_ITEM_TYPES:
            return nested
    if event.get("type") in _COMMAND_ITEM_TYPES:
        return event
    return None


def _command_sha256(command: object) -> str:
    if isinstance(command, str):
        material = command.encode("utf-8")
    else:
        material = canonical_json(command).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def extract_command_executions(
    rollout_events: tuple[Mapping[str, object], ...]
) -> tuple[CommandExecution, ...]:
    extracted: list[CommandExecution] = []
    for index, event in enumerate(rollout_events):
        if not isinstance(event, Mapping):
            continue
        item = _command_item(event)
        if item is None:
            continue
        command = item.get("command", item)
        extracted.append(
            CommandExecution(
                command_sha256=_command_sha256(command),
                producer="ROLLOUT_TOOL_EVENTS",
                producer_ref=str(index),
            )
        )
    return tuple(extracted)


def construction_step_producer_ref(*, plan_sha256: str, subtask_id: str) -> str:
    digest = _string(plan_sha256, "plan_sha256")
    if not _HEX64.fullmatch(digest):
        _fail("INVALID_TYPE", "plan_sha256 must be a lowercase 64-hex digest")
    step = _string(subtask_id, "subtask_id")
    return f"{digest}:{step}"


def retag_command_executions(
    executions: tuple[CommandExecution, ...],
    *,
    producer: str,
    producer_ref: str,
) -> tuple[CommandExecution, ...]:
    if producer not in COMMAND_PRODUCERS:
        _fail("INVALID_ENUM", "producer is not supported")
    ref = _string(producer_ref, "producer_ref")
    return tuple(
        CommandExecution(
            command_sha256=item.command_sha256,
            producer=producer,
            producer_ref=ref,
        )
        for item in executions
    )


def derive_effectful_roles(config: Mapping[str, object]) -> frozenset[str]:
    roles = config.get("roles")
    if not isinstance(roles, Mapping):
        return frozenset()
    names: list[str] = []
    for name, spec in roles.items():
        if isinstance(name, str) and isinstance(spec, Mapping):
            if spec.get("sandbox") in EFFECTFUL_ROLE_SANDBOXES:
                names.append(name)
    return frozenset(names)


def record_external_side_effect_locked(
    store: TaskStoreProtocol, task_id: str, *, role: str, permit_id: str
) -> None:
    store._assert_lock_held(task_id)
    record_side_effect_locked(
        store,
        task_id,
        role=role,
        path="EXTERNAL",
        effect_kind="EXTERNAL",
        permit_id=permit_id,
    )


def record_unobserved_side_effect(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    role: str,
    permit_id: str | None,
    reason: str,
) -> None:
    _string(reason, "reason")
    record_side_effect(
        store,
        task_id,
        role=role,
        path="UNOBSERVED",
        effect_kind="UNOBSERVED_ASSUMED_PRESENT",
        permit_id=permit_id,
    )


def observe_execution_side_effects(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    role: str,
    permit_id: str | None,
    before: FSSnapshot,
    after: FSSnapshot,
    rollout_events: tuple[Mapping[str, object], ...] = (),
    construction_step: Mapping[str, object] | None = None,
) -> tuple[FSChange, ...]:
    registry = load_ownership_registry(store, task_id)
    path_owners: Mapping[str, str] = {} if registry is None else registry.path_owners
    changes = diff_fs_snapshots(before, after)
    for change in changes:
        record_side_effect(
            store,
            task_id,
            role=role,
            path=change.path,
            effect_kind=classify_side_effect(change, path_owners=path_owners),
            permit_id=permit_id,
        )
    executions = extract_command_executions(rollout_events)
    if executions:
        if construction_step is None:
            attributed = executions
        else:
            attributed = retag_command_executions(
                executions,
                producer="CONSTRUCTION_FROZEN_STEP",
                producer_ref=construction_step_producer_ref(
                    plan_sha256=_string(construction_step.get("plan_sha256"), "plan_sha256"),
                    subtask_id=_string(construction_step.get("subtask_id"), "subtask_id"),
                ),
            )
        record_side_effect(
            store,
            task_id,
            role=role,
            path="COMMAND_GENERATED",
            effect_kind="COMMAND_GENERATED",
            permit_id=permit_id,
            extra={
                "producer": attributed[0].producer,
                "producer_ref": attributed[0].producer_ref,
                "command_sha256s": [item.command_sha256 for item in attributed],
            },
        )
    return changes
