"""Append-only plan scheduler: select a ready batch and record dispatch proposals.

This module consumes a ``validate_plan`` FrozenPlan, derives completed and
dispatched sets from ``scheduler.jsonl``, and calls ``ready_batch``.  It does
not run a model, create git worktrees, or mutate FrozenPlan / ready_batch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

try:
    from .ai_workflow_artifacts import artifact_sha256
    from .ai_workflow_ownership import ensure_ownership_registry_for_paths_locked
    from .ai_workflow_planning import (
        FrozenPlan,
        FrozenSubtask,
        dispatch_id,
        normalize_scope,
        ready_batch,
        record_dispatch,
        validate_plan,
    )
    from .ai_workflow_declarations import load_route_declaration_locked
except ImportError:  # direct script execution
    from ai_workflow_artifacts import artifact_sha256
    from ai_workflow_ownership import ensure_ownership_registry_for_paths_locked
    from ai_workflow_planning import (
        FrozenPlan,
        FrozenSubtask,
        dispatch_id,
        normalize_scope,
        ready_batch,
        record_dispatch,
        validate_plan,
    )
    from ai_workflow_declarations import load_route_declaration_locked


SCHEMA_VERSION = "plan-scheduler-1"
RECEIPT_SCHEMA_VERSION = "construction-receipt-1"
LEDGER_NAME = "scheduler.jsonl"
PLAN_NAME = "scheduler-plan.json"
PARENT_BINDING_NAME = "scheduler-parent.json"
MAX_RESULT_BYTES = 1024 * 1024
MAX_TASK_BYTES = 1024 * 1024
EVENT_TYPES = frozenset(
    {
        "SCHEDULER_OPENED",
        "STEP_DISPATCHED",
        "STEP_RECEIPTED",
        "FINAL_ACCEPTANCE_OPENED",
    }
)
RECEIPT_STATUSES = frozenset(
    {"IMPLEMENTED_CANDIDATE", "NEEDS_CLARIFICATION", "BLOCKED"}
)
RECEIPT_FIELDS = (
    "schema_version",
    "task_id",
    "subtask_id",
    "dispatch_id",
    "plan_sha256",
    "task_sha256",
    "candidate_commit",
    "result_sha256",
    "status",
)
COMMON_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_type",
        "event_index",
        "event_id",
        "previous_event_id",
        "timestamp",
        "task_id",
        "plan_sha256",
        "task_sha256",
    }
)
EVENT_FIELDS = {
    "SCHEDULER_OPENED": COMMON_EVENT_FIELDS | {"candidate_commit"},
    "STEP_DISPATCHED": COMMON_EVENT_FIELDS
    | {"subtask_id", "dispatch_id", "owner_role", "worktree_path", "attempt", "scope_sha256"},
    "STEP_RECEIPTED": COMMON_EVENT_FIELDS | {"receipt"},
    "FINAL_ACCEPTANCE_OPENED": COMMON_EVENT_FIELDS
    | {"acceptance_task_id", "candidate_commit", "acceptance_task_sha256"},
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SAFE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
WRITE_ROLES = frozenset({"luna_construction", "terra", "terra_xhigh"})
SECTION_READ_ROLES = frozenset({"luna"})
SECTION_ROLES = WRITE_ROLES | SECTION_READ_ROLES


def _workflow_error(code: str, message: str) -> BaseException:
    try:
        from .ai_workflow import WorkflowError
    except (ImportError, ModuleNotFoundError):
        from ai_workflow import WorkflowError
    return WorkflowError(code, message)


def _fail(code: str, message: str) -> None:
    raise _workflow_error(code, message)


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_id(event: Mapping[str, object]) -> str:
    payload = {key: value for key, value in event.items() if key != "event_id"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _sha256_string(value: object, *, field: str, code: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        _fail(code, f"{field} must be a SHA256 digest")
    return value


def _nonempty_string(value: object, *, field: str, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(code, f"{field} must be a non-empty string")
    return value


def _candidate_commit(value: object, *, field: str = "candidate_commit", code: str = "FINAL_CANDIDATE_INVALID") -> str:
    if not isinstance(value, str) or not COMMIT_PATTERN.fullmatch(value):
        _fail(code, f"{field} must be a 40-character lowercase hex commit")
    return value


def _workflow():
    try:
        from . import ai_workflow as workflow
    except ImportError:
        import ai_workflow as workflow
    return workflow


def _repairs():
    try:
        from . import ai_workflow_repairs as repairs
    except ImportError:
        import ai_workflow_repairs as repairs
    return repairs


def _append_jsonl(path: Path, record: Mapping[str, object]) -> None:
    _workflow().append_jsonl(path, record)


def _timestamp() -> str:
    return _workflow()._utc_timestamp()


def _load_config() -> dict[str, object]:
    return _workflow()._load_workflow_config()


def _stored_task(store: object, task_id: str) -> dict[str, object]:
    require_task = getattr(store, "_require_task", None)
    if not callable(require_task):
        _fail("PLAN_INVALID", "scheduler requires the workflow append-only store")
    try:
        value = json.loads((require_task(task_id) / "task.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _workflow_error("DISPATCH_IDENTITY_DRIFT", "cannot read stored scheduler task") from exc
    if not isinstance(value, Mapping):
        _fail("DISPATCH_IDENTITY_DRIFT", "stored scheduler task must be an object")
    return dict(value)


def _revalidate_plan(store: object, plan: FrozenPlan) -> tuple[FrozenPlan, dict[str, object]]:
    if not isinstance(plan, FrozenPlan):
        _fail("PLAN_INVALID", "scheduler requires a frozen plan")
    stored_task = _stored_task(store, plan.task_id)
    revalidated = validate_plan(plan.to_dict(), stored_task)
    if (
        revalidated.plan_sha256 != plan.plan_sha256
        or revalidated.task_sha256 != plan.task_sha256
        or revalidated.candidate_commit != plan.candidate_commit
    ):
        _fail("DISPATCH_IDENTITY_DRIFT", "frozen plan identity does not match stored task")
    if not isinstance(plan.candidate_commit, str) or not plan.candidate_commit:
        _fail("DISPATCH_IDENTITY_DRIFT", "frozen plan requires a candidate_commit")
    _assert_safe_plan_ids(plan)
    return revalidated, stored_task


def _scope_sha256(task: FrozenSubtask) -> str:
    return artifact_sha256(
        {
            "read_scope": list(task.read_scope),
            "write_scope": list(task.write_scope),
            "do_not_touch": list(task.do_not_touch),
        }
    )


def _slot_limits(config: Mapping[str, object] | None = None) -> tuple[int, int]:
    document = dict(config) if isinstance(config, Mapping) else _load_config()
    automation = document.get("automation")
    if not isinstance(automation, Mapping):
        _fail("CAPACITY_UNAVAILABLE", "automation configuration is required")
    read_only = automation.get("max_parallel_read_only")
    writers = automation.get("max_active_writers")
    if isinstance(read_only, bool) or not isinstance(read_only, int) or read_only < 0:
        _fail("CAPACITY_UNAVAILABLE", "max_parallel_read_only must be a non-negative integer")
    if isinstance(writers, bool) or not isinstance(writers, int) or writers < 0:
        _fail("CAPACITY_UNAVAILABLE", "max_active_writers must be a non-negative integer")
    return read_only, writers


def _ledger_path(store: object, task_id: str) -> Path:
    require_task = getattr(store, "_require_task", None)
    if not callable(require_task):
        _fail("PLAN_INVALID", "scheduler requires the workflow append-only store")
    return Path(require_task(task_id)) / LEDGER_NAME


def _ensure_plan_artifact(store: object, plan: FrozenPlan) -> FrozenPlan:
    path = _ledger_path(store, plan.task_id).with_name(PLAN_NAME)
    document = plan.to_dict()
    if not path.exists():
        _workflow().write_json_once(
            path,
            document,
            conflict_code="SCHEDULER_PLAN_MISMATCH",
        )
    if path.is_symlink() or not path.is_file():
        _fail("SCHEDULER_PLAN_MISMATCH", "scheduler plan artifact is not a regular file")
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _workflow_error(
            "SCHEDULER_PLAN_MISMATCH", "cannot read scheduler plan artifact"
        ) from exc
    frozen = validate_plan(stored, _stored_task(store, plan.task_id))
    if (
        frozen.plan_sha256 != plan.plan_sha256
        or frozen.task_sha256 != plan.task_sha256
        or frozen.candidate_commit != plan.candidate_commit
    ):
        _fail("SCHEDULER_PLAN_MISMATCH", "scheduler plan artifact identity drifted")
    return frozen


def _assert_safe_id(value: str, *, field: str) -> str:
    if any(marker in value for marker in ("\x00", "\n", "/", "\\")):
        _fail("SCHEDULER_WORKTREE_INVALID", f"{field} contains an unsafe path character")
    if value != value.casefold() or not SAFE_ID_PATTERN.fullmatch(value):
        _fail("SCHEDULER_WORKTREE_INVALID", f"{field} is not a lowercase safe identifier")
    return value


def _assert_safe_plan_ids(plan: FrozenPlan) -> None:
    _assert_safe_id(plan.task_id.casefold(), field="task_id")
    folded = [task.id.casefold() for task in plan.tasks]
    if len(folded) != len(set(folded)):
        _fail("SCHEDULER_WORKTREE_INVALID", "plan subtask ids collide after casefold")
    for task in plan.tasks:
        _assert_safe_id(task.id, field="subtask_id")


def isolated_worktree_path(repository_root: str, task_id: str, subtask_id: str) -> Path:
    """Return the proposed per-step worktree path without creating it."""

    root = Path(_nonempty_string(repository_root, field="repository_root", code="PLAN_INVALID"))
    if not root.is_absolute():
        _fail("SCHEDULER_WORKTREE_INVALID", "repository_root must be absolute")
    root = root.resolve()
    identifier = _nonempty_string(subtask_id, field="subtask_id", code="PLAN_INVALID")
    folded_task = _assert_safe_id(task_id.casefold(), field="task_id")
    _assert_safe_id(identifier, field="subtask_id")
    path = (root / ".codex-worktrees" / folded_task / identifier).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        _fail("SCHEDULER_WORKTREE_INVALID", "subtask worktree path would escape the repository")
    return path


def _require_role(role: str) -> str:
    if role in SECTION_ROLES:
        return role
    _fail("SCHEDULER_ROLE_INVALID", f"scheduler cannot dispatch role {role}")
    raise AssertionError("unreachable")


def _assert_section_roles(plan: FrozenPlan) -> None:
    for task in plan.tasks:
        _require_role(task.owner_role)


@dataclass
class SchedulerReplay:
    events: list[dict[str, object]] = field(default_factory=list)
    completed: set[str] = field(default_factory=set)
    dispatched: set[str] = field(default_factory=set)
    receipted: set[str] = field(default_factory=set)
    receipted_dispatch_ids: set[str] = field(default_factory=set)
    in_flight: dict[str, str] = field(default_factory=dict)
    worktree_paths: dict[str, str] = field(default_factory=dict)
    dispatches: dict[str, dict[str, object]] = field(default_factory=dict)
    final_acceptance_task_id: str | None = None
    final_acceptance_task_sha256: str | None = None
    last_event_id: str | None = None
    opened: bool = False


def _validate_event_shape(event: Mapping[str, object], plan: FrozenPlan, expected_index: int, previous_id: str | None) -> None:
    if event.get("schema_version") != SCHEMA_VERSION:
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler event schema_version is invalid")
    event_type = event.get("event_type")
    if event_type not in EVENT_TYPES:
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler event_type is not in the closed set")
    if set(event) != EVENT_FIELDS[str(event_type)]:
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler event fields are not the exact closed set")
    if event.get("event_index") != expected_index:
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler event_index does not chain")
    if event.get("previous_event_id") != previous_id:
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler previous_event_id does not chain")
    if event.get("task_id") != plan.task_id:
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler event task_id does not match the frozen plan")
    if event.get("plan_sha256") != plan.plan_sha256 or event.get("task_sha256") != plan.task_sha256:
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler event is not bound to the frozen plan")
    if not isinstance(event.get("timestamp"), str) or not event["timestamp"]:
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler timestamp is invalid")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not SHA256_PATTERN.fullmatch(event_id) or event_id != _event_id(event):
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler event_id does not match the canonical payload")


def replay_scheduler(store: object, plan: FrozenPlan) -> SchedulerReplay:
    """Fail-closed replay of the append-only scheduler ledger."""

    path = _ledger_path(store, plan.task_id)
    try:
        raw_bytes = _workflow()._read_regular_file(
            path,
            error_code="SCHEDULER_LEDGER_INVALID",
            missing_ok=True,
        )
        if raw_bytes is None:
            return SchedulerReplay()
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _workflow_error("SCHEDULER_LEDGER_INVALID", "scheduler ledger is not valid UTF-8") from exc
    except FileNotFoundError:
        return SchedulerReplay()

    replay = SchedulerReplay()
    tasks_by_id = {task.id: task for task in plan.tasks}
    stored_root = _stored_task(store, plan.task_id)["repository_root"]
    for line_number, line in enumerate(raw.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _workflow_error("SCHEDULER_LEDGER_INVALID", "scheduler ledger contains invalid JSON") from exc
        if not isinstance(event, dict):
            _fail("SCHEDULER_LEDGER_INVALID", "scheduler event must be an object")
        _validate_event_shape(event, plan, line_number, replay.last_event_id)
        event_type = event["event_type"]
        if line_number == 0 and event_type != "SCHEDULER_OPENED":
            _fail("SCHEDULER_LEDGER_INVALID", "scheduler ledger must start with SCHEDULER_OPENED")
        if event_type == "SCHEDULER_OPENED":
            if replay.opened:
                _fail("SCHEDULER_LEDGER_INVALID", "scheduler was opened more than once")
            if event.get("candidate_commit") != plan.candidate_commit:
                _fail("SCHEDULER_LEDGER_INVALID", "SCHEDULER_OPENED candidate_commit does not match")
            replay.opened = True
        elif event_type == "STEP_DISPATCHED":
            subtask_id = _nonempty_string(event.get("subtask_id"), field="subtask_id", code="SCHEDULER_LEDGER_INVALID")
            selected = tasks_by_id.get(subtask_id)
            if selected is None:
                _fail("SCHEDULER_LEDGER_INVALID", "STEP_DISPATCHED references an unknown subtask")
            owner_role = _nonempty_string(event.get("owner_role"), field="owner_role", code="SCHEDULER_LEDGER_INVALID")
            if owner_role != selected.owner_role:
                _fail("SCHEDULER_LEDGER_INVALID", "STEP_DISPATCHED owner_role does not match the frozen plan")
            _require_role(owner_role)
            attempt = event.get("attempt")
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                _fail("SCHEDULER_LEDGER_INVALID", "STEP_DISPATCHED attempt is invalid")
            worktree_path = _nonempty_string(
                event.get("worktree_path"), field="worktree_path", code="SCHEDULER_LEDGER_INVALID"
            )
            expected_path = isolated_worktree_path(str(stored_root), plan.task_id, subtask_id)
            if worktree_path != str(expected_path):
                _fail("SCHEDULER_LEDGER_INVALID", "STEP_DISPATCHED worktree_path is not the isolated path")
            for other_id, other_path in replay.worktree_paths.items():
                if other_path == worktree_path and other_id != subtask_id:
                    _fail("SCHEDULER_LEDGER_INVALID", "STEP_DISPATCHED worktree_path is not unique")
            identity = _sha256_string(
                event.get("dispatch_id"), field="dispatch_id", code="SCHEDULER_LEDGER_INVALID"
            )
            expected_identity = dispatch_id(
                plan.plan_sha256,
                plan.task_sha256,
                subtask_id,
                attempt,
                str(plan.candidate_commit),
            )
            if identity != expected_identity:
                _fail("SCHEDULER_LEDGER_INVALID", "STEP_DISPATCHED dispatch_id does not match planning identity")
            if event.get("scope_sha256") != _scope_sha256(selected):
                _fail("SCHEDULER_LEDGER_INVALID", "STEP_DISPATCHED scope_sha256 does not match the frozen step")
            ready = ready_batch(
                plan,
                replay.completed,
                replay.dispatched,
                capacity=len(plan.tasks),
            )
            if subtask_id not in ready:
                _fail(
                    "SCHEDULER_LEDGER_INVALID",
                    "STEP_DISPATCHED references a step that was not ready",
                )
            replay.dispatched.add(subtask_id)
            replay.in_flight[subtask_id] = owner_role
            replay.worktree_paths[subtask_id] = worktree_path
            replay.dispatches[identity] = dict(event)
        elif event_type == "STEP_RECEIPTED":
            receipt = event.get("receipt")
            if not isinstance(receipt, Mapping):
                _fail("SCHEDULER_LEDGER_INVALID", "STEP_RECEIPTED requires a construction receipt")
            value = _validate_receipt(
                store, receipt, plan, replay, code="SCHEDULER_LEDGER_INVALID"
            )
            subtask_id = str(value["subtask_id"])
            identity = str(value["dispatch_id"])
            replay.receipted.add(subtask_id)
            replay.receipted_dispatch_ids.add(identity)
            replay.in_flight.pop(subtask_id, None)
            replay.dispatched.discard(subtask_id)
            if value["status"] == "IMPLEMENTED_CANDIDATE":
                replay.completed.add(subtask_id)
        elif event_type == "FINAL_ACCEPTANCE_OPENED":
            if replay.final_acceptance_task_id is not None:
                _fail("SCHEDULER_LEDGER_INVALID", "FINAL_ACCEPTANCE_OPENED appears more than once")
            if set(tasks_by_id) - replay.completed:
                _fail("SCHEDULER_LEDGER_INVALID", "FINAL_ACCEPTANCE_OPENED before every step is completed")
            _candidate_commit(event.get("candidate_commit"), code="SCHEDULER_LEDGER_INVALID")
            replay.final_acceptance_task_id = _nonempty_string(
                event.get("acceptance_task_id"),
                field="acceptance_task_id",
                code="SCHEDULER_LEDGER_INVALID",
            )
            replay.final_acceptance_task_sha256 = _sha256_string(
                event.get("acceptance_task_sha256"),
                field="acceptance_task_sha256",
                code="SCHEDULER_LEDGER_INVALID",
            )
            child_bytes = _child_task_bytes(store, replay.final_acceptance_task_id)
            if child_bytes is None:
                _fail("SCHEDULER_LEDGER_INVALID", "bound acceptance child task is missing")
            try:
                stored_child = json.loads(child_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                _fail("SCHEDULER_LEDGER_INVALID", "acceptance child task is not valid JSON")
            if not isinstance(stored_child, Mapping) or artifact_sha256(stored_child) != replay.final_acceptance_task_sha256:
                _fail("SCHEDULER_LEDGER_INVALID", "acceptance_task_sha256 does not match the child task")
        replay.events.append(event)
        replay.last_event_id = str(event["event_id"])
    return replay


def _validate_receipt(
    store: object,
    receipt: Mapping[str, object],
    plan: FrozenPlan,
    replay: SchedulerReplay,
    *,
    code: str,
) -> dict[str, object]:
    if set(receipt) != set(RECEIPT_FIELDS):
        _fail(code, "construction receipt fields are not the strict closed set")
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        _fail(code, "construction receipt schema_version must be construction-receipt-1")
    task_id = _nonempty_string(receipt.get("task_id"), field="task_id", code=code)
    subtask_id = _nonempty_string(receipt.get("subtask_id"), field="subtask_id", code=code)
    if task_id != plan.task_id:
        _fail(code, "construction receipt task_id does not match the frozen plan")
    plan_sha256 = _sha256_string(receipt.get("plan_sha256"), field="plan_sha256", code=code)
    task_sha256 = _sha256_string(receipt.get("task_sha256"), field="task_sha256", code=code)
    candidate = _nonempty_string(receipt.get("candidate_commit"), field="candidate_commit", code=code)
    if plan_sha256 != plan.plan_sha256 or task_sha256 != plan.task_sha256 or candidate != plan.candidate_commit:
        _fail(code, "construction receipt is not bound to the frozen plan")
    _sha256_string(receipt.get("result_sha256"), field="result_sha256", code=code)
    status = receipt.get("status")
    if status not in RECEIPT_STATUSES:
        _fail(code, "construction receipt status is not in the closed set")
    identity = _sha256_string(receipt.get("dispatch_id"), field="dispatch_id", code=code)
    if identity in replay.receipted_dispatch_ids:
        _fail("DUPLICATE_RECEIPT", "construction receipt dispatch_id was already receipted")
    recorded = replay.dispatches.get(identity)
    if recorded is None:
        _fail(code, "construction receipt does not match a dispatched step")
    if recorded["subtask_id"] != subtask_id:
        _fail(code, "construction receipt dispatch_id does not match the dispatched step")
    expected_identity = dispatch_id(
        plan.plan_sha256,
        plan.task_sha256,
        subtask_id,
        int(recorded["attempt"]),
        str(plan.candidate_commit),
    )
    if identity != expected_identity:
        _fail(code, "construction receipt dispatch_id does not match planning identity")
    _validate_result_evidence(store, receipt, recorded)
    return {field: receipt[field] for field in RECEIPT_FIELDS}


def _validate_result_evidence(
    store: object,
    receipt: Mapping[str, object],
    dispatch: Mapping[str, object],
) -> None:
    task_dir = Path(store._require_task(str(receipt["task_id"])))
    result_dir = task_dir / "scheduler-results"
    filename = f"{receipt['dispatch_id']}.json"
    task_descriptor = -1
    result_dir_descriptor = -1
    descriptor = -1

    def close_opened() -> None:
        for opened_descriptor in (
            descriptor,
            result_dir_descriptor,
            task_descriptor,
        ):
            if opened_descriptor >= 0:
                os.close(opened_descriptor)

    try:
        task_descriptor = os.open(
            task_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        result_dir_descriptor = os.open(
            "scheduler-results",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=task_descriptor,
        )
        descriptor = os.open(
            filename,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=result_dir_descriptor,
        )
        current = os.stat(
            "scheduler-results", dir_fd=task_descriptor, follow_symlinks=False
        )
        opened = os.fstat(result_dir_descriptor)
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            close_opened()
            _fail("RECEIPT_RESULT_UNSAFE", "scheduler result directory changed during open")
    except FileNotFoundError:
        close_opened()
        _fail("RECEIPT_RESULT_MISSING", "controller result file is missing")
    except OSError as exc:
        close_opened()
        raise _workflow_error("RECEIPT_RESULT_UNSAFE", "controller result file is unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_RESULT_BYTES
        ):
            _fail("RECEIPT_RESULT_UNSAFE", "controller result must be a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if result_dir_descriptor >= 0:
            os.close(result_dir_descriptor)
        if task_descriptor >= 0:
            os.close(task_descriptor)
    if hashlib.sha256(raw).hexdigest() != receipt["result_sha256"]:
        _fail("RECEIPT_RESULT_HASH_MISMATCH", "controller result hash does not match receipt")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _workflow_error(
            "RECEIPT_RESULT_IDENTITY_MISMATCH", "controller result is not valid JSON"
        ) from exc
    expected_result_status = receipt.get("status")
    if dispatch.get("owner_role") == "luna":
        expected_result_status = {
            "IMPLEMENTED_CANDIDATE": "SUPPORTED",
            "NEEDS_CLARIFICATION": "PARTIALLY_SUPPORTED",
            "BLOCKED": "BLOCKED",
        }.get(str(receipt.get("status")))
    if (
        not isinstance(result, Mapping)
        or result.get("schema_version") != "ai-result-1"
        or result.get("role") != dispatch.get("owner_role")
        or result.get("status") != expected_result_status
        or result.get("dispatch_id") != receipt.get("dispatch_id")
        or result.get("task_id") != receipt.get("task_id")
        or result.get("step_id") != dispatch.get("subtask_id")
        or result.get("attempt") != dispatch.get("attempt")
    ):
        _fail(
            "RECEIPT_RESULT_IDENTITY_MISMATCH",
            "controller result role or status does not match dispatch receipt",
        )
    changed = result.get("changed_files")
    if not isinstance(changed, list) or any(not isinstance(item, str) for item in changed):
        _fail("RECEIPT_RESULT_IDENTITY_MISMATCH", "controller result changed_files is invalid")
    _workflow().validate_role_result(str(dispatch["owner_role"]), result, set(changed))


def _select_ready(
    plan: FrozenPlan,
    ready: tuple[str, ...],
    in_flight: Mapping[str, str],
    max_read_only: int,
    max_writers: int,
) -> tuple[str, ...]:
    tasks_by_id = {task.id: task for task in plan.tasks}
    read_used = sum(1 for role in in_flight.values() if role in SECTION_READ_ROLES)
    write_used = sum(1 for role in in_flight.values() if role in WRITE_ROLES)
    selected: list[str] = []
    for identifier in ready:
        role = _require_role(tasks_by_id[identifier].owner_role)
        if role in WRITE_ROLES:
            if write_used >= max_writers:
                continue
            write_used += 1
        else:
            if read_used >= max_read_only:
                continue
            read_used += 1
        selected.append(identifier)
    return tuple(selected)


def _append_event(
    store: object,
    plan: FrozenPlan,
    replay: SchedulerReplay,
    event_type: str,
    fields: Mapping[str, object],
) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "event_index": len(replay.events),
        "previous_event_id": replay.last_event_id,
        "timestamp": _timestamp(),
        "task_id": plan.task_id,
        "plan_sha256": plan.plan_sha256,
        "task_sha256": plan.task_sha256,
    }
    event.update(fields)
    if set(event) | {"event_id"} != EVENT_FIELDS[event_type]:
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler event fields are not the exact closed set")
    event["event_id"] = _event_id(event)
    _append_jsonl(_ledger_path(store, plan.task_id), event)
    replay.events.append(event)
    replay.last_event_id = str(event["event_id"])
    return event


def _ensure_opened(store: object, plan: FrozenPlan, replay: SchedulerReplay) -> None:
    if replay.opened:
        return
    _append_event(store, plan, replay, "SCHEDULER_OPENED", {"candidate_commit": plan.candidate_commit})
    replay.opened = True


def _expected_dispatch_record(
    plan: FrozenPlan, selected: FrozenSubtask, attempt: int, identity: str
) -> dict[str, object]:
    return {
        "event_type": "DISPATCH_RECORDED",
        "owner_task_id": selected.id,
        "owner_role": selected.owner_role,
        "plan_sha256": plan.plan_sha256,
        "task_sha256": plan.task_sha256,
        "scope_sha256": _scope_sha256(selected),
        "subtask_id": selected.id,
        "attempt": attempt,
        "candidate_commit": plan.candidate_commit,
        "dispatch_id": identity,
    }


def _load_dispatch_record(store: object, task_id: str, identity: str) -> dict[str, object] | None:
    require_task = getattr(store, "_require_task", None)
    if not callable(require_task):
        _fail("PLAN_INVALID", "scheduler requires the workflow append-only store")
    path = Path(require_task(task_id)) / "dispatches.jsonl"
    try:
        raw = _workflow()._read_regular_file(
            path,
            error_code="DISPATCH_READ_ERROR",
            missing_ok=True,
        )
        if raw is None:
            return None
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise _workflow_error("DISPATCH_READ_ERROR", "dispatch ledger is not valid UTF-8") from exc
    except FileNotFoundError:
        return None
    matched: dict[str, object] | None = None
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _workflow_error("DISPATCH_IDENTITY_DRIFT", "dispatch ledger contains invalid JSON") from exc
        if not isinstance(record, dict):
            _fail("DISPATCH_IDENTITY_DRIFT", "dispatch ledger contains an invalid record")
        if record.get("dispatch_id") == identity:
            if matched is not None:
                _fail("DISPATCH_IDENTITY_DRIFT", "dispatch ledger contains a duplicate identity")
            matched = dict(record)
    return matched


def _record_or_recover_dispatch(
    store: object,
    plan: FrozenPlan,
    selected: FrozenSubtask,
    attempt: int,
    replay: SchedulerReplay,
) -> str:
    identity = dispatch_id(
        plan.plan_sha256,
        plan.task_sha256,
        selected.id,
        attempt,
        str(plan.candidate_commit),
    )
    expected = _expected_dispatch_record(plan, selected, attempt, identity)
    prior = _load_dispatch_record(store, plan.task_id, identity)
    already_scheduled = any(
        event.get("event_type") == "STEP_DISPATCHED" and event.get("dispatch_id") == identity
        for event in replay.events
    )
    if prior is not None:
        if prior != expected:
            _fail("ORPHAN_DISPATCH_MISMATCH", "existing dispatch record does not match this attempt")
        if already_scheduled:
            _fail("DUPLICATE_DISPATCH", "dispatch identity has already been recorded")
        return identity
    recorded = record_dispatch(
        store,
        plan.task_id,
        plan,
        selected.id,
        attempt,
        str(plan.candidate_commit),
        store_locked=True,
    )
    if recorded != identity:
        _fail("DISPATCH_IDENTITY_DRIFT", "recorded dispatch identity drifted")
    return identity


def _reject_locked_dispatch(replay: SchedulerReplay, subtask_id: str) -> None:
    if replay.final_acceptance_task_id is not None:
        _fail("FINAL_ACCEPTANCE_ALREADY_OPEN", "final acceptance was already opened")
    if subtask_id in replay.completed:
        _fail("STEP_ALREADY_COMPLETED", "subtask already has an IMPLEMENTED_CANDIDATE receipt")
    if subtask_id in replay.in_flight:
        _fail("STEP_IN_FLIGHT", "subtask still has an open dispatch attempt")


def _dispatch_step_locked(
    store: object,
    plan: FrozenPlan,
    stored_task: Mapping[str, object],
    subtask_id: str,
    attempt: int,
    replay: SchedulerReplay,
) -> dict[str, object]:
    _reject_locked_dispatch(replay, subtask_id)
    tasks_by_id = {task.id: task for task in plan.tasks}
    selected = tasks_by_id.get(subtask_id)
    if selected is None:
        _fail("PLAN_INVALID", "dispatch references an unknown subtask")
    owner_role = _require_role(selected.owner_role)
    declaration = load_route_declaration_locked(store, plan.task_id)
    if declaration is None:
        _fail("ROUTE_DECLARATION_MISSING", "route declaration is missing")
        raise AssertionError("unreachable")
    if owner_role not in declaration.allowed_roles:
        _fail("ROLE_NOT_ALLOWED", f"role {owner_role} is not allowed")
    worktree = isolated_worktree_path(str(stored_task["repository_root"]), plan.task_id, subtask_id)
    for other_id, other_path in replay.worktree_paths.items():
        if other_path == str(worktree) and other_id != subtask_id:
            _fail("SCHEDULER_WORKTREE_INVALID", "subtask worktree path collides with another step")
    identity = _record_or_recover_dispatch(store, plan, selected, attempt, replay)
    _ensure_opened(store, plan, replay)
    event = _append_event(
        store,
        plan,
        replay,
        "STEP_DISPATCHED",
        {
            "subtask_id": subtask_id,
            "dispatch_id": identity,
            "owner_role": owner_role,
            "worktree_path": str(worktree),
            "attempt": attempt,
            "scope_sha256": _scope_sha256(selected),
        },
    )
    replay.dispatched.add(subtask_id)
    replay.in_flight[subtask_id] = owner_role
    replay.worktree_paths[subtask_id] = str(worktree)
    replay.dispatches[identity] = dict(event)
    return event


def dispatch_step(
    store: object,
    plan: FrozenPlan,
    subtask_id: str,
    *,
    attempt: int = 1,
) -> dict[str, object]:
    """Record one dispatch identity and scheduler STEP_DISPATCHED event under the parent lock."""

    plan, stored_task = _revalidate_plan(store, plan)
    _assert_section_roles(plan)
    lock = getattr(store, "lock", None)
    if not callable(lock):
        _fail("PLAN_INVALID", "scheduler requires the workflow parent lock")
    with lock(plan.task_id):
        plan = _ensure_plan_artifact(store, plan)
        replay = replay_scheduler(store, plan)
        _reject_locked_dispatch(replay, subtask_id)
        ready = ready_batch(
            plan,
            replay.completed,
            replay.dispatched,
            len(plan.tasks),
        )
        if subtask_id not in ready:
            _fail("STEP_NOT_READY", "subtask dependencies or stage barrier are not satisfied")
        return _dispatch_step_locked(store, plan, stored_task, subtask_id, attempt, replay)


def dispatch_ready_batch(
    store: object,
    plan: FrozenPlan,
    *,
    config: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], ...]:
    """Select the current-stage batch that fits the configured slots and record proposals."""

    plan, stored_task = _revalidate_plan(store, plan)
    _assert_section_roles(plan)
    max_read_only, max_writers = _slot_limits(config)
    lock = getattr(store, "lock", None)
    if not callable(lock):
        _fail("PLAN_INVALID", "scheduler requires the workflow parent lock")
    with lock(plan.task_id):
        plan = _ensure_plan_artifact(store, plan)
        replay = replay_scheduler(store, plan)
        if replay.final_acceptance_task_id is not None:
            return ()
        ready = ready_batch(
            plan,
            set(replay.completed),
            set(replay.dispatched),
            len(plan.tasks),
        )
        selected = _select_ready(plan, ready, replay.in_flight, max_read_only, max_writers)
        if not selected:
            return ()
        declaration = load_route_declaration_locked(store, plan.task_id)
        if declaration is None:
            _fail("ROUTE_DECLARATION_MISSING", "route declaration is missing")
            raise AssertionError("unreachable")
        for subtask_id in selected:
            subtask = next(task for task in plan.tasks if task.id == subtask_id)
            if subtask.owner_role not in declaration.allowed_roles:
                _fail("ROLE_NOT_ALLOWED", f"role {subtask.owner_role} is not allowed")
        _ensure_opened(store, plan, replay)
        proposals: list[dict[str, object]] = []
        for subtask_id in selected:
            attempt = 1 + sum(
                1
                for event in replay.events
                if event.get("event_type") == "STEP_DISPATCHED" and event.get("subtask_id") == subtask_id
            )
            proposals.append(
                _dispatch_step_locked(store, plan, stored_task, subtask_id, attempt, replay)
            )
        return tuple(proposals)


def record_step_receipt(
    store: object,
    plan: FrozenPlan,
    receipt: Mapping[str, object],
) -> dict[str, object]:
    """Append one strict construction receipt; only IMPLEMENTED_CANDIDATE completes a step."""

    plan, _stored = _revalidate_plan(store, plan)
    if not isinstance(receipt, Mapping):
        _fail("RECEIPT_IDENTITY_DRIFT", "construction receipt must be an object")
    lock = getattr(store, "lock", None)
    if not callable(lock):
        _fail("PLAN_INVALID", "scheduler requires the workflow parent lock")
    with lock(plan.task_id):
        replay = replay_scheduler(store, plan)
        value = _validate_receipt(
            store,
            dict(receipt),
            plan,
            replay,
            code="RECEIPT_IDENTITY_DRIFT",
        )
        event = _append_event(store, plan, replay, "STEP_RECEIPTED", {"receipt": value})
        return event


def open_final_acceptance(
    store: object,
    plan: FrozenPlan,
    acceptance_task_id: str,
    candidate_commit: str,
) -> dict[str, object]:
    """Create and bind the unique ACCEPTANCE child.  There is no ghost ledger path."""

    return create_final_acceptance_case(store, plan, acceptance_task_id, candidate_commit)


def _acceptance_task_id(value: object) -> str:
    identifier = _nonempty_string(value, field="acceptance_task_id", code="INVALID_TASK_ID")
    if not _workflow().TASK_ID_PATTERN.fullmatch(identifier):
        _fail("INVALID_TASK_ID", "task_id must match AWF-YYYYMMDD-NNN")
    return identifier


def _acceptance_repo(stored_task: Mapping[str, object]) -> Path:
    root = stored_task.get("repository_root")
    if not isinstance(root, str) or not root.strip():
        _fail("PLAN_INVALID", "parent repository_root is required")
    return Path(root).resolve()


def _projected_source_worktree(stored_task: Mapping[str, object]) -> str | None:
    root = stored_task["repository_root"]
    source = stored_task.get("source_worktree")
    if not isinstance(source, str) or not source.strip():
        return None
    try:
        if Path(source).resolve() == Path(str(root)).resolve():
            return str(root)
    except OSError:
        return None
    return None


def _allowed_final_scopes(plan: FrozenPlan, stored_task: Mapping[str, object]) -> tuple[object, ...]:
    scopes = [normalize_scope(item) for item in stored_task["allowed_write_paths"]]
    for step in plan.tasks:
        for raw_scope in step.write_scope:
            scopes.append(normalize_scope(raw_scope))
    return tuple(scopes)


def _assert_final_candidate_binding(
    stored_task: Mapping[str, object],
    plan: FrozenPlan,
    candidate: str,
) -> None:
    workflow = _workflow()
    repo = _acceptance_repo(stored_task)
    workflow._reject_dirty_input(repo, "FINAL_CANDIDATE_DIRTY", "repository working tree is dirty")
    head = workflow.git(repo, "rev-parse", "HEAD")
    if head != candidate:
        _fail("FINAL_CANDIDATE_HEAD_MISMATCH", "repository HEAD must equal the final candidate")
    origin = _candidate_commit(plan.candidate_commit, field="plan.candidate_commit")
    try:
        workflow.git(repo, "merge-base", "--is-ancestor", origin, candidate)
    except workflow.WorkflowError as exc:
        if exc.code == "GIT_COMMAND_FAILED":
            _fail("FINAL_CANDIDATE_NOT_ANCESTOR", "plan candidate_commit must be an ancestor of the final candidate")
        raise
    changed = workflow.changed_paths(repo, origin, candidate)
    allowed = _allowed_final_scopes(plan, stored_task)
    for path in changed:
        scope = normalize_scope(path)
        if not any(scope == item or item in scope.parents for item in allowed):
            _fail("FINAL_CANDIDATE_SCOPE", "final candidate changes escape the authorized write union")


def _task_document_bytes(task: Mapping[str, object]) -> bytes:
    return (_workflow()._canonical_json(dict(task)) + "\n").encode("utf-8")


def _projected_final_acceptance_task(
    plan: FrozenPlan,
    stored_task: Mapping[str, object],
    acceptance_task_id: str,
    candidate_commit: str,
) -> dict[str, object]:
    parent_allowed = tuple(normalize_scope(item) for item in stored_task["allowed_write_paths"])
    write_paths: list[str] = []
    seen_paths: set[str] = set()
    for step in plan.tasks:
        for raw_scope in step.write_scope:
            scope = normalize_scope(raw_scope)
            if not any(scope == allowed or allowed in scope.parents for allowed in parent_allowed):
                _fail("PLAN_INVALID", "write_scope is outside parent allowed_write_paths")
            posix = scope.as_posix()
            if posix not in seen_paths:
                seen_paths.add(posix)
                write_paths.append(posix)
    write_paths.sort()
    commands: list[str] = []
    seen_commands: set[str] = set()
    for command in list(stored_task["acceptance_commands"]) + [
        command for step in plan.tasks for command in step.verification_commands
    ]:
        if command not in seen_commands:
            seen_commands.add(command)
            commands.append(command)
    forbidden = list(stored_task["forbidden_actions"])
    for action in ("merge", "push"):
        if action not in forbidden:
            forbidden.append(action)
    return {
        "schema_version": "ai-task-1",
        "task_id": acceptance_task_id,
        "task_type": "ACCEPTANCE",
        "objective": plan.goal,
        "repository_root": stored_task["repository_root"],
        "source_worktree": _projected_source_worktree(stored_task),
        "base_commit": plan.base_commit,
        "candidate_commit": candidate_commit,
        "authoritative_files": list(stored_task["authoritative_files"]),
        "allowed_write_paths": write_paths,
        "forbidden_actions": forbidden,
        "risk_flags": list(stored_task["risk_flags"]),
        "acceptance_commands": commands,
        "verification_level": stored_task["verification_level"],
        "human_gates": ["FINAL_ACCEPTANCE", "XHIGH_APPROVAL"],
    }


def _final_event(replay: SchedulerReplay) -> dict[str, object] | None:
    for event in replay.events:
        if event.get("event_type") == "FINAL_ACCEPTANCE_OPENED":
            return event
    return None


def _child_task_bytes(store: object, task_id: str) -> bytes | None:
    require_task = getattr(store, "_task_dir", None)
    if not callable(require_task):
        _fail("PLAN_INVALID", "scheduler requires the workflow append-only store")
    path = Path(require_task(task_id)) / "task.json"
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _workflow_error(
            "SCHEDULER_LEDGER_INVALID", "acceptance child task file is unsafe"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_TASK_BYTES
        ):
            _fail("SCHEDULER_LEDGER_INVALID", "acceptance child task file is unsafe")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    except OSError as exc:
        raise _workflow_error("DISPATCH_IDENTITY_DRIFT", "cannot read stored acceptance task") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _parent_binding_document(
    plan: FrozenPlan,
    child_task: Mapping[str, object],
    final_event: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "scheduler-parent-1",
        "parent_task_id": plan.task_id,
        "parent_task_sha256": plan.task_sha256,
        "plan_sha256": plan.plan_sha256,
        "final_event_id": final_event["event_id"],
        "child_task_sha256": artifact_sha256(child_task),
        "candidate_commit": child_task["candidate_commit"],
    }


def _write_parent_binding(
    store: object,
    plan: FrozenPlan,
    child_task: Mapping[str, object],
    final_event: Mapping[str, object],
) -> None:
    path = Path(store._require_task(str(child_task["task_id"]))) / PARENT_BINDING_NAME
    document = _parent_binding_document(plan, child_task, final_event)
    expected = (_workflow()._canonical_json(document) + "\n").encode("utf-8")
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            _fail(
                "FINAL_ACCEPTANCE_PARENT_MISMATCH",
                "existing scheduler parent binding does not match",
            )
        return
    _workflow().write_json_once(
        path,
        document,
        conflict_code="FINAL_ACCEPTANCE_PARENT_MISMATCH",
    )


def verify_final_acceptance_child_binding(
    store: object, child_task: Mapping[str, object]
) -> bool:
    """Replay the unique parent scheduler plan that binds a final child."""

    child_id = child_task.get("task_id")
    if (
        child_task.get("task_type") != "ACCEPTANCE"
        or not isinstance(child_id, str)
        or "FINAL_ACCEPTANCE" not in child_task.get("human_gates", [])
    ):
        return False
    child_dir = Path(store._require_task(child_id))
    binding_path = child_dir / PARENT_BINDING_NAME
    if not binding_path.exists():
        return False
    if binding_path.is_symlink() or not binding_path.is_file():
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler parent binding is unsafe")
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _workflow_error(
            "SCHEDULER_LEDGER_INVALID", "cannot read scheduler parent binding"
        ) from exc
    expected_fields = {
        "schema_version",
        "parent_task_id",
        "parent_task_sha256",
        "plan_sha256",
        "final_event_id",
        "child_task_sha256",
        "candidate_commit",
    }
    if not isinstance(binding, Mapping) or set(binding) != expected_fields:
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler parent binding is invalid")
    parent_id = binding.get("parent_task_id")
    if not isinstance(parent_id, str) or parent_id == child_id:
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler parent task id is invalid")
    parent_dir = Path(store._require_task(parent_id))
    plan_path = parent_dir / PLAN_NAME
    if plan_path.is_symlink() or not plan_path.is_file():
        _fail("SCHEDULER_LEDGER_INVALID", "bound scheduler plan is unsafe")
    try:
        parent = json.loads((parent_dir / "task.json").read_text(encoding="utf-8"))
        plan_document = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _workflow_error(
            "SCHEDULER_LEDGER_INVALID", "cannot read bound scheduler parent artifacts"
        ) from exc
    if not isinstance(parent, Mapping) or not isinstance(plan_document, Mapping):
        _fail("SCHEDULER_LEDGER_INVALID", "bound scheduler parent is invalid")
    frozen = validate_plan(plan_document, parent)
    replay = replay_scheduler(store, frozen)
    event = _final_event(replay)
    if event is None:
        _fail("SCHEDULER_LEDGER_INVALID", "bound scheduler final event is missing")
    if (
        binding.get("schema_version") != "scheduler-parent-1"
        or binding.get("parent_task_sha256") != frozen.task_sha256
        or binding.get("plan_sha256") != frozen.plan_sha256
        or binding.get("final_event_id") != event.get("event_id")
        or binding.get("child_task_sha256") != artifact_sha256(child_task)
        or binding.get("candidate_commit") != child_task.get("candidate_commit")
        or replay.final_acceptance_task_id != child_id
        or replay.final_acceptance_task_sha256 != artifact_sha256(child_task)
        or event.get("candidate_commit") != child_task.get("candidate_commit")
    ):
        _fail("SCHEDULER_LEDGER_INVALID", "scheduler parent binding identity drifted")
    return True


def _assert_ready_for_final(plan: FrozenPlan, replay: SchedulerReplay) -> None:
    missing = sorted({task.id for task in plan.tasks} - replay.completed)
    if missing or replay.in_flight:
        _fail("FINAL_ACCEPTANCE_NOT_READY", "not every planned step has an IMPLEMENTED_CANDIDATE receipt")


def create_final_acceptance_case(
    store: object,
    plan: FrozenPlan,
    acceptance_task_id: str,
    candidate_commit: str,
) -> dict[str, object]:
    """Create the unique ACCEPTANCE child and bind it with FINAL_ACCEPTANCE_OPENED."""

    plan, stored_task = _revalidate_plan(store, plan)
    identifier = _acceptance_task_id(acceptance_task_id)
    pinned = _candidate_commit(candidate_commit)
    projection = _projected_final_acceptance_task(plan, stored_task, identifier, pinned)
    expected_bytes = _task_document_bytes(projection)
    workflow = _workflow()
    workflow.validate_task(projection)
    lock = getattr(store, "lock", None)
    if not callable(lock):
        _fail("PLAN_INVALID", "scheduler requires the workflow parent lock")
    child_digest = artifact_sha256(projection)
    with lock(plan.task_id):
        plan = _ensure_plan_artifact(store, plan)
        replay = replay_scheduler(store, plan)
        child_bytes = _child_task_bytes(store, identifier)
        final_event = _final_event(replay)
        if final_event is not None:
            if (
                replay.final_acceptance_task_id == identifier
                and final_event.get("candidate_commit") == pinned
                and final_event.get("acceptance_task_sha256") == child_digest
                and child_bytes == expected_bytes
            ):
                _assert_final_candidate_binding(stored_task, plan, pinned)
                _write_parent_binding(store, plan, projection, final_event)
                return dict(projection)
            _fail("FINAL_ACCEPTANCE_ALREADY_OPEN", "final acceptance binding does not match this call")
        if child_bytes is not None and child_bytes != expected_bytes:
            _fail("FINAL_ACCEPTANCE_CHILD_MISMATCH", "existing acceptance child does not match the projection")
        _assert_ready_for_final(plan, replay)
        _assert_final_candidate_binding(stored_task, plan, pinned)
        if child_bytes is None:
            store.create_task(dict(projection))
        with lock(identifier):
            ensure_ownership_registry_for_paths_locked(
                store,
                identifier,
                path_owners={
                    path: "sol_medium_reviewer"
                    for path in projection["allowed_write_paths"]
                },
            )
        _assert_final_candidate_binding(stored_task, plan, pinned)
        _ensure_opened(store, plan, replay)
        final_event = _append_event(
            store,
            plan,
            replay,
            "FINAL_ACCEPTANCE_OPENED",
            {
                "acceptance_task_id": identifier,
                "candidate_commit": pinned,
                "acceptance_task_sha256": child_digest,
            },
        )
        _write_parent_binding(store, plan, projection, final_event)
        return dict(projection)


def issue_final_acceptance(
    store: object,
    parent_plan: FrozenPlan,
    acceptance_task_id: str,
    owner_receipt: object,
    acceptor_actor: object,
):
    """Open the bound child once and issue the single Sol-medium REVIEW_1 assignment."""

    plan, _stored = _revalidate_plan(store, parent_plan)
    identifier = _acceptance_task_id(acceptance_task_id)
    module = _repairs()
    if not isinstance(acceptor_actor, module.ActorIdentity) or acceptor_actor.role != module._SOL_MEDIUM_REVIEWER:
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptor must be sol_medium_reviewer")
    module._final_acceptance_rework_policy()
    lock = getattr(store, "lock", None)
    if not callable(lock):
        _fail("PLAN_INVALID", "scheduler requires the workflow parent lock")
    with lock(plan.task_id):
        replay = replay_scheduler(store, plan)
        final_event = _final_event(replay)
        if replay.final_acceptance_task_id != identifier or final_event is None:
            _fail("FINAL_ACCEPTANCE_NOT_OPEN", "parent scheduler has no matching final acceptance")
        child = _workflow().load_task(store._require_task(identifier) / "task.json")
        if (
            child.get("task_id") != identifier
            or child.get("task_type") != "ACCEPTANCE"
            or child.get("candidate_commit") != final_event.get("candidate_commit")
            or artifact_sha256(child) != final_event.get("acceptance_task_sha256")
        ):
            _fail("FINAL_ACCEPTANCE_CHILD_MISMATCH", "child task is not the bound ACCEPTANCE case")
        module._v2_validate_observed_receipt(owner_receipt, child)
        module._v2_verified_runtime_receipt(
            store, identifier, owner_receipt, child, expected_attempt_id=None
        )
        child_replay = module.replay_acceptance_ledger(store, identifier)
        if child_replay is None:
            module.open_task_acceptance(store, child, owner_receipt)
            child_replay = module.replay_acceptance_ledger(store, identifier)
        if child_replay is None or not child_replay.whole_project_final:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "child acceptance ledger is not a whole-project final")
        review_ones = [item for item in child_replay.assignments.values() if item.phase == "REVIEW_1"]
        if review_ones:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "REVIEW_1 has already been issued")
        if child_replay.active_assignment_id is not None:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "child acceptance already has an active assignment")
        return module.issue_acceptance_assignment(store, identifier, "REVIEW_1", acceptor_actor)
