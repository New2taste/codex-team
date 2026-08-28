"""Immutable, append-only repair assignments for the Terra OS workflow.

This module deliberately owns only the repair ledger.  It does not widen any
role's normal repository permissions or alter the versioned workflow wires.
The workflow module imports these helpers after it has defined its store and
error type; lazy access below avoids an import cycle for direct script use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from .ai_workflow import WorkflowStore

try:
    from .ai_workflow_side_effects import (
        capture_fs_snapshot,
        observation_exclusions,
        observe_execution_side_effects,
        record_unobserved_side_effect,
    )
except ImportError:  # direct script execution
    from ai_workflow_side_effects import (
        capture_fs_snapshot,
        observation_exclusions,
        observe_execution_side_effects,
        record_unobserved_side_effect,
    )


_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_ASSIGNMENT_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TERRA_XHIGH = "terra_xhigh"
_SOL_MEDIUM_REVIEWER = "sol_medium_reviewer"
_REPAIR_EVENT_TYPES = frozenset(
    {
        "REPAIR_ASSIGNED",
        "REPAIR_COMPLETED",
        "REPAIR_REVIEWED",
        "SOL_REPAIR_AUTHORIZED",
        "REPAIR_BUDGET_EXHAUSTED",
    }
)
_ACCEPTANCE_VERDICTS = frozenset(
    {"ACCEPTANCE_RECOMMENDED", "ACCEPTANCE_WITH_NOTES_RECOMMENDED"}
)
_REWORK_VERDICTS = frozenset({"REWORK_RECOMMENDED", "REJECT_RECOMMENDED"})
_REVIEW_VERDICTS = _ACCEPTANCE_VERDICTS | _REWORK_VERDICTS


# ``repair-ledger-1`` above remains a read-only compatibility surface.  New
# adversarial acceptance never extends that protocol: v2 has a separate,
# self-authenticating event stream so that a generic workflow runner cannot
# silently turn a review finding into a write-capable assignment.
_ACCEPTANCE_LEDGER_VERSION = "adversarial-acceptance-1"
_ACCEPTANCE_EVENT_TYPES = frozenset(
    {
        "ACCEPTANCE_OPENED",
        "ASSIGNMENT_ISSUED",
        "ASSIGNMENT_ATTEMPT_STARTED",
        "ASSIGNMENT_ATTEMPT_FAILED",
        "REPAIR_COMPLETED",
        "REVIEW_COMPLETED",
    }
)
_ACCEPTANCE_PHASES = frozenset(
    {
        "REVIEW_1",
        "OWNER_REPAIR",
        "REVIEW_2",
        "SOL_MEDIUM_REPAIR",
        "SOL_MEDIUM_PEER_REVIEW",
        "SOL_XHIGH_TERMINAL_REPAIR",
    }
)
_REPAIR_PHASES = frozenset(
    {"OWNER_REPAIR", "SOL_MEDIUM_REPAIR", "SOL_XHIGH_TERMINAL_REPAIR"}
)
_REVIEW_PHASES = _ACCEPTANCE_PHASES - _REPAIR_PHASES
_V2_COMMON_FIELDS = frozenset(
    {
        "ledger_version",
        "event_type",
        "event_index",
        "event_id",
        "previous_event_id",
        "timestamp_utc",
        "task_id",
        "task_sha256",
        "base_commit",
        "candidate_commit",
    }
)
_FINAL_XHIGH_DECISION = "authorize_final_xhigh"
_FINAL_XHIGH_STATE = "FINAL_XHIGH_AUTHORIZED"
_FINAL_XHIGH_TICKET_FIELDS = frozenset(
    {
        "event_type",
        "decision",
        "actor",
        "timestamp_utc",
        "previous_state",
        "new_state",
        "task_sha256",
        "candidate_commit",
        "acceptance_event_id",
    }
)
_FROZEN_FINAL_ACCEPTANCE_REWORK = {
    "fixer_role": "sol_medium_reviewer",
    "fixer_permission_profile": "assignment-scoped-write",
    "fixer_distinct_from_acceptor": True,
    "recheck_role": "sol_medium_reviewer",
    "recheck_distinct_from_fixer": True,
    "terminal_escalation_role": "sol_xhigh",
    "terminal_review_required": False,
}
REPAIR_PROMPT_COMPACT_POLICY = "full_only"
_MAX_EVIDENCE_TRANSCRIPT_BYTES = 64 * 1024
_SAFE_EXECUTABLE_PATH = os.defpath
_SAFE_COMMAND_TEXT_PATTERN = re.compile(r"^[A-Za-z0-9_.= -]+$")
_UNITTEST_TARGET_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)


def _workflow():
    try:
        from . import ai_workflow as workflow
    except (ImportError, ModuleNotFoundError):
        import ai_workflow as workflow
    return workflow


def _fail(code: str, message: str) -> None:
    raise _workflow().WorkflowError(code, message)


def _v1_protocol_disabled() -> None:
    """Keep v1 names importable while rejecting all new v1 ledger writes."""

    _fail(
        "REPAIR_PROTOCOL_V1_DISABLED",
        "repair-ledger-1 is replay-only; adversarial-acceptance-1 is required",
    )


def _nonempty(value: object, field: str, *, code: str = "REPAIR_INPUT_INVALID") -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _fail(code, f"{field} must be a non-empty trimmed string")
    return value


def _path(value: object, field: str) -> str:
    candidate = _nonempty(value, field)
    if "\\" in candidate or candidate.startswith("/"):
        _fail("REPAIR_INPUT_INVALID", f"{field} must be a normalized relative path")
    normalized = PurePosixPath(candidate).as_posix()
    if normalized != candidate or normalized in {".", ".."} or ".." in normalized.split("/"):
        _fail("REPAIR_INPUT_INVALID", f"{field} must be a normalized relative path")
    return normalized


def _tuple(value: object, field: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)):
        _fail("REPAIR_INPUT_INVALID", f"{field} must be an iterable, not text")
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError:
        _fail("REPAIR_INPUT_INVALID", f"{field} must be iterable")
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class RepairFinding:
    """One stable finding ID and its normalized, permitted repair scope."""

    finding_id: str
    allowed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        finding_id = _nonempty(self.finding_id, "finding_id")
        raw_paths = _tuple(self.allowed_paths, "allowed_paths")
        if not raw_paths:
            _fail("REPAIR_INPUT_INVALID", "allowed_paths must not be empty")
        paths = tuple(sorted(_path(path, "allowed_paths") for path in raw_paths))
        if len(set(paths)) != len(paths):
            _fail("REPAIR_INPUT_INVALID", "allowed_paths must be unique")
        object.__setattr__(self, "finding_id", finding_id)
        object.__setattr__(self, "allowed_paths", paths)


@dataclass(frozen=True)
class ActorIdentity:
    """A role pin plus the immutable local identity assigned to that role."""

    identity: str
    role: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "identity", _nonempty(self.identity, "actor identity"))
        object.__setattr__(self, "role", _nonempty(self.role, "actor role"))


@dataclass(frozen=True)
class RepairAssignment:
    """The canonical, immutable assignment used by all repair ledger events."""

    assignment_id: str
    repair_round: int
    fixer_identity: ActorIdentity
    reviewer_identity: ActorIdentity
    peer_reviewer_identity: ActorIdentity | None
    findings: tuple[RepairFinding, ...]

    def __post_init__(self) -> None:
        findings = _findings(self.findings)
        object.__setattr__(self, "findings", findings)
        _assignment_fields(
            self.repair_round,
            self.fixer_identity,
            self.reviewer_identity,
            self.peer_reviewer_identity,
            findings,
        )
        expected = _assignment_id(
            self.repair_round,
            self.fixer_identity,
            self.reviewer_identity,
            self.peer_reviewer_identity,
            findings,
        )
        if self.assignment_id != expected:
            _fail("REPAIR_INPUT_INVALID", "assignment_id does not match canonical assignment fields")


def _require_actor(value: object, field: str) -> ActorIdentity:
    if not isinstance(value, ActorIdentity):
        _fail("REPAIR_INPUT_INVALID", f"{field} must be an ActorIdentity")
    return value


def _require_medium_reviewer(value: object, field: str) -> ActorIdentity:
    actor = _require_actor(value, field)
    if actor.role != _SOL_MEDIUM_REVIEWER:
        _fail("REPAIR_ACTOR_MISMATCH", f"{field} must have role {_SOL_MEDIUM_REVIEWER}")
    return actor


def _findings(value: object) -> tuple[RepairFinding, ...]:
    raw_findings = _tuple(value, "open_findings")
    if not raw_findings:
        _fail("REPAIR_INPUT_INVALID", "open_findings must not be empty")
    if any(not isinstance(finding, RepairFinding) for finding in raw_findings):
        _fail("REPAIR_INPUT_INVALID", "open_findings must contain RepairFinding values")
    findings = tuple(sorted(raw_findings, key=lambda finding: finding.finding_id))
    ids = tuple(finding.finding_id for finding in findings)
    if len(set(ids)) != len(ids):
        _fail("REPAIR_INPUT_INVALID", "finding IDs must be unique")
    return findings


def _flatten_allowed_paths(findings: tuple[RepairFinding, ...]) -> tuple[str, ...]:
    paths = tuple(sorted(path for finding in findings for path in finding.allowed_paths))
    if len(set(paths)) != len(paths):
        _fail("REPAIR_INPUT_INVALID", "finding scopes must not overlap exactly")
    return paths


def _assignment_fields(
    repair_round: object,
    fixer_identity: object,
    reviewer_identity: object,
    peer_reviewer_identity: object,
    findings: object,
) -> dict[str, object]:
    if not isinstance(repair_round, int) or isinstance(repair_round, bool) or repair_round < 1:
        _fail("REPAIR_INPUT_INVALID", "repair_round must be a positive integer")
    if repair_round > 3:
        _fail("REPAIR_BUDGET_EXHAUSTED", "the Terra OS repair budget is exhausted")
    fixer = _require_actor(fixer_identity, "fixer_identity")
    reviewer = _require_medium_reviewer(reviewer_identity, "reviewer_identity")
    peer = None if peer_reviewer_identity is None else _require_medium_reviewer(
        peer_reviewer_identity, "peer_reviewer_identity"
    )
    frozen_findings = _findings(findings)
    if fixer.identity == reviewer.identity:
        _fail("REPAIR_REVIEWER_CONFLICT", "a fixer must not review its own repair")
    if repair_round in {1, 2}:
        if fixer != ActorIdentity(_TERRA_XHIGH, _TERRA_XHIGH) or peer is not None:
            _fail("REPAIR_ACTOR_MISMATCH", "rounds 1 and 2 require fixed Terra xhigh and no peer")
    else:
        if fixer.role != _SOL_MEDIUM_REVIEWER or peer is None or reviewer != peer:
            _fail(
                "REPAIR_REVIEWER_CONFLICT",
                "round 3 requires the original Sol fixer and one distinct Sol medium peer",
            )
        if peer.identity == fixer.identity:
            _fail("REPAIR_REVIEWER_CONFLICT", "round 3 peer must differ from the fixer")
    return {
        "repair_round": repair_round,
        "fixer_identity": {"identity": fixer.identity, "role": fixer.role},
        "reviewer_identity": {"identity": reviewer.identity, "role": reviewer.role},
        "peer_reviewer_identity": (
            None if peer is None else {"identity": peer.identity, "role": peer.role}
        ),
        "finding_scopes": [
            {"finding_id": finding.finding_id, "allowed_paths": list(finding.allowed_paths)}
            for finding in frozen_findings
        ],
        "finding_ids": [finding.finding_id for finding in frozen_findings],
        "allowed_paths": list(_flatten_allowed_paths(frozen_findings)),
    }


def _assignment_id(
    repair_round: int,
    fixer_identity: ActorIdentity,
    reviewer_identity: ActorIdentity,
    peer_reviewer_identity: ActorIdentity | None,
    findings: tuple[RepairFinding, ...],
) -> str:
    fields = _assignment_fields(
        repair_round, fixer_identity, reviewer_identity, peer_reviewer_identity, findings
    )
    encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assign_repair(
    open_findings: Iterable[RepairFinding],
    round_number: int,
    original_reviewer: ActorIdentity,
    peer_reviewer: ActorIdentity | None,
) -> RepairAssignment:
    """Create one immutable repair assignment without reading mutable state."""

    _v1_protocol_disabled()

    original = _require_medium_reviewer(original_reviewer, "original_reviewer")
    findings = _findings(open_findings)
    if round_number in {1, 2}:
        if peer_reviewer is not None:
            _fail("REPAIR_INPUT_INVALID", "Terra rounds do not take a peer reviewer")
        fixer = ActorIdentity(_TERRA_XHIGH, _TERRA_XHIGH)
        reviewer = original
        peer = None
    elif round_number == 3:
        peer = _require_medium_reviewer(peer_reviewer, "peer_reviewer")
        fixer = original
        reviewer = peer
    elif not isinstance(round_number, int) or isinstance(round_number, bool) or round_number < 1:
        _fail("REPAIR_INPUT_INVALID", "round_number must be a positive integer")
    else:
        _fail("REPAIR_BUDGET_EXHAUSTED", "the Terra OS repair budget is exhausted")
    assignment_id = _assignment_id(round_number, fixer, reviewer, peer, findings)
    return RepairAssignment(assignment_id, round_number, fixer, reviewer, peer, findings)


@dataclass(frozen=True)
class _TaskContext:
    base_commit: str
    candidate_commit: str
    task_sha256: str
    allowed_write_paths: tuple[str, ...]


def _is_whole_project_final(
    task: Mapping[str, object], *, store: WorkflowStore | None = None
) -> bool:
    gates = task.get("human_gates")
    if not (
        task.get("task_type") == "ACCEPTANCE"
        and isinstance(gates, list)
        and "FINAL_ACCEPTANCE" in gates
        and store is not None
    ):
        return False
    try:
        from .ai_workflow_scheduler import verify_final_acceptance_child_binding
    except ImportError:
        from ai_workflow_scheduler import verify_final_acceptance_child_binding
    return verify_final_acceptance_child_binding(store, task)


def _frozen_task_context(store: WorkflowStore, task_id: str, task: Mapping[str, object]) -> _TaskContext:
    workflow = _workflow()
    base = task.get("base_commit")
    candidate = task.get("candidate_commit")
    if (
        not isinstance(base, str)
        or not isinstance(candidate, str)
        or not _SHA_PATTERN.fullmatch(base)
        or not _SHA_PATTERN.fullmatch(candidate)
    ):
        _fail("REPAIR_COMMIT_MISSING", "stored task must contain canonical base and candidate commits")
    allowed_write_paths = task.get("allowed_write_paths")
    if not isinstance(allowed_write_paths, list) or any(
        not isinstance(path, str) for path in allowed_write_paths
    ):
        _fail("REPAIR_INPUT_INVALID", "stored task allowed_write_paths is invalid")
    return _TaskContext(
        base,
        candidate,
        workflow._task_sha256(store, task_id),
        tuple(allowed_write_paths),
    )


def _task_context(store: WorkflowStore, task_id: str) -> _TaskContext:
    workflow = _workflow()
    task = workflow.load_task(store._require_task(task_id) / "task.json")
    if task.get("task_type") != "REMEDIATION":
        _fail("REPAIR_INPUT_INVALID", "repairs require a REMEDIATION task")
    return _frozen_task_context(store, task_id, task)


def _common_event_fields(assignment: RepairAssignment, context: _TaskContext) -> dict[str, object]:
    fields = _assignment_fields(
        assignment.repair_round,
        assignment.fixer_identity,
        assignment.reviewer_identity,
        assignment.peer_reviewer_identity,
        assignment.findings,
    )
    fields.update(
        {
            "assignment_id": assignment.assignment_id,
            "fixer_identity": assignment.fixer_identity.identity,
            "reviewer_identity": assignment.reviewer_identity.identity,
            "peer_reviewer_identity": (
                None
                if assignment.peer_reviewer_identity is None
                else assignment.peer_reviewer_identity.identity
            ),
            "base_commit": context.base_commit,
            "candidate_commit": context.candidate_commit,
            "task_sha256": context.task_sha256,
        }
    )
    return fields


def _event_timestamp() -> str:
    return _workflow()._utc_timestamp()


def _record_value(record: Mapping[str, object], field: str, *, allow_none: bool = False) -> str | None:
    value = record.get(field)
    if allow_none and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail("REPAIR_SEQUENCE_INVALID", f"repair ledger {field} is invalid")
    return value


def _event_string_list(record: Mapping[str, object], field: str) -> tuple[str, ...]:
    value = record.get(field)
    if not isinstance(value, list) or not value:
        _fail("REPAIR_SEQUENCE_INVALID", f"repair ledger {field} is invalid")
    values = tuple(value)
    if any(not isinstance(item, str) or not item for item in values) or len(set(values)) != len(values):
        _fail("REPAIR_SEQUENCE_INVALID", f"repair ledger {field} is invalid")
    return values


def _validate_event_context(record: Mapping[str, object], context: _TaskContext) -> None:
    if (
        record.get("base_commit") != context.base_commit
        or record.get("candidate_commit") != context.candidate_commit
        or record.get("task_sha256") != context.task_sha256
    ):
        _fail("REPAIR_COMMIT_DRIFT", "repair ledger no longer matches the stored task commits")


def _event_assignment(record: Mapping[str, object], context: _TaskContext) -> dict[str, object]:
    _validate_event_context(record, context)
    assignment_id = _record_value(record, "assignment_id")
    if not isinstance(assignment_id, str) or not _ASSIGNMENT_ID_PATTERN.fullmatch(assignment_id):
        _fail("REPAIR_SEQUENCE_INVALID", "repair ledger assignment_id is invalid")
    repair_round = record.get("repair_round")
    if not isinstance(repair_round, int) or isinstance(repair_round, bool) or repair_round not in {1, 2, 3}:
        _fail("REPAIR_SEQUENCE_INVALID", "repair ledger repair_round is invalid")
    fixer = _record_value(record, "fixer_identity")
    reviewer = _record_value(record, "reviewer_identity")
    peer = _record_value(record, "peer_reviewer_identity", allow_none=True)
    finding_ids = _event_string_list(record, "finding_ids")
    allowed_paths = _event_string_list(record, "allowed_paths")
    raw_scopes = record.get("finding_scopes")
    if not isinstance(raw_scopes, list) or len(raw_scopes) != len(finding_ids):
        _fail("REPAIR_SEQUENCE_INVALID", "repair ledger finding_scopes is invalid")
    finding_scopes: list[tuple[str, tuple[str, ...]]] = []
    for scope in raw_scopes:
        if not isinstance(scope, Mapping) or set(scope) != {"finding_id", "allowed_paths"}:
            _fail("REPAIR_SEQUENCE_INVALID", "repair ledger finding_scopes is invalid")
        scope_id = scope.get("finding_id")
        scope_paths = scope.get("allowed_paths")
        if not isinstance(scope_id, str) or not scope_id.strip() or not isinstance(scope_paths, list):
            _fail("REPAIR_SEQUENCE_INVALID", "repair ledger finding_scopes is invalid")
        scope_tuple = tuple(scope_paths)
        if (
            not scope_tuple
            or any(not isinstance(path, str) or not path for path in scope_tuple)
            or tuple(sorted(scope_tuple)) != scope_tuple
            or len(set(scope_tuple)) != len(scope_tuple)
        ):
            _fail("REPAIR_SEQUENCE_INVALID", "repair ledger finding_scopes is invalid")
        if tuple(_path(path, "allowed_paths") for path in scope_tuple) != scope_tuple:
            _fail("REPAIR_SEQUENCE_INVALID", "repair ledger finding_scopes is invalid")
        finding_scopes.append((scope_id, scope_tuple))
    if tuple(sorted(finding_ids)) != finding_ids or tuple(sorted(allowed_paths)) != allowed_paths:
        _fail("REPAIR_SEQUENCE_INVALID", "repair ledger canonical fields are not sorted")
    if tuple(scope[0] for scope in finding_scopes) != finding_ids:
        _fail("REPAIR_SEQUENCE_INVALID", "repair ledger finding_scopes do not match finding_ids")
    if tuple(sorted(path for _, paths in finding_scopes for path in paths)) != allowed_paths:
        _fail("REPAIR_SEQUENCE_INVALID", "repair ledger finding_scopes do not match allowed_paths")
    if tuple(_path(path, "allowed_paths") for path in allowed_paths) != allowed_paths:
        _fail("REPAIR_SEQUENCE_INVALID", "repair ledger allowed_paths are invalid")
    if repair_round in {1, 2}:
        if fixer != _TERRA_XHIGH or peer is not None:
            _fail("REPAIR_SEQUENCE_INVALID", "repair ledger Terra assignment is invalid")
    elif fixer == reviewer or peer != reviewer:
        _fail("REPAIR_SEQUENCE_INVALID", "repair ledger Sol fallback assignment is invalid")
    fixer_role = _TERRA_XHIGH if repair_round in {1, 2} else _SOL_MEDIUM_REVIEWER
    canonical_fields = {
        "repair_round": repair_round,
        "fixer_identity": {"identity": fixer, "role": fixer_role},
        "reviewer_identity": {"identity": reviewer, "role": _SOL_MEDIUM_REVIEWER},
        "peer_reviewer_identity": (
            None if peer is None else {"identity": peer, "role": _SOL_MEDIUM_REVIEWER}
        ),
        "finding_scopes": [
            {"finding_id": scope_id, "allowed_paths": list(scope_paths)}
            for scope_id, scope_paths in finding_scopes
        ],
        "finding_ids": list(finding_ids),
        "allowed_paths": list(allowed_paths),
    }
    canonical_id = hashlib.sha256(
        json.dumps(canonical_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if assignment_id != canonical_id:
        _fail("REPAIR_SEQUENCE_INVALID", "repair ledger assignment_id is not canonical")
    return {
        "assignment_id": assignment_id,
        "repair_round": repair_round,
        "fixer_identity": fixer,
        "reviewer_identity": reviewer,
        "peer_reviewer_identity": peer,
        "finding_scopes": tuple(finding_scopes),
        "finding_ids": finding_ids,
        "allowed_paths": allowed_paths,
    }


def _same_assignment(expected: Mapping[str, object], actual: Mapping[str, object]) -> bool:
    return all(expected.get(field) == actual.get(field) for field in expected)


@dataclass
class _ReplayState:
    assignments: dict[int, dict[str, object]]
    completed: set[str]
    reviews: dict[int, str]
    sol_authorized: bool = False
    blocked: bool = False


def _require_prior_rework(replay: _ReplayState, round_number: int) -> None:
    prior = round_number - 1
    if replay.reviews.get(prior) not in _REWORK_VERDICTS:
        _fail("REPAIR_SEQUENCE_INVALID", f"round {round_number} requires a rejected round {prior}")


def _validate_assignment_sequence(
    replay: _ReplayState, assignment_fields: Mapping[str, object]
) -> None:
    repair_round = assignment_fields["repair_round"]
    assert isinstance(repair_round, int)
    if replay.blocked:
        _fail("REPAIR_BUDGET_EXHAUSTED", "the repair protocol is already blocked")
    if repair_round in replay.assignments:
        _fail("REPAIR_REPLAY", "repair round already has an assignment")
    if any(
        fields["assignment_id"] not in replay.completed
        or fields["repair_round"] not in replay.reviews
        for fields in replay.assignments.values()
    ):
        _fail("REPAIR_SEQUENCE_INVALID", "a prior repair assignment remains open")
    if repair_round == 1:
        if replay.assignments:
            _fail("REPAIR_SEQUENCE_INVALID", "round 1 cannot follow another repair")
        return
    _require_prior_rework(replay, repair_round)
    previous = replay.assignments[repair_round - 1]
    finding_ids = assignment_fields["finding_ids"]
    allowed_paths = assignment_fields["allowed_paths"]
    finding_scopes = assignment_fields["finding_scopes"]
    assert (
        isinstance(finding_ids, tuple)
        and isinstance(allowed_paths, tuple)
        and isinstance(finding_scopes, tuple)
    )
    if not set(finding_ids).issubset(previous["finding_ids"]) or not set(allowed_paths).issubset(
        previous["allowed_paths"]
    ):
        _fail("REPAIR_FINDING_DRIFT", "later repair findings may only keep or reduce open scope")
    previous_scopes = dict(previous["finding_scopes"])
    if any(
        scope_id not in previous_scopes or not set(scope_paths).issubset(previous_scopes[scope_id])
        for scope_id, scope_paths in finding_scopes
    ):
        _fail("REPAIR_FINDING_DRIFT", "later repair finding IDs may not change scope")
    original = replay.assignments[1]["reviewer_identity"]
    if repair_round == 2:
        if assignment_fields["reviewer_identity"] != original:
            _fail("REPAIR_ACTOR_MISMATCH", "round 2 must retain the original reviewer")
    else:
        if not replay.sol_authorized:
            _fail("SOL_REPAIR_NOT_AUTHORIZED", "round 3 requires SOL_REPAIR_AUTHORIZED")
        if assignment_fields["fixer_identity"] != original:
            _fail("REPAIR_ACTOR_MISMATCH", "round 3 fixer must be the original reviewer")


def _replay(store: WorkflowStore, task_id: str, context: _TaskContext) -> _ReplayState:
    workflow = _workflow()
    replay = _ReplayState({}, set(), {})
    assignment_by_id: dict[str, dict[str, object]] = {}
    for record in workflow._load_event_records(store, task_id):
        event_type = record.get("event_type")
        if event_type not in _REPAIR_EVENT_TYPES:
            continue
        if not isinstance(event_type, str):
            _fail("REPAIR_SEQUENCE_INVALID", "repair ledger event_type is invalid")
        if event_type == "REPAIR_ASSIGNED":
            fields = _event_assignment(record, context)
            _validate_assignment_sequence(replay, fields)
            assignment_id = fields["assignment_id"]
            assert isinstance(assignment_id, str)
            if assignment_id in assignment_by_id:
                _fail("REPAIR_REPLAY", "repair assignment_id was replayed")
            replay.assignments[fields["repair_round"]] = fields  # type: ignore[index]
            assignment_by_id[assignment_id] = fields
        elif event_type == "REPAIR_COMPLETED":
            fields = _event_assignment(record, context)
            assignment_id = fields["assignment_id"]
            if assignment_id not in assignment_by_id or not _same_assignment(
                assignment_by_id[assignment_id], fields
            ):
                _fail("REPAIR_SEQUENCE_INVALID", "repair completion lacks its exact assignment")
            if assignment_id in replay.completed:
                _fail("REPAIR_REPLAY", "repair completion was replayed")
            actor = _record_value(record, "actor_identity")
            if actor != fields["fixer_identity"]:
                _fail("REPAIR_ACTOR_MISMATCH", "repair completion actor does not match the fixer")
            changed_paths = _event_string_list(record, "changed_paths")
            if not set(changed_paths).issubset(fields["allowed_paths"]):
                _fail("REPAIR_SCOPE_VIOLATION", "repair completion changed an out-of-scope path")
            replay.completed.add(assignment_id)
        elif event_type == "REPAIR_REVIEWED":
            fields = _event_assignment(record, context)
            assignment_id = fields["assignment_id"]
            if assignment_id not in assignment_by_id or not _same_assignment(
                assignment_by_id[assignment_id], fields
            ):
                _fail("REPAIR_SEQUENCE_INVALID", "repair review lacks its exact assignment")
            repair_round = fields["repair_round"]
            assert isinstance(repair_round, int)
            if assignment_id not in replay.completed or repair_round in replay.reviews:
                _fail("REPAIR_SEQUENCE_INVALID", "repair review must follow one completion")
            if _record_value(record, "reviewer_identity") != fields["reviewer_identity"]:
                _fail("REPAIR_ACTOR_MISMATCH", "repair review actor does not match the reviewer")
            verdict = _record_value(record, "verdict")
            if verdict not in _REVIEW_VERDICTS:
                _fail("REPAIR_SEQUENCE_INVALID", "repair review verdict is invalid")
            replay.reviews[repair_round] = verdict
        elif event_type == "SOL_REPAIR_AUTHORIZED":
            _validate_event_context(record, context)
            if replay.sol_authorized or replay.reviews.get(2) not in _REWORK_VERDICTS:
                _fail("REPAIR_SEQUENCE_INVALID", "Sol repair authorization is not applicable")
            original = _record_value(record, "original_reviewer_identity")
            if original != replay.assignments[1]["reviewer_identity"]:
                _fail("REPAIR_ACTOR_MISMATCH", "Sol repair authorization changed the original reviewer")
            replay.sol_authorized = True
        else:
            _validate_event_context(record, context)
            if (
                replay.reviews.get(3) not in _REWORK_VERDICTS
                or record.get("new_state") != "BLOCKED"
                or replay.blocked
            ):
                _fail("REPAIR_SEQUENCE_INVALID", "repair budget exhaustion is invalid")
            replay.blocked = True
    return replay


def _assignment_for_event(assignment: RepairAssignment, context: _TaskContext) -> dict[str, object]:
    return _common_event_fields(assignment, context)


def record_repair_assignment(
    store: WorkflowStore, task_id: str, assignment: RepairAssignment
) -> None:
    """Append one exact assignment only after a locked, complete-ledger replay."""

    _v1_protocol_disabled()

    if not isinstance(assignment, RepairAssignment):
        _fail("REPAIR_INPUT_INVALID", "assignment must be a RepairAssignment")
    with store.lock(task_id):
        context = _task_context(store, task_id)
        replay = _replay(store, task_id, context)
        fields = _assignment_for_event(assignment, context)
        sequence_fields = _event_assignment(fields, context)
        try:
            _workflow().assert_allowed_changes(
                set(sequence_fields["allowed_paths"]), context.allowed_write_paths
            )
        except _workflow().WorkflowError as exc:
            if exc.code == "OUT_OF_SCOPE_CHANGE":
                _fail("REPAIR_SCOPE_VIOLATION", "repair findings exceed the parent task write scope")
            raise
        _validate_assignment_sequence(replay, sequence_fields)
        event = {"event_type": "REPAIR_ASSIGNED", "timestamp_utc": _event_timestamp(), **fields}
        store.append_event(task_id, event)


def validate_repair_result(
    assignment: RepairAssignment,
    actor_identity: ActorIdentity,
    changed_paths: Iterable[str],
) -> tuple[str, ...]:
    """Verify exact fixer identity and the union of frozen finding scopes."""

    if not isinstance(assignment, RepairAssignment):
        _fail("REPAIR_INPUT_INVALID", "assignment must be a RepairAssignment")
    actor = _require_actor(actor_identity, "actor_identity")
    if actor != assignment.fixer_identity:
        _fail("REPAIR_ACTOR_MISMATCH", "repair actor does not match the assigned fixer")
    paths = tuple(sorted(_path(path, "changed_paths") for path in _tuple(changed_paths, "changed_paths")))
    if len(set(paths)) != len(paths):
        _fail("REPAIR_INPUT_INVALID", "changed_paths must be unique")
    allowed = set(_flatten_allowed_paths(assignment.findings))
    if not set(paths).issubset(allowed):
        _fail("REPAIR_SCOPE_VIOLATION", "repair result changed an out-of-scope path")
    return paths


def record_repair_completion(
    store: WorkflowStore,
    task_id: str,
    assignment: RepairAssignment,
    actor_identity: ActorIdentity,
    changed_paths: Iterable[str],
) -> None:
    """Append a single completed result bound to the stored assignment and commits."""

    _v1_protocol_disabled()

    normalized_paths = validate_repair_result(assignment, actor_identity, changed_paths)
    with store.lock(task_id):
        context = _task_context(store, task_id)
        replay = _replay(store, task_id, context)
        fields = _assignment_for_event(assignment, context)
        sequence_fields = _event_assignment(fields, context)
        assigned = next(
            (
                item
                for item in replay.assignments.values()
                if item["assignment_id"] == assignment.assignment_id
            ),
            None,
        )
        if assigned is None or not _same_assignment(assigned, sequence_fields):
            _fail("REPAIR_SEQUENCE_INVALID", "repair completion does not match an active assignment")
        if assignment.assignment_id in replay.completed:
            _fail("REPAIR_REPLAY", "repair assignment is already completed")
        event = {
            "event_type": "REPAIR_COMPLETED",
            "timestamp_utc": _event_timestamp(),
            "actor_identity": actor_identity.identity,
            "changed_paths": list(normalized_paths),
            **fields,
        }
        store.append_event(task_id, event)


def record_repair_review(
    store: WorkflowStore,
    task_id: str,
    assignment: RepairAssignment,
    reviewer_identity: ActorIdentity,
    verdict: str,
) -> None:
    """Record one closed-set peer review and hard-stop a third rework."""

    _v1_protocol_disabled()

    reviewer = _require_medium_reviewer(reviewer_identity, "reviewer_identity")
    if reviewer != assignment.reviewer_identity:
        _fail("REPAIR_ACTOR_MISMATCH", "reviewer does not match the assigned peer")
    if verdict not in _REVIEW_VERDICTS:
        _fail("REPAIR_INPUT_INVALID", "verdict is not in the repair review closed set")
    with store.lock(task_id):
        context = _task_context(store, task_id)
        replay = _replay(store, task_id, context)
        fields = _assignment_for_event(assignment, context)
        sequence_fields = _event_assignment(fields, context)
        assigned = replay.assignments.get(assignment.repair_round)
        if assigned is None or not _same_assignment(assigned, sequence_fields):
            _fail("REPAIR_SEQUENCE_INVALID", "repair review does not match an active assignment")
        if assignment.assignment_id not in replay.completed:
            _fail("REPAIR_SEQUENCE_INVALID", "repair review requires a completion")
        if assignment.repair_round in replay.reviews:
            _fail("REPAIR_REPLAY", "repair assignment is already reviewed")
        store.append_event(
            task_id,
            {
                "event_type": "REPAIR_REVIEWED",
                "timestamp_utc": _event_timestamp(),
                "verdict": verdict,
                **fields,
            },
        )
        if assignment.repair_round == 3 and verdict in _REWORK_VERDICTS:
            workflow = _workflow()
            store.append_event(
                task_id,
                {
                    "event_type": "REPAIR_BUDGET_EXHAUSTED",
                    "timestamp_utc": _event_timestamp(),
                    "assignment_id": assignment.assignment_id,
                    "repair_round": 3,
                    "base_commit": context.base_commit,
                    "candidate_commit": context.candidate_commit,
                    "task_sha256": context.task_sha256,
                    "previous_state": workflow._current_state(store, task_id),
                    "new_state": "BLOCKED",
                },
            )


def record_sol_repair_authorization(
    store: WorkflowStore, task_id: str, original_reviewer: ActorIdentity
) -> None:
    """Append the explicit authorization required before the Sol fallback assignment."""

    _v1_protocol_disabled()

    original = _require_medium_reviewer(original_reviewer, "original_reviewer")
    with store.lock(task_id):
        context = _task_context(store, task_id)
        replay = _replay(store, task_id, context)
        if replay.sol_authorized:
            _fail("REPAIR_REPLAY", "Sol repair is already authorized")
        if replay.reviews.get(2) not in _REWORK_VERDICTS:
            _fail("SOL_REPAIR_NOT_AUTHORIZED", "round 2 must be rejected before authorizing Sol repair")
        if original.identity != replay.assignments[1]["reviewer_identity"]:
            _fail("REPAIR_ACTOR_MISMATCH", "Sol repair authorization changed the original reviewer")
        store.append_event(
            task_id,
            {
                "event_type": "SOL_REPAIR_AUTHORIZED",
                "timestamp_utc": _event_timestamp(),
                "original_reviewer_identity": original.identity,
                "base_commit": context.base_commit,
                "candidate_commit": context.candidate_commit,
                "task_sha256": context.task_sha256,
            },
        )


def _v2_canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _v2_sha256(value: object) -> str:
    return hashlib.sha256(_v2_canonical(value).encode("utf-8")).hexdigest()


def _v2_sha(value: object, field: str, *, length: int = 64) -> str:
    text = _nonempty(value, field, code="ACCEPTANCE_LEDGER_INVALID")
    if not re.fullmatch(rf"[0-9a-f]{{{length}}}", text):
        _fail("ACCEPTANCE_LEDGER_INVALID", f"{field} must be a canonical SHA digest")
    return text


def _v2_actor_payload(actor: ActorIdentity) -> dict[str, str]:
    return {"identity": actor.identity, "role": actor.role}


def _v2_actor(value: object, field: str) -> ActorIdentity:
    if not isinstance(value, Mapping) or set(value) != {"identity", "role"}:
        _fail("ACCEPTANCE_LEDGER_INVALID", f"{field} must be a canonical actor identity")
    return ActorIdentity(value["identity"], value["role"])


def _v2_findings(value: object, field: str = "findings") -> tuple[RepairFinding, ...]:
    raw = _tuple(value, field)
    if any(not isinstance(item, RepairFinding) for item in raw):
        _fail("ACCEPTANCE_LEDGER_INVALID", f"{field} must contain RepairFinding values")
    findings = tuple(raw)
    ids = tuple(item.finding_id for item in findings)
    if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
        _fail("ACCEPTANCE_LEDGER_INVALID", f"{field} must have canonically sorted unique IDs")
    paths = tuple(path for item in findings for path in item.allowed_paths)
    if len(paths) != len(set(paths)):
        _fail("ACCEPTANCE_LEDGER_INVALID", f"{field} may not overlap path authority")
    return findings


def _v2_findings_payload(findings: tuple[RepairFinding, ...]) -> list[dict[str, object]]:
    return [
        {"finding_id": finding.finding_id, "allowed_paths": list(finding.allowed_paths)}
        for finding in findings
    ]


def _v2_findings_from_payload(value: object) -> tuple[RepairFinding, ...]:
    if not isinstance(value, list):
        _fail("ACCEPTANCE_LEDGER_INVALID", "findings must be a list")
    parsed: list[RepairFinding] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"finding_id", "allowed_paths"}:
            _fail("ACCEPTANCE_LEDGER_INVALID", "finding payload is invalid")
        paths = item["allowed_paths"]
        if not isinstance(paths, list):
            _fail("ACCEPTANCE_LEDGER_INVALID", "finding paths are invalid")
        parsed.append(RepairFinding(item["finding_id"], tuple(paths)))
    return _v2_findings(tuple(parsed))


def _v2_allowed_paths(findings: tuple[RepairFinding, ...]) -> tuple[str, ...]:
    return tuple(sorted(path for finding in findings for path in finding.allowed_paths))


@dataclass(frozen=True)
class VerifiedActorReceipt:
    """A runtime-verifiable identity receipt, never a role name alone."""

    assignment_id: str
    execution_surface: str
    runtime_instance_id: str
    attempt_id: str
    requested_role: str
    observed_model: str
    observed_reasoning_effort: str
    observed_sandbox_policy: str
    observed_permission_profile: str
    observed_cwd: str
    runtime_evidence_sha256: str
    native_agent_uuid: str | None
    codex_thread_id: str | None

    def __post_init__(self) -> None:
        _v2_sha(self.assignment_id, "receipt.assignment_id")
        surface = _nonempty(
            self.execution_surface, "receipt.execution_surface", code="ACCEPTANCE_LEDGER_INVALID"
        )
        if surface not in {"NATIVE_SUBAGENT", "CODEX_EXEC_ROLE_CONTRACT"}:
            _fail("ACCEPTANCE_LEDGER_INVALID", "receipt execution surface is unsupported")
        for field in (
            "runtime_instance_id",
            "attempt_id",
            "requested_role",
            "observed_model",
            "observed_reasoning_effort",
            "observed_sandbox_policy",
            "observed_permission_profile",
            "observed_cwd",
        ):
            _nonempty(getattr(self, field), f"receipt.{field}", code="ACCEPTANCE_LEDGER_INVALID")
        _v2_sha(self.runtime_evidence_sha256, "receipt.runtime_evidence_sha256")
        identity_sources = (self.native_agent_uuid, self.codex_thread_id)
        if sum(isinstance(value, str) and bool(value.strip()) for value in identity_sources) != 1:
            _fail("ACCEPTANCE_LEDGER_INVALID", "receipt must bind exactly one runtime identity source")

    @property
    def actor_identity(self) -> ActorIdentity:
        return ActorIdentity(
            f"{self.execution_surface}:{self.runtime_instance_id}", self.requested_role
        )


@dataclass(frozen=True)
class ControllerExecutionAttestation:
    """Persisted binding between an issued capability and observed execution.

    This value is audit data, not executable authority.  ``run_assignment``
    constructs it only after inspecting the issued runtime; callers cannot
    submit one to launch or complete an assignment.
    """

    task_id: str
    task_sha256: str
    assignment_id: str
    capability_id: str
    candidate_commit: str
    actor_receipt: VerifiedActorReceipt
    attestation_sha256: str = ""

    def __post_init__(self) -> None:
        _nonempty(self.task_id, "controller_attestation.task_id", code="ACCEPTANCE_LEDGER_INVALID")
        _v2_sha(self.task_sha256, "controller_attestation.task_sha256")
        _v2_sha(self.assignment_id, "controller_attestation.assignment_id")
        _v2_sha(self.capability_id, "controller_attestation.capability_id")
        _v2_sha(self.candidate_commit, "controller_attestation.candidate_commit", length=40)
        if not isinstance(self.actor_receipt, VerifiedActorReceipt):
            _fail("ACCEPTANCE_LEDGER_INVALID", "controller attestation lacks a runtime receipt")
        payload = asdict(self)
        payload.pop("attestation_sha256", None)
        expected = _v2_sha256(payload)
        if self.attestation_sha256 and self.attestation_sha256 != expected:
            _fail("ACCEPTANCE_LEDGER_INVALID", "controller attestation identity drifted")
        object.__setattr__(self, "attestation_sha256", expected)


class ControllerAssignmentBoundary:
    """Read-only compatibility marker for the superseded callback boundary.

    Public imports remain stable, but ``run_assignment`` rejects every instance
    before either method is called.  V2 execution now uses one fixed controller
    command and never accepts caller-provided executable behavior.
    """

    def attest_execution(self, capability: AssignmentCapability) -> ControllerExecutionAttestation:
        raise NotImplementedError

    def execute_capability(self, capability: AssignmentCapability) -> Mapping[str, object]:
        raise NotImplementedError


@dataclass(frozen=True)
class AssignmentCapability:
    """A hash-bound, assignment-scoped authority with no merge or push power."""

    capability_id: str
    task_id: str
    task_sha256: str
    assignment_id: str
    phase: str
    attempt_id: str
    base_commit: str
    input_candidate_commit: str
    finding_ids: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    write_authority: str
    issuing_event_id: str
    expected_actor: ActorIdentity

    def __post_init__(self) -> None:
        _nonempty(self.task_id, "capability.task_id", code="ACCEPTANCE_LEDGER_INVALID")
        _v2_sha(self.task_sha256, "capability.task_sha256")
        _v2_sha(self.assignment_id, "capability.assignment_id")
        _nonempty(self.attempt_id, "capability.attempt_id", code="ACCEPTANCE_LEDGER_INVALID")
        _v2_sha(self.base_commit, "capability.base_commit", length=40)
        _v2_sha(self.input_candidate_commit, "capability.input_candidate_commit", length=40)
        _v2_sha(self.issuing_event_id, "capability.issuing_event_id")
        if self.phase not in _ACCEPTANCE_PHASES:
            _fail("ACCEPTANCE_LEDGER_INVALID", "capability phase is invalid")
        if tuple(self.finding_ids) != tuple(sorted(self.finding_ids)) or len(set(self.finding_ids)) != len(self.finding_ids):
            _fail("ACCEPTANCE_LEDGER_INVALID", "capability finding IDs are not canonical")
        paths = tuple(_path(path, "capability.allowed_paths") for path in self.allowed_paths)
        if paths != tuple(self.allowed_paths) or paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            _fail("ACCEPTANCE_LEDGER_INVALID", "capability paths are not canonical")
        forbidden = tuple(self.forbidden_actions)
        if forbidden != tuple(sorted(forbidden)) or len(set(forbidden)) != len(forbidden):
            _fail("ACCEPTANCE_LEDGER_INVALID", "capability forbidden actions are not canonical")
        if "merge" not in forbidden or "push" not in forbidden:
            _fail("ACCEPTANCE_LEDGER_INVALID", "capability must explicitly forbid merge and push")
        expected = _v2_capability_id_payload(self)
        if self.capability_id != _v2_sha256(expected):
            _fail("ACCEPTANCE_LEDGER_INVALID", "capability_id is not bound to its authority")


def _v2_capability_id_payload(capability: AssignmentCapability) -> dict[str, object]:
    value = asdict(capability)
    value.pop("capability_id", None)
    return value


@dataclass(frozen=True)
class AcceptanceAssignment:
    assignment_id: str
    task_id: str
    phase: str
    attempt_id: str
    expected_actor: ActorIdentity
    base_commit: str
    input_candidate_commit: str
    findings: tuple[RepairFinding, ...]
    allowed_paths: tuple[str, ...]
    capability: AssignmentCapability

    def __post_init__(self) -> None:
        _v2_sha(self.assignment_id, "assignment_id")
        _nonempty(self.task_id, "assignment.task_id", code="ACCEPTANCE_LEDGER_INVALID")
        if self.phase not in _ACCEPTANCE_PHASES:
            _fail("ACCEPTANCE_LEDGER_INVALID", "assignment phase is invalid")
        _nonempty(self.attempt_id, "assignment.attempt_id", code="ACCEPTANCE_LEDGER_INVALID")
        _v2_sha(self.base_commit, "assignment.base_commit", length=40)
        _v2_sha(self.input_candidate_commit, "assignment.input_candidate_commit", length=40)
        findings = _v2_findings(self.findings)
        object.__setattr__(self, "findings", findings)
        paths = _v2_allowed_paths(findings)
        if tuple(self.allowed_paths) != paths:
            _fail("ACCEPTANCE_LEDGER_INVALID", "assignment paths are not bound to findings")
        if self.capability.assignment_id != self.assignment_id or self.capability.phase != self.phase:
            _fail("ACCEPTANCE_LEDGER_INVALID", "assignment capability does not match assignment")


@dataclass(frozen=True)
class AdversarialEvidence:
    """Controller-produced review evidence bound to the frozen task snapshot.

    The first three fields remain readable for v2 event compatibility.  The
    receipts below are mandatory only for an evidence value that reaches the
    ledger: ``record_adversarial_review`` independently re-executes the
    controller checks and requires exact equality with that result.
    """

    verification_commands: tuple[str, ...]
    negative_checks: tuple[str, ...]
    outputs: tuple[str, ...]
    verification_exit_codes: tuple[int, ...] = ()
    verification_output_sha256: tuple[str, ...] = ()
    artifact_sha256: tuple[str, ...] = ()
    negative_exit_codes: tuple[int, ...] = ()
    negative_output_sha256: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("verification_commands", "negative_checks", "outputs"):
            values = _tuple(getattr(self, field), field)
            if not values or any(not isinstance(value, str) or not value.strip() for value in values):
                _fail("ACCEPTANCE_EVIDENCE_INVALID", f"{field} must contain non-empty evidence")
            object.__setattr__(self, field, tuple(values))
        receipt_fields = (
            "verification_exit_codes",
            "verification_output_sha256",
            "artifact_sha256",
            "negative_exit_codes",
            "negative_output_sha256",
        )
        values = {field: _tuple(getattr(self, field), field) for field in receipt_fields}
        if not any(values.values()):
            return
        if not all(values.values()):
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller evidence receipts are incomplete")
        if (
            len(values["verification_exit_codes"]) != len(self.verification_commands)
            or len(values["verification_output_sha256"]) != len(self.outputs)
            or len(values["artifact_sha256"]) != len(self.verification_commands)
            or len(values["negative_exit_codes"]) != len(self.negative_checks)
            or len(values["negative_output_sha256"]) != len(self.negative_checks)
        ):
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller evidence receipt counts do not bind")
        if any(not isinstance(code, int) for code in values["verification_exit_codes"] + values["negative_exit_codes"]):
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller evidence exit codes are invalid")
        if any(code != 0 for code in values["verification_exit_codes"]):
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "verification command did not succeed")
        if any(code == 0 for code in values["negative_exit_codes"]):
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "negative mutation was not rejected")
        for field in ("verification_output_sha256", "artifact_sha256", "negative_output_sha256"):
            for digest in values[field]:
                _v2_sha(digest, field)


@dataclass
class _AcceptanceReplay:
    task_id: str
    task_sha256: str
    base_commit: str
    initial_candidate_commit: str
    current_candidate_commit: str
    owner_actor: ActorIdentity
    owner_receipt: VerifiedActorReceipt
    last_event_id: str
    event_count: int
    assignments: dict[str, AcceptanceAssignment]
    active_assignment_id: str | None
    terminal: bool
    phase_outcomes: dict[str, str]
    pending_findings: tuple[RepairFinding, ...]
    reviewer_identities: set[str]
    started_receipts: dict[str, VerifiedActorReceipt]
    started_attestations: dict[str, ControllerExecutionAttestation]
    finished_assignment_ids: set[str]
    repairer_identities: dict[str, str]
    whole_project_final: bool = False


def _v2_receipt_payload(receipt: VerifiedActorReceipt) -> dict[str, object]:
    return asdict(receipt)


def _v2_receipt(value: object, field: str) -> VerifiedActorReceipt:
    if not isinstance(value, Mapping):
        _fail("ACCEPTANCE_LEDGER_INVALID", f"{field} must be a receipt object")
    expected = set(VerifiedActorReceipt.__dataclass_fields__)
    if set(value) != expected:
        _fail("ACCEPTANCE_LEDGER_INVALID", f"{field} has unsupported receipt fields")
    try:
        return VerifiedActorReceipt(**dict(value))
    except (TypeError, RuntimeError):
        _fail("ACCEPTANCE_LEDGER_INVALID", f"{field} is not a valid receipt")
    raise AssertionError("unreachable")


def _v2_controller_attestation(
    value: object, field: str
) -> ControllerExecutionAttestation:
    if not isinstance(value, Mapping):
        _fail("ACCEPTANCE_LEDGER_INVALID", f"{field} must be a controller attestation")
    expected = set(ControllerExecutionAttestation.__dataclass_fields__)
    if set(value) != expected:
        _fail("ACCEPTANCE_LEDGER_INVALID", f"{field} has unsupported fields")
    payload = dict(value)
    payload["actor_receipt"] = _v2_receipt(payload.get("actor_receipt"), f"{field}.actor_receipt")
    try:
        return ControllerExecutionAttestation(**payload)
    except (TypeError, RuntimeError):
        _fail("ACCEPTANCE_LEDGER_INVALID", f"{field} is not valid")
    raise AssertionError("unreachable")


def _v2_event_records(store: WorkflowStore, task_id: str) -> list[dict[str, object]]:
    workflow = _workflow()
    records = workflow._load_event_records(store, task_id)
    v2 = [record for record in records if record.get("ledger_version") == _ACCEPTANCE_LEDGER_VERSION]
    if v2 and any(
        record.get("ledger_version") != _ACCEPTANCE_LEDGER_VERSION
        and record.get("event_type") in _REPAIR_EVENT_TYPES
        for record in records
    ):
        _fail("ACCEPTANCE_LEDGER_INVALID", "v1 repair history may not be upgraded into a v2 ledger")
    if v2 and any(
        "ledger_version" in record
        and record.get("ledger_version") != _ACCEPTANCE_LEDGER_VERSION
        for record in records
    ):
        _fail("ACCEPTANCE_LEDGER_INVALID", "unknown acceptance ledger version")
    return v2


def _v2_context(store: WorkflowStore, task_id: str) -> _TaskContext:
    workflow = _workflow()
    task = workflow.load_task(store._require_task(task_id) / "task.json")
    if task.get("task_type") == "REMEDIATION":
        return _task_context(store, task_id)
    if _is_whole_project_final(task, store=store):
        return _frozen_task_context(store, task_id, task)
    _fail("REPAIR_INPUT_INVALID", "repairs require a REMEDIATION task")
    raise AssertionError("unreachable")


def _v2_event_id(event: Mapping[str, object]) -> str:
    payload = dict(event)
    payload.pop("event_id", None)
    return _v2_sha256(payload)


def _v2_common(
    replay: _AcceptanceReplay | None,
    context: _TaskContext,
    task_id: str,
    event_type: str,
    candidate_commit: str,
) -> dict[str, object]:
    previous = None if replay is None else replay.last_event_id
    index = 0 if replay is None else replay.event_count
    return {
        "ledger_version": _ACCEPTANCE_LEDGER_VERSION,
        "event_type": event_type,
        "event_index": index,
        "previous_event_id": previous,
        "timestamp_utc": _event_timestamp(),
        "task_id": task_id,
        "task_sha256": context.task_sha256,
        "base_commit": context.base_commit,
        "candidate_commit": candidate_commit,
    }


def _v2_append(
    store: WorkflowStore,
    task_id: str,
    replay: _AcceptanceReplay | None,
    context: _TaskContext,
    event_type: str,
    candidate_commit: str,
    fields: Mapping[str, object],
) -> dict[str, object]:
    if event_type not in _ACCEPTANCE_EVENT_TYPES:
        _fail("ACCEPTANCE_LEDGER_INVALID", "event type is not part of the v2 ledger")
    event = _v2_common(replay, context, task_id, event_type, candidate_commit)
    event.update(fields)
    event["event_id"] = _v2_event_id(event)
    store.append_event(task_id, event)
    return event


def _v2_validate_observed_receipt(
    receipt: VerifiedActorReceipt,
    task: Mapping[str, object],
    expected_runtime: tuple[str, str, str, str] | None = None,
) -> None:
    expected = expected_runtime or {
        "luna": ("gpt-5.6-luna", "max", "workspace-write", "workspace-write"),
        "luna_construction": ("gpt-5.6-luna", "max", "workspace-write", "workspace-write"),
        "terra_xhigh": ("gpt-5.6-terra", "xhigh", "workspace-write", "workspace-write"),
        "terra_xhigh_reviewer": ("gpt-5.6-terra", "xhigh", "read-only", "read-only"),
        "sol_medium_reviewer": ("gpt-5.6-sol", "medium", "read-only", "read-only"),
        "sol_xhigh": ("gpt-5.6-sol", "xhigh", "workspace-write", "assignment-scoped-write"),
    }.get(receipt.requested_role)
    if expected is None or (
        receipt.observed_model,
        receipt.observed_reasoning_effort,
        receipt.observed_sandbox_policy,
        receipt.observed_permission_profile,
    ) != expected:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt observed runtime does not match the assigned role")
    if (
        receipt.execution_surface == "NATIVE_SUBAGENT"
        and (not isinstance(receipt.native_agent_uuid, str) or receipt.codex_thread_id is not None)
    ) or (
        receipt.execution_surface == "CODEX_EXEC_ROLE_CONTRACT"
        and (not isinstance(receipt.codex_thread_id, str) or receipt.native_agent_uuid is not None)
    ):
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt identity source does not match its execution surface")
    try:
        if Path(receipt.observed_cwd).resolve() != Path(task["repository_root"]).resolve():
            _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt working directory is not task-bound")
    except (TypeError, OSError):
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt working directory is invalid")


def _v2_verified_runtime_receipt(
    store: WorkflowStore,
    task_id: str,
    receipt: VerifiedActorReceipt,
    task: Mapping[str, object],
    *,
    expected_attempt_id: str | None,
) -> None:
    """Bind a receipt to controller-written runtime evidence, never its caller.

    A v2 receipt is only a convenient projection of controller facts.  The
    authoritative record remains the append-only verified runtime-evidence
    ledger plus the controller event that recorded the native UUID or fresh
    Codex thread.  Keeping both sources lets a public adapter object *offer*
    a receipt while preventing it from inventing one.
    """

    workflow = _workflow()
    if expected_attempt_id is not None and receipt.attempt_id != expected_attempt_id:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt attempt does not match the issued capability")
    identity_source = (
        receipt.native_agent_uuid
        if receipt.execution_surface == "NATIVE_SUBAGENT"
        else receipt.codex_thread_id
    )
    if not isinstance(identity_source, str):
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt runtime identity source is missing")
    try:
        parsed_identity = uuid.UUID(identity_source)
    except (TypeError, ValueError):
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt runtime identity source is not canonical")
    if str(parsed_identity) != identity_source.lower() or receipt.runtime_instance_id != identity_source:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt runtime instance is not the recorded identity")

    evidence_path = Path(store._require_task(task_id)) / "runtime-evidence.jsonl"
    try:
        lines = evidence_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "controller runtime evidence cannot be read")
    evidence_matches: list[dict[str, object]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            _fail("ACCEPTANCE_RECEIPT_MISMATCH", "controller runtime evidence is malformed")
        if not isinstance(value, Mapping):
            _fail("ACCEPTANCE_RECEIPT_MISMATCH", "controller runtime evidence is malformed")
        try:
            workflow.validate_runtime_evidence(value)
        except RuntimeError:
            _fail("ACCEPTANCE_RECEIPT_MISMATCH", "controller runtime evidence is invalid")
        if (
            value.get("attempt_id") == receipt.attempt_id
            and value.get("requested_role") == receipt.requested_role
            and value.get("execution_surface") == receipt.execution_surface
            and value.get("observed_model") == receipt.observed_model
            and value.get("observed_reasoning_effort") == receipt.observed_reasoning_effort
            and value.get("observed_sandbox_policy") == receipt.observed_sandbox_policy
            and value.get("observed_permission_profile") == receipt.observed_permission_profile
            and value.get("observed_cwd") == receipt.observed_cwd
            and value.get("verification_status") == "VERIFIED"
            and _v2_sha256(value) == receipt.runtime_evidence_sha256
        ):
            evidence_matches.append(dict(value))
    if len(evidence_matches) != 1:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt is not bound to one verified runtime evidence record")

    identity_field = (
        "native_agent_uuid"
        if receipt.execution_surface == "NATIVE_SUBAGENT"
        else "thread_id"
    )
    runtime_events = [
        event
        for event in workflow._load_event_records(store, task_id)
        if event.get("event_type") == "RUNTIME_EVIDENCE_RECORDED"
        and event.get("attempt_id") == receipt.attempt_id
        and event.get("execution_surface") == receipt.execution_surface
        and event.get(identity_field) == identity_source
    ]
    if len(runtime_events) != 1:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt runtime identity lacks one controller event")
    recorded_hash = runtime_events[0].get("runtime_evidence_sha256")
    if recorded_hash is not None and recorded_hash != receipt.runtime_evidence_sha256:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "controller runtime event hash does not match receipt")


def _v2_validate_assignment_receipt(
    store: WorkflowStore,
    task_id: str,
    assignment: AcceptanceAssignment,
    receipt: VerifiedActorReceipt,
    task: Mapping[str, object],
    owner_actor: ActorIdentity,
) -> None:
    if receipt.assignment_id != assignment.assignment_id:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt belongs to another assignment")
    expected_runtime = None
    if assignment.phase == "SOL_MEDIUM_REPAIR":
        expected_runtime = (
            "gpt-5.6-sol",
            "medium",
            "workspace-write",
            "assignment-scoped-write",
        )
    _v2_validate_observed_receipt(receipt, task, expected_runtime)
    if receipt.actor_identity != assignment.expected_actor:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt identity does not match the issued actor")
    _v2_verified_runtime_receipt(
        store, task_id, receipt, task, expected_attempt_id=assignment.attempt_id
    )


def _v2_validate_controller_attestation(
    store: WorkflowStore,
    task_id: str,
    assignment: AcceptanceAssignment,
    attestation: ControllerExecutionAttestation,
    replay: _AcceptanceReplay,
    task: Mapping[str, object],
) -> None:
    """Require controller evidence to name the exact live capability only."""

    if (
        attestation.task_id != assignment.task_id
        or attestation.task_sha256 != assignment.capability.task_sha256
        or attestation.assignment_id != assignment.assignment_id
        or attestation.capability_id != assignment.capability.capability_id
        or attestation.candidate_commit != replay.current_candidate_commit
    ):
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "controller attestation is not bound to the active capability")
    _v2_validate_assignment_receipt(
        store, task_id, assignment, attestation.actor_receipt, task, replay.owner_actor
    )
    if assignment.phase in _REPAIR_PHASES:
        if assignment.capability.write_authority != "assignment-scoped-write":
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "repair capability lacks scoped write authority")
    elif assignment.capability.write_authority != "read-only":
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "review capability may not receive write authority")


def _v2_evidence(value: object) -> AdversarialEvidence:
    expected = set(AdversarialEvidence.__dataclass_fields__)
    if not isinstance(value, Mapping) or set(value) != expected:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "review evidence shape is invalid")
    try:
        return AdversarialEvidence(**dict(value))
    except (TypeError, RuntimeError):
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "review evidence is invalid")
    raise AssertionError("unreachable")


def _v2_evidence_output(completed: subprocess.CompletedProcess[str]) -> str:
    """Canonical bounded command transcript for the append-only ledger."""

    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    stderr = completed.stderr if isinstance(completed.stderr, str) else ""
    if len(stdout.encode("utf-8", errors="replace")) + len(
        stderr.encode("utf-8", errors="replace")
    ) > _MAX_EVIDENCE_TRANSCRIPT_BYTES:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller verification transcript exceeds its cap")
    return _v2_canonical(
        {
            "stdout": stdout,
            "stderr": stderr,
        }
    )


def _v2_safe_acceptance_argv(command: str) -> list[str]:
    """Resolve one intentionally small, non-interpreting acceptance argv.

    ``acceptance_commands`` is a frozen legacy string field, so the controller
    parses it once and immediately reduces it to a closed argv grammar.  Shell
    and interpreter expression forms are never accepted, and a caller's PATH
    is never used to resolve the executable.
    """

    if not isinstance(command, str) or not command.strip() or command != command.strip():
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "frozen acceptance command is empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in command):
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "frozen acceptance command contains control text")
    if not _SAFE_COMMAND_TEXT_PATTERN.fullmatch(command):
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "frozen acceptance command contains shell syntax")
    try:
        argv = shlex.split(command)
    except ValueError:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "frozen acceptance command cannot be parsed")
    if not argv or any(not isinstance(value, str) or not value for value in argv):
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "frozen acceptance command is empty")
    if command != " ".join(argv):
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "frozen acceptance command is not canonical argv")
    executable = argv[0]
    basename = Path(executable).name.casefold()
    forbidden = {"sh", "bash", "zsh", "dash", "fish", "command", "env"}
    if executable.casefold() != basename or basename in forbidden:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "frozen acceptance command is not safe argv")
    if basename == "git" and argv[1:] in (["diff", "--check"], ["status", "--porcelain=v1"]):
        resolved = shutil.which("git", path=_SAFE_EXECUTABLE_PATH)
    elif basename in {"python", "python3", "python3.11"} and tuple(argv[1:3]) == ("-m", "unittest"):
        targets = argv[3:]
        if not targets or any(
            not _UNITTEST_TARGET_PATTERN.fullmatch(target) for target in targets
        ):
            _fail(
                "ACCEPTANCE_EVIDENCE_INVALID",
                "frozen unittest command requires explicit dotted test targets",
            )
        resolved = shutil.which(basename, path=_SAFE_EXECUTABLE_PATH)
    else:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "frozen acceptance command is outside the safe argv allowlist")
    if not isinstance(resolved, str) or not resolved:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller safe executable is unavailable")
    return [resolved, *argv[1:]]


def execute_adversarial_evidence(
    store: WorkflowStore,
    task_id: str,
    expected_candidate_commit: str | None = None,
) -> AdversarialEvidence:
    """Execute frozen review checks and return controller-attested evidence.

    Model output never supplies this data.  The controller resolves the frozen
    task, executes each approved command without a shell, snapshots the checked
    out candidate tree, and exercises an out-of-scope mutation guard itself.
    """

    workflow = _workflow()
    stored_task = workflow.load_task(store._require_task(task_id) / "task.json")
    repository = Path(stored_task["repository_root"]).resolve()
    try:
        snapshot = workflow.capture_repo(repository)
    except RuntimeError:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller cannot capture the review repository")
    if snapshot.status:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller review repository is dirty")
    if expected_candidate_commit is not None and snapshot.head != expected_candidate_commit:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller review candidate does not match ledger")
    commands = _tuple(stored_task.get("acceptance_commands"), "acceptance_commands")
    if not commands or any(not isinstance(command, str) or not command.strip() for command in commands):
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "frozen acceptance commands are invalid")
    outputs: list[str] = []
    output_hashes: list[str] = []
    exit_codes: list[int] = []
    for command in commands:
        argv = _v2_safe_acceptance_argv(command)
        try:
            before_command = workflow.capture_repo(repository)
        except RuntimeError:
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller cannot snapshot verification command input")
        if before_command != snapshot or before_command.status:
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller verification repository changed before a command")
        try:
            completed = subprocess.run(
                argv,
                cwd=repository,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
                env={"PATH": _SAFE_EXECUTABLE_PATH, "LANG": "C", "LC_ALL": "C", "PYTHONNOUSERSITE": "1"},
            )
        except (OSError, subprocess.TimeoutExpired):
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller verification command could not run")
        try:
            after_command = workflow.capture_repo(repository)
        except RuntimeError:
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller cannot snapshot verification command output")
        if after_command != before_command or after_command.status:
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller verification command changed the repository")
        output = _v2_evidence_output(completed)
        outputs.append(output)
        output_hashes.append(_v2_sha256(output))
        exit_codes.append(completed.returncode)
        if completed.returncode != 0:
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller verification command failed")
    try:
        tree = workflow.git(repository, "rev-parse", f"{snapshot.head}^{{tree}}")
    except RuntimeError:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller cannot bind the review artifact")
    artifact = _v2_sha256(
        {
            "candidate_commit": snapshot.head,
            "candidate_tree": tree,
            "task_sha256": workflow._task_sha256(store, task_id),
        }
    )
    probe_path = ".adversarial-controller-scope-probe"
    try:
        workflow.assert_allowed_changes({probe_path}, stored_task["allowed_write_paths"])
    except RuntimeError as exc:
        if getattr(exc, "code", None) != "OUT_OF_SCOPE_CHANGE":
            _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller scope mutation returned an unexpected result")
        negative_output = _v2_canonical(
            {"code": exc.code, "message": exc.message, "probe_path": probe_path}
        )
    else:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "controller scope mutation was accepted")
    return AdversarialEvidence(
        verification_commands=tuple(commands),
        negative_checks=("controller-scope-mutation",),
        outputs=tuple(outputs),
        verification_exit_codes=tuple(exit_codes),
        verification_output_sha256=tuple(output_hashes),
        artifact_sha256=tuple(artifact for _ in commands),
        negative_exit_codes=(1,),
        negative_output_sha256=(_v2_sha256(negative_output),),
    )


def _v2_task_forbidden_actions(task: Mapping[str, object]) -> tuple[str, ...]:
    raw = task.get("forbidden_actions")
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        _fail("ACCEPTANCE_LEDGER_INVALID", "task forbidden actions are invalid")
    forbidden = tuple(sorted(set(raw)))
    if "merge" not in forbidden or "push" not in forbidden:
        _fail("ACCEPTANCE_LEDGER_INVALID", "task must forbid merge and push")
    return forbidden


def _v2_assert_findings_within_task(
    findings: tuple[RepairFinding, ...], task: Mapping[str, object]
) -> None:
    allowed = task.get("allowed_write_paths")
    if not isinstance(allowed, list) or any(not isinstance(path, str) for path in allowed):
        _fail("ACCEPTANCE_LEDGER_INVALID", "task write scope is invalid")
    try:
        _workflow().assert_allowed_changes(set(_v2_allowed_paths(findings)), allowed)
    except RuntimeError as exc:
        if getattr(exc, "code", None) == "OUT_OF_SCOPE_CHANGE":
            _fail("ACCEPTANCE_SCOPE_VIOLATION", "findings exceed the immutable task scope")
        raise


def _v2_assignment_payload(
    task_id: str,
    phase: str,
    attempt_id: str,
    expected_actor: ActorIdentity,
    base_commit: str,
    input_candidate_commit: str,
    findings: tuple[RepairFinding, ...],
    issuing_event_id: str,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "phase": phase,
        "attempt_id": attempt_id,
        "expected_actor": _v2_actor_payload(expected_actor),
        "base_commit": base_commit,
        "input_candidate_commit": input_candidate_commit,
        "findings": _v2_findings_payload(findings),
        "allowed_paths": list(_v2_allowed_paths(findings)),
        "issuing_event_id": issuing_event_id,
    }


def _v2_make_assignment(
    context: _TaskContext,
    task_id: str,
    phase: str,
    attempt_id: str,
    expected_actor: ActorIdentity,
    input_candidate_commit: str,
    findings: tuple[RepairFinding, ...],
    issuing_event_id: str,
    forbidden_actions: tuple[str, ...],
) -> AcceptanceAssignment:
    payload = _v2_assignment_payload(
        task_id, phase, attempt_id, expected_actor, context.base_commit,
        input_candidate_commit, findings, issuing_event_id,
    )
    assignment_id = _v2_sha256(payload)
    write_authority = "read-only" if phase in _REVIEW_PHASES else "assignment-scoped-write"
    capability_fields = {
        "task_id": task_id,
        "task_sha256": context.task_sha256,
        "assignment_id": assignment_id,
        "phase": phase,
        "attempt_id": attempt_id,
        "base_commit": context.base_commit,
        "input_candidate_commit": input_candidate_commit,
        "finding_ids": tuple(finding.finding_id for finding in findings),
        "allowed_paths": _v2_allowed_paths(findings),
        "forbidden_actions": forbidden_actions,
        "write_authority": write_authority,
        "issuing_event_id": issuing_event_id,
        "expected_actor": expected_actor,
    }
    capability_hash_fields = dict(capability_fields)
    capability_hash_fields["expected_actor"] = _v2_actor_payload(expected_actor)
    capability = AssignmentCapability(
        capability_id=_v2_sha256(capability_hash_fields), **capability_fields
    )
    return AcceptanceAssignment(
        assignment_id=assignment_id,
        task_id=task_id,
        phase=phase,
        attempt_id=attempt_id,
        expected_actor=expected_actor,
        base_commit=context.base_commit,
        input_candidate_commit=input_candidate_commit,
        findings=findings,
        allowed_paths=_v2_allowed_paths(findings),
        capability=capability,
    )


def _v2_assignment_payload_for_event(assignment: AcceptanceAssignment) -> dict[str, object]:
    return {
        "assignment_id": assignment.assignment_id,
        "attempt_id": assignment.attempt_id,
        "phase": assignment.phase,
        "expected_actor": _v2_actor_payload(assignment.expected_actor),
        "input_candidate_commit": assignment.input_candidate_commit,
        "findings": _v2_findings_payload(assignment.findings),
        "allowed_paths": list(assignment.allowed_paths),
        "capability": asdict(assignment.capability),
    }


def _v2_assignment_from_event(
    event: Mapping[str, object], context: _TaskContext, task_id: str, previous_event_id: str
) -> AcceptanceAssignment:
    if event.get("phase") not in _ACCEPTANCE_PHASES:
        _fail("ACCEPTANCE_LEDGER_INVALID", "issued assignment phase is invalid")
    phase = str(event["phase"])
    expected_actor = _v2_actor(event.get("expected_actor"), "expected_actor")
    attempt_id = _nonempty(event.get("attempt_id"), "attempt_id", code="ACCEPTANCE_LEDGER_INVALID")
    findings = _v2_findings_from_payload(event.get("findings"))
    allowed = tuple(event.get("allowed_paths", ()))
    if allowed != _v2_allowed_paths(findings):
        _fail("ACCEPTANCE_LEDGER_INVALID", "assignment allowed paths drift from findings")
    candidate = _v2_sha(event.get("input_candidate_commit"), "input_candidate_commit", length=40)
    forbidden = tuple(context_forbidden_actions(event, context))
    assignment = _v2_make_assignment(
        context,
        task_id,
        phase,
        attempt_id,
        expected_actor,
        candidate,
        findings,
        previous_event_id,
        forbidden,
    )
    if event.get("assignment_id") != assignment.assignment_id:
        _fail("ACCEPTANCE_LEDGER_INVALID", "assignment ID is not canonical")
    if _v2_canonical(event.get("capability")) != _v2_canonical(asdict(assignment.capability)):
        _fail("ACCEPTANCE_LEDGER_INVALID", "assignment capability is not immutable")
    return assignment


def context_forbidden_actions(event: Mapping[str, object], context: _TaskContext) -> tuple[str, ...]:
    capability = event.get("capability")
    if not isinstance(capability, Mapping):
        _fail("ACCEPTANCE_LEDGER_INVALID", "assignment capability is missing")
    forbidden = capability.get("forbidden_actions")
    if not isinstance(forbidden, list) or any(not isinstance(item, str) for item in forbidden):
        _fail("ACCEPTANCE_LEDGER_INVALID", "capability forbidden actions are invalid")
    values = tuple(forbidden)
    if values != tuple(sorted(values)) or "merge" not in values or "push" not in values:
        _fail("ACCEPTANCE_LEDGER_INVALID", "capability forbidden actions are invalid")
    return values


def open_task_acceptance(
    store: WorkflowStore, task: Mapping[str, object], owner_receipt: VerifiedActorReceipt
) -> ActorIdentity:
    """Open one v2 ledger from the immutable stored task and owner receipt."""

    if not isinstance(task, Mapping):
        _fail("ACCEPTANCE_LEDGER_INVALID", "task must be an object")
    task_id = task.get("task_id")
    if not isinstance(task_id, str):
        _fail("ACCEPTANCE_LEDGER_INVALID", "task_id is missing")
    with store.lock(task_id):
        stored = _workflow().load_task(store._require_task(task_id) / "task.json")
        if stored != dict(task):
            _fail("ACCEPTANCE_LEDGER_INVALID", "acceptance task is not the stored frozen task")
        context = _v2_context(store, task_id)
        if _v2_event_records(store, task_id):
            _fail("ACCEPTANCE_LEDGER_REPLAY", "task acceptance has already been opened")
        if owner_receipt.assignment_id != hashlib.sha256(f"open:{task_id}".encode("utf-8")).hexdigest():
            _fail("ACCEPTANCE_RECEIPT_MISMATCH", "opening receipt is not task-bound")
        _v2_validate_observed_receipt(owner_receipt, stored)
        _v2_verified_runtime_receipt(
            store, task_id, owner_receipt, stored, expected_attempt_id=None
        )
        allowed_owners = {"luna", "terra_xhigh"}
        if _is_whole_project_final(stored, store=store):
            allowed_owners = {"luna", "terra_xhigh", "luna_construction"}
        if owner_receipt.requested_role not in allowed_owners:
            _fail("ACCEPTANCE_RECEIPT_MISMATCH", "only Luna or Terra xhigh may own acceptance")
        owner_actor = owner_receipt.actor_identity
        event = _v2_append(
            store,
            task_id,
            None,
            context,
            "ACCEPTANCE_OPENED",
            context.candidate_commit,
            {
                "owner_actor": _v2_actor_payload(owner_actor),
                "owner_receipt": _v2_receipt_payload(owner_receipt),
                "owner_receipt_sha256": _v2_sha256(_v2_receipt_payload(owner_receipt)),
                "initial_candidate_commit": context.candidate_commit,
            },
        )
        if not isinstance(event.get("event_id"), str):
            raise AssertionError("v2 event must have an ID")
        return owner_actor


def replay_acceptance_ledger(store: WorkflowStore, task_id: str) -> _AcceptanceReplay | None:
    """Validate the hash chain and recognise v2 ownership before any execution."""

    records = _v2_event_records(store, task_id)
    if not records:
        return None
    context = _v2_context(store, task_id)
    replay: _AcceptanceReplay | None = None
    previous_id: str | None = None
    for index, event in enumerate(records):
        if set(_V2_COMMON_FIELDS) - set(event):
            _fail("ACCEPTANCE_LEDGER_INVALID", "v2 event lacks a required common field")
        if event.get("ledger_version") != _ACCEPTANCE_LEDGER_VERSION or event.get("event_index") != index:
            _fail("ACCEPTANCE_LEDGER_INVALID", "v2 event ordering is invalid")
        if event.get("previous_event_id") != previous_id or event.get("event_id") != _v2_event_id(event):
            _fail("ACCEPTANCE_LEDGER_INVALID", "v2 event identity chain is invalid")
        if event.get("task_id") != task_id or event.get("task_sha256") != context.task_sha256:
            _fail("ACCEPTANCE_LEDGER_INVALID", "v2 event task binding drifted")
        if event.get("base_commit") != context.base_commit:
            _fail("ACCEPTANCE_LEDGER_INVALID", "v2 event base commit drifted")
        event_type = event.get("event_type")
        if event_type not in _ACCEPTANCE_EVENT_TYPES:
            _fail("ACCEPTANCE_LEDGER_INVALID", "v2 event type is invalid")
        if index == 0:
            if event_type != "ACCEPTANCE_OPENED" or event.get("candidate_commit") != context.candidate_commit:
                _fail("ACCEPTANCE_LEDGER_INVALID", "v2 ledger must begin with the stored candidate")
            owner_receipt = _v2_receipt(event.get("owner_receipt"), "owner_receipt")
            if event.get("owner_receipt_sha256") != _v2_sha256(_v2_receipt_payload(owner_receipt)):
                _fail("ACCEPTANCE_LEDGER_INVALID", "owner receipt binding drifted")
            owner_actor = _v2_actor(event.get("owner_actor"), "owner_actor")
            if owner_actor != owner_receipt.actor_identity:
                _fail("ACCEPTANCE_LEDGER_INVALID", "owner actor does not match the opening receipt")
            stored_task = _workflow().load_task(store._require_task(task_id) / "task.json")
            _v2_validate_observed_receipt(owner_receipt, stored_task)
            _v2_verified_runtime_receipt(
                store, task_id, owner_receipt, stored_task, expected_attempt_id=None
            )
            replay = _AcceptanceReplay(
                task_id,
                context.task_sha256,
                context.base_commit,
                context.candidate_commit,
                context.candidate_commit,
                owner_actor,
                owner_receipt,
                str(event["event_id"]),
                1,
                {},
                None,
                False,
                {},
                (),
                set(),
                {},
                {},
                set(),
                {},
                whole_project_final=_is_whole_project_final(stored_task, store=store),
            )
        else:
            assert replay is not None
            if event_type == "ASSIGNMENT_ISSUED":
                if replay.active_assignment_id is not None or replay.terminal:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "assignment was issued while the ladder is closed")
                if event.get("candidate_commit") != replay.current_candidate_commit:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "issued candidate binding drifted")
                assignment = _v2_assignment_from_event(event, context, task_id, previous_id or "")
                expected_phase, expected_findings = _v2_next_phase(replay)
                if assignment.phase != expected_phase or assignment.findings != expected_findings:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "issued phase or findings bypassed the repair ladder")
                _v2_validate_phase_actor(replay, assignment)
                replay.assignments[assignment.assignment_id] = assignment
                replay.active_assignment_id = assignment.assignment_id
            elif event_type == "ASSIGNMENT_ATTEMPT_STARTED":
                if event.get("candidate_commit") != replay.current_candidate_commit:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "attempt candidate binding drifted")
                assignment_id = event.get("assignment_id")
                if assignment_id != replay.active_assignment_id or not isinstance(assignment_id, str):
                    _fail("ACCEPTANCE_LEDGER_INVALID", "attempt start lacks the active assignment")
                assignment = replay.assignments[assignment_id]
                if event.get("attempt_id") != assignment.attempt_id or assignment_id in replay.started_receipts:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "attempt start is duplicated or mismatched")
                receipt = _v2_receipt(event.get("actor_receipt"), "actor_receipt")
                stored_task = _workflow().load_task(store._require_task(task_id) / "task.json")
                _v2_validate_assignment_receipt(
                    store, task_id, assignment, receipt, stored_task, replay.owner_actor
                )
                if event.get("receipt_sha256") != _v2_sha256(_v2_receipt_payload(receipt)):
                    _fail("ACCEPTANCE_LEDGER_INVALID", "attempt receipt binding drifted")
                if "controller_attestation" in event or "controller_attestation_sha256" in event:
                    attestation = _v2_controller_attestation(
                        event.get("controller_attestation"), "controller_attestation"
                    )
                    if event.get("controller_attestation_sha256") != attestation.attestation_sha256:
                        _fail("ACCEPTANCE_LEDGER_INVALID", "controller attestation binding drifted")
                    _v2_validate_controller_attestation(
                        store,
                        task_id,
                        assignment,
                        attestation,
                        replay,
                        stored_task,
                    )
                    replay.started_attestations[assignment_id] = attestation
                replay.started_receipts[assignment_id] = receipt
            elif event_type == "ASSIGNMENT_ATTEMPT_FAILED":
                if event.get("candidate_commit") != replay.current_candidate_commit:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "failed attempt candidate binding drifted")
                assignment_id = event.get("assignment_id")
                if assignment_id != replay.active_assignment_id or not isinstance(assignment_id, str):
                    _fail("ACCEPTANCE_LEDGER_INVALID", "failed attempt lacks its active assignment")
                assignment = replay.assignments[assignment_id]
                if (
                    event.get("attempt_id") != assignment.attempt_id
                    or assignment_id not in replay.started_receipts
                    or assignment_id in replay.finished_assignment_ids
                ):
                    _fail("ACCEPTANCE_LEDGER_INVALID", "failed attempt lifecycle is invalid")
                _nonempty(event.get("failure_code"), "failure_code", code="ACCEPTANCE_LEDGER_INVALID")
                _nonempty(event.get("failure_message"), "failure_message", code="ACCEPTANCE_LEDGER_INVALID")
                replay.finished_assignment_ids.add(assignment_id)
                replay.active_assignment_id = None
            elif event_type == "REPAIR_COMPLETED":
                assignment_id = event.get("assignment_id")
                if assignment_id != replay.active_assignment_id or not isinstance(assignment_id, str):
                    _fail("ACCEPTANCE_LEDGER_INVALID", "repair lacks the active assignment")
                assignment = replay.assignments[assignment_id]
                if assignment.phase not in _REPAIR_PHASES or event.get("attempt_id") != assignment.attempt_id:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "repair assignment binding is invalid")
                receipt = _v2_receipt(event.get("actor_receipt"), "actor_receipt")
                if replay.started_receipts.get(assignment_id) != receipt:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "repair receipt was not bound at attempt start")
                output = _v2_sha(event.get("candidate_commit"), "candidate_commit", length=40)
                actual = _v2_validate_repair_output(
                    _workflow().load_task(store._require_task(task_id) / "task.json"),
                    assignment,
                    output,
                    event.get("changed_paths"),
                )
                if event.get("actual_changed_paths") != list(actual):
                    _fail("ACCEPTANCE_LEDGER_INVALID", "repair actual diff binding drifted")
                replay.finished_assignment_ids.add(assignment_id)
                replay.active_assignment_id = None
                replay.current_candidate_commit = output
                replay.phase_outcomes[assignment.phase] = "COMPLETED"
                replay.repairer_identities[assignment.phase] = receipt.actor_identity.identity
                if assignment.phase == "SOL_XHIGH_TERMINAL_REPAIR":
                    if (
                        event.get("terminal_state") != "TASK_TERMINAL"
                        or event.get("whole_project_acceptance_required") != "PENDING"
                        or not isinstance(event.get("terminal_reason"), str)
                    ):
                        _fail("ACCEPTANCE_LEDGER_INVALID", "terminal Sol repair is not bounded")
                    replay.terminal = True
            elif event_type == "REVIEW_COMPLETED":
                if event.get("candidate_commit") != replay.current_candidate_commit:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "review candidate binding drifted")
                assignment_id = event.get("assignment_id")
                if assignment_id != replay.active_assignment_id or not isinstance(assignment_id, str):
                    _fail("ACCEPTANCE_LEDGER_INVALID", "review lacks the active assignment")
                assignment = replay.assignments[assignment_id]
                if assignment.phase not in _REVIEW_PHASES or event.get("attempt_id") != assignment.attempt_id:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "review assignment binding is invalid")
                receipt = _v2_receipt(event.get("reviewer_receipt"), "reviewer_receipt")
                if replay.started_receipts.get(assignment_id) != receipt:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "review receipt was not bound at attempt start")
                evidence = _v2_evidence(event.get("evidence"))
                if event.get("evidence_sha256") != _v2_sha256(asdict(evidence)):
                    _fail("ACCEPTANCE_LEDGER_INVALID", "review evidence binding drifted")
                verdict = event.get("verdict")
                if verdict not in {"ACCEPT", "REWORK"}:
                    _fail("ACCEPTANCE_LEDGER_INVALID", "review verdict is invalid")
                findings = _v2_findings_from_payload(event.get("findings"))
                stored_task = _workflow().load_task(store._require_task(task_id) / "task.json")
                if verdict == "ACCEPT":
                    if findings:
                        _fail("ACCEPTANCE_LEDGER_INVALID", "accepted review may not carry findings")
                    if (
                        event.get("terminal_state") != "TASK_TERMINAL"
                        or event.get("whole_project_acceptance_required") != "PENDING"
                        or not isinstance(event.get("terminal_reason"), str)
                    ):
                        _fail("ACCEPTANCE_LEDGER_INVALID", "terminal acceptance is not explicitly bounded")
                    replay.terminal = True
                else:
                    if not findings:
                        _fail("ACCEPTANCE_LEDGER_INVALID", "rework review must carry canonical findings")
                    _v2_assert_findings_within_task(findings, stored_task)
                    if assignment.phase != "REVIEW_1" and not set(_v2_allowed_paths(findings)).issubset(
                        assignment.allowed_paths
                    ):
                        _fail("ACCEPTANCE_LEDGER_INVALID", "rework findings expanded immutable review scope")
                    if any(field in event for field in (
                        "terminal_state", "terminal_reason", "whole_project_acceptance_required"
                    )):
                        _fail("ACCEPTANCE_LEDGER_INVALID", "rework review cannot terminally close the task")
                    replay.pending_findings = findings
                replay.active_assignment_id = None
                replay.finished_assignment_ids.add(assignment_id)
                replay.phase_outcomes[assignment.phase] = str(verdict)
                replay.reviewer_identities.add(receipt.actor_identity.identity)
            else:
                _fail("ACCEPTANCE_LEDGER_INVALID", "v2 event result type is invalid")
            replay.last_event_id = str(event["event_id"])
            replay.event_count += 1
        previous_id = str(event["event_id"])
    return replay


def _final_acceptance_rework_policy() -> Mapping[str, object]:
    config = _workflow()._load_workflow_config()
    policy = config.get("final_acceptance_rework")
    if not isinstance(policy, Mapping) or dict(policy) != _FROZEN_FINAL_ACCEPTANCE_REWORK:
        _fail(
            "ACCEPTANCE_SEQUENCE_INVALID",
            "final_acceptance_rework policy is not the frozen contract",
        )
    return policy


def _assert_automatic_xhigh_disabled() -> None:
    config = _workflow()._load_workflow_config()
    policy = config.get("policy")
    if not isinstance(policy, Mapping) or policy.get("automatic_xhigh") is not False:
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "automatic_xhigh must remain disabled")


def _v2_next_phase(replay: _AcceptanceReplay) -> tuple[str, tuple[RepairFinding, ...]]:
    if replay.terminal or replay.active_assignment_id is not None:
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance ladder has no issuable step")
    outcomes = replay.phase_outcomes
    if "REVIEW_1" not in outcomes:
        return "REVIEW_1", ()
    if outcomes["REVIEW_1"] != "REWORK":
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "accepted review already terminally closed the task")
    if replay.whole_project_final:
        if "SOL_MEDIUM_REPAIR" not in outcomes:
            return "SOL_MEDIUM_REPAIR", replay.pending_findings
        if "SOL_MEDIUM_PEER_REVIEW" not in outcomes:
            return "SOL_MEDIUM_PEER_REVIEW", replay.pending_findings
        if outcomes["SOL_MEDIUM_PEER_REVIEW"] != "REWORK":
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "Sol peer acceptance already terminally closed the task")
        if "SOL_XHIGH_TERMINAL_REPAIR" not in outcomes:
            return "SOL_XHIGH_TERMINAL_REPAIR", replay.pending_findings
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "terminal Sol repair already closed the task")
    if "OWNER_REPAIR" not in outcomes:
        return "OWNER_REPAIR", replay.pending_findings
    if "REVIEW_2" not in outcomes:
        return "REVIEW_2", replay.pending_findings
    if outcomes["REVIEW_2"] != "REWORK":
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "second accepted review already terminally closed the task")
    if "SOL_MEDIUM_REPAIR" not in outcomes:
        return "SOL_MEDIUM_REPAIR", replay.pending_findings
    if "SOL_MEDIUM_PEER_REVIEW" not in outcomes:
        return "SOL_MEDIUM_PEER_REVIEW", replay.pending_findings
    if outcomes["SOL_MEDIUM_PEER_REVIEW"] != "REWORK":
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "Sol peer acceptance already terminally closed the task")
    if "SOL_XHIGH_TERMINAL_REPAIR" not in outcomes:
        return "SOL_XHIGH_TERMINAL_REPAIR", replay.pending_findings
    _fail("ACCEPTANCE_SEQUENCE_INVALID", "terminal Sol repair already closed the task")


def _v2_validate_whole_project_phase_actor(
    replay: _AcceptanceReplay, assignment: AcceptanceAssignment
) -> None:
    phase = assignment.phase
    actor = assignment.expected_actor
    policy = _final_acceptance_rework_policy()
    if phase in {"OWNER_REPAIR", "REVIEW_2"} or actor.role == "terra_xhigh_reviewer":
        _fail(
            "ACCEPTANCE_SEQUENCE_INVALID",
            "whole-project final does not use OWNER_REPAIR, REVIEW_2, or terra_xhigh_reviewer",
        )
    if phase == "REVIEW_1":
        if actor.role != _SOL_MEDIUM_REVIEWER:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "whole-project REVIEW_1 requires Sol medium acceptor")
        return
    if phase == "SOL_MEDIUM_REPAIR":
        if actor.role != policy["fixer_role"] or actor.identity in replay.reviewer_identities:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "Sol fixer must be distinct from the acceptor")
        return
    if phase == "SOL_MEDIUM_PEER_REVIEW":
        fixer = replay.repairer_identities.get("SOL_MEDIUM_REPAIR")
        if (
            actor.role != policy["recheck_role"]
            or actor.identity == fixer
            or actor.identity in replay.reviewer_identities
        ):
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "Sol recheck must be distinct from acceptor and fixer")
        return
    if phase == "SOL_XHIGH_TERMINAL_REPAIR" and actor.role == policy["terminal_escalation_role"]:
        return
    _fail("ACCEPTANCE_SEQUENCE_INVALID", "assignment role is not allowed for this ladder phase")


def _v2_validate_phase_actor(
    replay: _AcceptanceReplay, assignment: AcceptanceAssignment
) -> None:
    if replay.whole_project_final:
        _v2_validate_whole_project_phase_actor(replay, assignment)
        return
    phase = assignment.phase
    actor = assignment.expected_actor
    if phase == "OWNER_REPAIR":
        if actor != replay.owner_actor:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "owner repair must retain the original owner role")
        return
    if phase in {"REVIEW_1", "REVIEW_2"}:
        if actor.role != "terra_xhigh_reviewer":
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "Terra reviews require Terra xhigh reviewer role")
        forbidden = set(replay.reviewer_identities) | {replay.owner_actor.identity}
        owner_repair = replay.repairer_identities.get("OWNER_REPAIR")
        if owner_repair is not None:
            forbidden.add(owner_repair)
        if actor.identity in forbidden:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "Terra reviewer identity must be independent")
        return
    if phase == "SOL_MEDIUM_REPAIR":
        if actor.role != "sol_medium_reviewer":
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "Sol fallback requires its bounded medium fixer")
        return
    if phase == "SOL_MEDIUM_PEER_REVIEW":
        if actor.role != "sol_medium_reviewer" or actor.identity == replay.repairer_identities.get(
            "SOL_MEDIUM_REPAIR"
        ):
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "Sol peer must be distinct from its fixer")
        return
    if phase == "SOL_XHIGH_TERMINAL_REPAIR" and actor.role == "sol_xhigh":
        return
    _fail("ACCEPTANCE_SEQUENCE_INVALID", "assignment role is not allowed for this ladder phase")


def issue_acceptance_assignment(
    store: WorkflowStore, task_id: str, phase: str, expected_actor: ActorIdentity
) -> AcceptanceAssignment:
    """Issue a single immutable v2 capability; it has no ambient write authority."""

    if phase not in _ACCEPTANCE_PHASES or not isinstance(expected_actor, ActorIdentity):
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "assignment request is invalid")
    with store.lock(task_id):
        replay = replay_acceptance_ledger(store, task_id)
        if replay is None:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance must be opened before issuing work")
        context = _v2_context(store, task_id)
        replay = _v2_recover_orphaned_attempt(store, task_id, replay, context)
        expected_phase, findings = _v2_next_phase(replay)
        if phase != expected_phase:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "requested phase does not match the capped repair ladder")
        stored = _workflow().load_task(store._require_task(task_id) / "task.json")
        _v2_assert_findings_within_task(findings, stored)
        forbidden = _v2_task_forbidden_actions(stored)
        attempt_id = f"{phase.lower()}-attempt-{sum(a.phase == phase for a in replay.assignments.values()) + 1}"
        assignment = _v2_make_assignment(
            context,
            task_id,
            phase,
            attempt_id,
            expected_actor,
            replay.current_candidate_commit,
            findings,
            replay.last_event_id,
            forbidden,
        )
        _v2_validate_phase_actor(replay, assignment)
        if replay.whole_project_final and phase == "SOL_XHIGH_TERMINAL_REPAIR":
            _assert_automatic_xhigh_disabled()
            if any(item.phase == "SOL_XHIGH_TERMINAL_REPAIR" for item in replay.assignments.values()):
                _fail("ACCEPTANCE_SEQUENCE_INVALID", "terminal Sol xhigh may be issued only once")
            _require_one_final_xhigh_ticket(store, task_id, replay)
        _v2_append(
            store,
            task_id,
            replay,
            context,
            "ASSIGNMENT_ISSUED",
            replay.current_candidate_commit,
            _v2_assignment_payload_for_event(assignment),
        )
        return assignment


def _final_xhigh_decision_records(store: WorkflowStore, task_id: str) -> list[dict[str, object]]:
    path = store._require_task(task_id) / "human-decisions.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise _workflow().WorkflowError("DECISION_READ_ERROR", f"cannot read decisions for {task_id}") from exc
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _workflow().WorkflowError(
                "INVALID_DECISION_RECORD", f"invalid decision JSON for {task_id}"
            ) from exc
        if not isinstance(record, dict):
            _fail("INVALID_DECISION_RECORD", "decision record must be an object")
        if record.get("decision") == _FINAL_XHIGH_DECISION:
            records.append(record)
    return records


def _final_xhigh_ticket_is_valid(
    record: Mapping[str, object],
    store: WorkflowStore,
    task_id: str,
    replay: _AcceptanceReplay,
) -> bool:
    if set(record) != _FINAL_XHIGH_TICKET_FIELDS:
        return False
    if (
        record.get("event_type") != "OWNER_DECISION"
        or record.get("decision") != _FINAL_XHIGH_DECISION
        or record.get("new_state") != _FINAL_XHIGH_STATE
    ):
        return False
    actor = record.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        return False
    for field in ("timestamp_utc", "previous_state"):
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    return (
        record.get("task_sha256") == _workflow()._task_sha256(store, task_id)
        and record.get("candidate_commit") == replay.current_candidate_commit
        and record.get("acceptance_event_id") == replay.last_event_id
    )


def _require_one_final_xhigh_ticket(
    store: WorkflowStore, task_id: str, replay: _AcceptanceReplay
) -> Mapping[str, object]:
    tickets = _final_xhigh_decision_records(store, task_id)
    if any(not _final_xhigh_ticket_is_valid(ticket, store, task_id, replay) for ticket in tickets):
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "final-xhigh authorization ticket is invalid")
    if len(tickets) != 1:
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "exactly one final-xhigh authorization ticket is required")
    return tickets[0]


def authorize_final_xhigh(store: WorkflowStore, task_id: str, actor: str) -> None:
    """Record one owner-authorized Sol xhigh for whole-project final acceptance."""

    if not isinstance(actor, str) or not actor.strip():
        _fail("INVALID_ACTOR", "actor must be a non-empty string")
    workflow = _workflow()
    with store.lock(task_id):
        stored = workflow.load_task(store._require_task(task_id) / "task.json")
        if not _is_whole_project_final(stored, store=store):
            _fail(
                "ACCEPTANCE_SEQUENCE_INVALID",
                "xhigh authorization is reserved for whole-project final acceptance",
            )
        replay = replay_acceptance_ledger(store, task_id)
        if replay is None:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance must be opened before authorizing xhigh")
        if replay.phase_outcomes.get("SOL_MEDIUM_PEER_REVIEW") != "REWORK":
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "xhigh authorization requires peer REWORK")
        if any(item.phase == "SOL_XHIGH_TERMINAL_REPAIR" for item in replay.assignments.values()):
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "terminal Sol xhigh has already been used")
        _assert_automatic_xhigh_disabled()
        if _final_xhigh_decision_records(store, task_id):
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "whole-project xhigh is already authorized")
        store.record_decision(
            task_id,
            {
                "event_type": "OWNER_DECISION",
                "decision": _FINAL_XHIGH_DECISION,
                "actor": actor.strip(),
                "timestamp_utc": _event_timestamp(),
                "previous_state": workflow._current_state(store, task_id),
                "new_state": _FINAL_XHIGH_STATE,
                "task_sha256": workflow._task_sha256(store, task_id),
                "candidate_commit": replay.current_candidate_commit,
                "acceptance_event_id": replay.last_event_id,
            },
        )


def _v2_require_issued_assignment(
    replay: _AcceptanceReplay,
    assignment: AcceptanceAssignment,
) -> None:
    if replay.active_assignment_id != assignment.assignment_id:
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "assignment is not the active ledger capability")
    stored = replay.assignments.get(assignment.assignment_id)
    if stored != assignment:
        _fail("ACCEPTANCE_LEDGER_INVALID", "assignment differs from its immutable issuance")


def _v2_start_attempt(
    store: WorkflowStore,
    task_id: str,
    replay: _AcceptanceReplay,
    context: _TaskContext,
    assignment: AcceptanceAssignment,
    receipt: VerifiedActorReceipt,
    stored_task: Mapping[str, object],
    controller_attestation: ControllerExecutionAttestation | None = None,
) -> _AcceptanceReplay:
    _v2_require_issued_assignment(replay, assignment)
    _v2_validate_assignment_receipt(
        store, task_id, assignment, receipt, stored_task, replay.owner_actor
    )
    if controller_attestation is not None:
        _v2_validate_controller_attestation(
            store, task_id, assignment, controller_attestation, replay, stored_task
        )
        if controller_attestation.actor_receipt != receipt:
            _fail("ACCEPTANCE_RECEIPT_MISMATCH", "controller attestation receipt drifted")
    started = replay.started_receipts.get(assignment.assignment_id)
    if started is not None:
        if started != receipt:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "assignment attempt receipt changed after launch")
        if controller_attestation is not None and (
            replay.started_attestations.get(assignment.assignment_id) != controller_attestation
        ):
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "assignment controller attestation changed after launch")
        return replay
    fields: dict[str, object] = {
        "assignment_id": assignment.assignment_id,
        "attempt_id": assignment.attempt_id,
        "actor_receipt": _v2_receipt_payload(receipt),
        "receipt_sha256": _v2_sha256(_v2_receipt_payload(receipt)),
    }
    if controller_attestation is not None:
        fields.update(
            {
                "controller_attestation": asdict(controller_attestation),
                "controller_attestation_sha256": controller_attestation.attestation_sha256,
            }
        )
    _v2_append(
        store,
        task_id,
        replay,
        context,
        "ASSIGNMENT_ATTEMPT_STARTED",
        replay.current_candidate_commit,
        fields,
    )
    fresh = replay_acceptance_ledger(store, task_id)
    if fresh is None:
        raise AssertionError("started v2 ledger must replay")
    return fresh


def _v2_fail_attempt(
    store: WorkflowStore,
    task_id: str,
    replay: _AcceptanceReplay,
    context: _TaskContext,
    assignment: AcceptanceAssignment,
    error: BaseException,
) -> None:
    _v2_append(
        store,
        task_id,
        replay,
        context,
        "ASSIGNMENT_ATTEMPT_FAILED",
        replay.current_candidate_commit,
        {
            "assignment_id": assignment.assignment_id,
            "attempt_id": assignment.attempt_id,
            "failure_code": str(getattr(error, "code", "ACCEPTANCE_ATTEMPT_FAILED")),
            "failure_message": str(getattr(error, "message", error)) or "attempt failed",
        },
    )


def _v2_validate_repair_output(
    task: Mapping[str, object],
    assignment: AcceptanceAssignment,
    output_candidate_commit: str,
    changed_paths: object,
) -> tuple[str, ...]:
    workflow = _workflow()
    repository = Path(task["repository_root"]).resolve()
    try:
        workflow.git(repository, "merge-base", "--is-ancestor", assignment.input_candidate_commit, output_candidate_commit)
        parents = workflow.git(repository, "rev-list", "--parents", "-n", "1", output_candidate_commit).split()
    except RuntimeError:
        _fail("ACCEPTANCE_CANDIDATE_INVALID", "repair candidate is not a resolvable descendant")
    if len(parents) != 2:
        _fail("ACCEPTANCE_CANDIDATE_INVALID", "repair candidate must be a non-merge commit")
    actual = tuple(sorted(workflow.changed_paths(repository, assignment.input_candidate_commit, output_candidate_commit)))
    if not actual:
        _fail("ACCEPTANCE_SCOPE_VIOLATION", "repair candidate has no actual scoped changes")
    try:
        workflow.assert_allowed_changes(set(actual), assignment.allowed_paths)
    except RuntimeError as exc:
        if getattr(exc, "code", None) == "OUT_OF_SCOPE_CHANGE":
            _fail("ACCEPTANCE_SCOPE_VIOLATION", "actual repair diff escapes the immutable finding scope")
        raise
    for path in actual:
        if _path(path, "actual_changed_paths") != path:
            _fail("ACCEPTANCE_SCOPE_VIOLATION", "actual repair path is not normalized")
        listing = workflow.git(repository, "ls-tree", "-r", output_candidate_commit, "--", path)
        if listing.startswith("120000 "):
            _fail("ACCEPTANCE_SCOPE_VIOLATION", "repair candidate may not introduce a scoped symlink")
    reported = tuple(sorted(_path(path, "changed_paths") for path in _tuple(changed_paths, "changed_paths")))
    if len(set(reported)) != len(reported) or reported != actual:
        _fail("ACCEPTANCE_SCOPE_VIOLATION", "reported repair paths do not equal the actual Git diff")
    return actual


def _v2_reject_orphaned_direct_attempt(
    store: WorkflowStore,
    task_id: str,
    replay: _AcceptanceReplay,
    context: _TaskContext,
    assignment: AcceptanceAssignment,
) -> None:
    """Prevent a public completion API from adopting an old STARTED attempt."""

    if (
        replay.active_assignment_id == assignment.assignment_id
        and assignment.assignment_id in replay.started_receipts
    ):
        _v2_recover_orphaned_attempt(store, task_id, replay, context)
        _fail(
            "ASSIGNMENT_ATTEMPT_INTERRUPTED",
            "started assignment was recovered; issue a new capability before retrying",
        )


def record_adversarial_review(
    store: WorkflowStore,
    task_id: str,
    assignment: AcceptanceAssignment,
    reviewer_receipt: VerifiedActorReceipt,
    verdict: str,
    findings: Iterable[RepairFinding],
    evidence: AdversarialEvidence,
) -> None:
    """Reject caller-authored review completion outside controller execution."""

    with store.lock(task_id):
        replay = replay_acceptance_ledger(store, task_id)
        if replay is None:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance ledger is not open")
        context = _v2_context(store, task_id)
        _v2_reject_orphaned_direct_attempt(
            store, task_id, replay, context, assignment
        )
    _fail(
        "REPAIR_ADAPTER_REQUIRED",
        "review completion is emitted only by the controller-owned assignment executor",
    )


def complete_acceptance_assignment(
    store: WorkflowStore,
    task_id: str,
    assignment: AcceptanceAssignment,
    actor_receipt: VerifiedActorReceipt,
    output_candidate_commit: str,
    changed_paths: Iterable[str],
) -> None:
    """Reject caller-authored repair completion outside controller execution."""

    with store.lock(task_id):
        replay = replay_acceptance_ledger(store, task_id)
        if replay is None:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance ledger is not open")
        context = _v2_context(store, task_id)
        _v2_reject_orphaned_direct_attempt(
            store, task_id, replay, context, assignment
        )
    _fail(
        "REPAIR_ADAPTER_REQUIRED",
        "repair completion is emitted only by the controller-owned assignment executor",
    )


def _v2_recover_orphaned_attempt(
    store: WorkflowStore,
    task_id: str,
    replay: _AcceptanceReplay,
    context: _TaskContext,
) -> _AcceptanceReplay:
    """Consume an interrupted STARTED attempt exactly once before any restart."""

    assignment_id = replay.active_assignment_id
    if assignment_id is None or assignment_id not in replay.started_receipts:
        return replay
    assignment = replay.assignments[assignment_id]
    error = _workflow().WorkflowError(
        "ASSIGNMENT_ATTEMPT_INTERRUPTED",
        "controller restart recovered an orphaned started attempt; issue a new capability",
    )
    _v2_fail_attempt(store, task_id, replay, context, assignment, error)
    fresh = replay_acceptance_ledger(store, task_id)
    if fresh is None:
        raise AssertionError("recovered v2 ledger must replay")
    return fresh


def _v2_controller_snapshot(
    task: Mapping[str, object], replay: _AcceptanceReplay
) -> tuple[Path, object]:
    workflow = _workflow()
    repository = Path(task["repository_root"]).resolve()
    try:
        snapshot = workflow.capture_repo(repository)
    except RuntimeError:
        _fail("REPAIR_ADAPTER_REQUIRED", "controller cannot snapshot the assignment repository")
    if snapshot.status:
        _fail("REPAIR_ADAPTER_REQUIRED", "controller refuses a dirty assignment repository")
    if snapshot.head != replay.current_candidate_commit:
        _fail("REPAIR_ADAPTER_REQUIRED", "controller snapshot does not match the active candidate")
    return repository, snapshot


def _v2_controller_runtime_receipt(
    store: WorkflowStore,
    task_id: str,
    assignment: AcceptanceAssignment,
    task: Mapping[str, object],
    runtime_sessions_dir: Path,
) -> VerifiedActorReceipt:
    """Inspect and record the already-issued Codex runtime before resuming it."""

    workflow = _workflow()
    try:
        execution_surface, runtime_instance_id = assignment.expected_actor.identity.split(":", 1)
        parsed = uuid.UUID(runtime_instance_id)
    except (ValueError, AttributeError):
        _fail("REPAIR_ADAPTER_REQUIRED", "issued actor is not a canonical Codex runtime")
    if (
        execution_surface != "CODEX_EXEC_ROLE_CONTRACT"
        or str(parsed) != runtime_instance_id.lower()
    ):
        _fail("REPAIR_ADAPTER_REQUIRED", "fixed executor requires an issued Codex thread")
    sessions = Path(runtime_sessions_dir)
    if not sessions.is_absolute() or not sessions.is_dir() or sessions.is_symlink():
        _fail("REPAIR_ADAPTER_REQUIRED", "controller runtime sessions directory is invalid")
    role_runtime = {
        "luna": ("gpt-5.6-luna", "max", "workspace-write"),
        "terra_xhigh": ("gpt-5.6-terra", "xhigh", "workspace-write"),
        "terra_xhigh_reviewer": ("gpt-5.6-terra", "xhigh", "read-only"),
        "sol_medium_reviewer": (
            "gpt-5.6-sol",
            "medium",
            "assignment-scoped-write"
            if assignment.phase == "SOL_MEDIUM_REPAIR"
            else "read-only",
        ),
        "sol_xhigh": ("gpt-5.6-sol", "xhigh", "assignment-scoped-write"),
    }.get(assignment.expected_actor.role)
    if role_runtime is None:
        _fail("REPAIR_ADAPTER_REQUIRED", "issued role has no fixed controller runtime")
    model, effort, permission_profile = role_runtime
    sandbox = "read-only" if assignment.phase in _REVIEW_PHASES else "workspace-write"
    inspector = (
        Path(__file__).resolve().parents[1]
        / "plugins"
        / "ai-workflow"
        / "scripts"
        / "inspect-agent-runtime.sh"
    )
    observed = workflow.inspect_agent_runtime(
        sessions, runtime_instance_id, execution_surface, inspector
    )
    expected = {
        "execution_surface": execution_surface,
        "agent_type": None,
        "model": model,
        "reasoning_effort": effort,
        "sandbox_policy": sandbox,
        "permission_profile": permission_profile,
    }
    if any(observed.get(field) != value for field, value in expected.items()):
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "issued runtime does not match its capability")
    try:
        observed_cwd = Path(str(observed.get("cwd"))).resolve()
        task_cwd = Path(str(task["repository_root"])).resolve()
    except (OSError, TypeError):
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "issued runtime cwd is invalid")
    if observed_cwd != task_cwd:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "issued runtime does not match its capability")
    evidence = {
        "schema_version": "runtime-evidence-1",
        "attempt_id": assignment.attempt_id,
        "requested_role": assignment.expected_actor.role,
        "execution_surface": execution_surface,
        "observed_agent_type": None,
        "native_agent_id": None,
        "native_thread_id": None,
        "observed_model": model,
        "observed_reasoning_effort": effort,
        "observed_sandbox_policy": sandbox,
        "observed_permission_profile": permission_profile,
        "observed_cwd": str(observed_cwd),
        "evidence_source": "LOCAL_ROLLOUT",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "verification_status": "VERIFIED",
        "failure_reasons": [],
    }
    workflow.validate_runtime_evidence(evidence)
    evidence_sha256 = _v2_sha256(evidence)
    workflow.write_runtime_evidence(store, task_id, evidence)
    store.append_event(
        task_id,
        {
            "event_type": "RUNTIME_EVIDENCE_RECORDED",
            "attempt_id": assignment.attempt_id,
            "requested_role": assignment.expected_actor.role,
            "thread_id": runtime_instance_id,
            "execution_surface": execution_surface,
            "runtime_evidence_sha256": evidence_sha256,
        },
    )
    return VerifiedActorReceipt(
        assignment_id=assignment.assignment_id,
        execution_surface=execution_surface,
        runtime_instance_id=runtime_instance_id,
        attempt_id=assignment.attempt_id,
        requested_role=assignment.expected_actor.role,
        observed_model=model,
        observed_reasoning_effort=effort,
        observed_sandbox_policy=sandbox,
        observed_permission_profile=permission_profile,
        observed_cwd=str(observed_cwd),
        runtime_evidence_sha256=evidence_sha256,
        native_agent_uuid=None,
        codex_thread_id=runtime_instance_id,
    )


def _v2_assignment_prompt(
    task: Mapping[str, object], assignment: AcceptanceAssignment
) -> str:
    """Always emit the full assignment prompt; compact projection is not used here."""

    action = (
        "Review the pinned candidate and return an acceptance or rework recommendation."
        if assignment.phase in _REVIEW_PHASES
        else "Repair only the issued findings, commit one non-merge candidate, and report its files."
    )
    return "\n".join(
        (
            action,
            f"Task: {_v2_canonical(dict(task))}",
            f"Assignment: {_v2_canonical(_v2_assignment_payload_for_event(assignment))}",
            f'Output role exactly "{assignment.expected_actor.role}" using ai-result-1 JSON.',
            _workflow().RESULT_IDENTITY_PROMPT,
            "Never merge or push. Do not widen the immutable allowed paths.",
        )
    )


def _v2_validate_controller_result(
    result: object,
    assignment: AcceptanceAssignment,
    actual_changed_paths: set[str],
    *,
    expected_task_id: str | None = None,
) -> Mapping[str, object]:
    workflow = _workflow()
    if not isinstance(result, Mapping):
        _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "controller assignment result is not an object")
    result = workflow.normalize_result_identity(
        result,
        expected_task_id=expected_task_id,
        error_code="REPAIR_ADAPTER_INVALID_OUTPUT",
    )
    if set(result) != set(workflow.RESULT_REQUIRED_FIELDS):
        _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "controller assignment result shape is invalid")
    if result.get("schema_version") != "ai-result-1" or result.get("role") != assignment.expected_actor.role:
        _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "controller assignment result identity drifted")
    if assignment.phase in _REVIEW_PHASES:
        result = workflow.validate_role_result(
            assignment.expected_actor.role,
            result,
            actual_changed_paths,
            expected_task_id=expected_task_id,
        )
        if result.get("status") not in {
            "ACCEPTANCE_RECOMMENDED",
            "ACCEPTANCE_WITH_NOTES_RECOMMENDED",
            "REWORK_RECOMMENDED",
            "REJECT_RECOMMENDED",
        }:
            _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "review result has no bounded verdict")
        return result
    if result.get("status") != "IMPLEMENTED_CANDIDATE":
        _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "repair result did not produce a candidate")
    if not isinstance(result.get("summary"), str) or not str(result["summary"]).strip():
        _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "repair result summary is invalid")
    if not isinstance(result.get("recommended_next_state"), str) or not str(
        result["recommended_next_state"]
    ).strip():
        _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "repair result next state is invalid")
    for field in (
        "claims",
        "evidence",
        "counter_checks",
        "changed_files",
        "blind_spots",
        "unresolved_questions",
    ):
        if not isinstance(result.get(field), list):
            _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "repair result arrays are invalid")
    try:
        workflow._validate_result_records(
            result["claims"],
            "claims",
            frozenset({"id", "kind", "text", "evidence_ids"}),
        )
        if any(
            claim["kind"] not in {"FACT", "INFERENCE", "RECOMMENDATION"}
            for claim in result["claims"]
        ):
            _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "repair result claim kind is invalid")
        workflow._validate_result_records(
            result["evidence"],
            "evidence",
            frozenset({"id", "type", "locator", "observation"}),
            type_values=frozenset({"FILE", "COMMAND", "HASH", "TEST"}),
        )
        workflow._validate_result_records(
            result["counter_checks"],
            "counter_checks",
            frozenset({"target_claim_id", "method", "result"}),
        )
    except RuntimeError:
        _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "repair result records are invalid")
    for field in ("blind_spots", "unresolved_questions"):
        values = result[field]
        if any(not isinstance(value, str) for value in values) or len(set(values)) != len(values):
            _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "repair result text arrays are invalid")
    declared = result.get("changed_files")
    if (
        any(not isinstance(path, str) for path in declared)
        or len(set(declared)) != len(declared)
        or set(declared) != actual_changed_paths
    ):
        _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "repair result changed files drifted")
    return result


def run_assignment(
    store: WorkflowStore,
    task_id: str,
    assignment: AcceptanceAssignment,
    runtime_sessions_dir: Path | object,
    legacy_adapter: object | None = None,
) -> None:
    """Resume the issued runtime through the fixed controller execution path."""

    if legacy_adapter is not None:
        _fail(
            "REPAIR_ADAPTER_REQUIRED",
            "callers cannot supply an executable v2 assignment boundary",
        )
    if not isinstance(assignment, AcceptanceAssignment):
        _fail("REPAIR_ADAPTER_REQUIRED", "v2 boundary input is not an immutable assignment")
    caller_boundary = isinstance(runtime_sessions_dir, ControllerAssignmentBoundary)
    workflow = _workflow()
    started = False
    before_fs = None
    with store.lock(task_id):
        replay = replay_acceptance_ledger(store, task_id)
        if replay is None:
            _fail("REPAIR_ADAPTER_REQUIRED", "v2 acceptance ledger is not open")
        context = _v2_context(store, task_id)
        stored_task = _workflow().load_task(store._require_task(task_id) / "task.json")
        if (
            replay.active_assignment_id == assignment.assignment_id
            and assignment.assignment_id in replay.started_receipts
        ):
            _v2_recover_orphaned_attempt(store, task_id, replay, context)
            _fail(
                "ASSIGNMENT_ATTEMPT_INTERRUPTED",
                "started assignment was recovered; issue a new capability before retrying",
            )
        if caller_boundary:
            _fail(
                "REPAIR_ADAPTER_REQUIRED",
                "callers cannot supply an executable v2 assignment boundary",
            )
        if not isinstance(runtime_sessions_dir, (str, os.PathLike)):
            _fail("REPAIR_ADAPTER_REQUIRED", "fixed executor requires controller runtime sessions")
        repository, before = _v2_controller_snapshot(stored_task, replay)
        before_fs = capture_fs_snapshot(
            repository, exclusions=observation_exclusions(repository)
        )
        task_dir = store._require_task(task_id)
        output_path = task_dir / "attempts" / f"{assignment.attempt_id}-assignment-result.json"
        if output_path.exists():
            _fail("REPAIR_ADAPTER_REQUIRED", "controller assignment output path is not fresh")
        output_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = _v2_controller_runtime_receipt(
        store,
        task_id,
        assignment,
        stored_task,
        Path(runtime_sessions_dir),
    )
    attestation = ControllerExecutionAttestation(
        task_id=task_id,
        task_sha256=assignment.capability.task_sha256,
        assignment_id=assignment.assignment_id,
        capability_id=assignment.capability.capability_id,
        candidate_commit=assignment.input_candidate_commit,
        actor_receipt=receipt,
    )
    with store.lock(task_id):
        replay = replay_acceptance_ledger(store, task_id)
        if replay is None:
            _fail("REPAIR_ADAPTER_REQUIRED", "v2 acceptance ledger is not open")
        context = _v2_context(store, task_id)
        replay = _v2_start_attempt(
            store,
            task_id,
            replay,
            context,
            assignment,
            receipt,
            stored_task,
            attestation,
        )
        started = True
    try:
        role_config = workflow._load_role_config(assignment.expected_actor.role)
        model = role_config.get("model")
        effort = role_config.get("reasoning_effort")
        if not isinstance(model, str) or not isinstance(effort, str):
            _fail("REPAIR_ADAPTER_REQUIRED", "issued role runtime configuration is incomplete")
        codex = shutil.which("codex", path=os.environ.get("PATH", os.defpath))
        if not isinstance(codex, str):
            _fail("REPAIR_ADAPTER_REQUIRED", "controller Codex executable is unavailable")
        dispatch_schema_path = workflow.materialize_dispatch_result_schema(
            repository / "config" / "ai_workflow_result.schema.json",
            output_path.parent,
            f"{assignment.attempt_id}-assignment",
        )
        command = [
            codex,
            "exec",
            "resume",
            "-m",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "-c",
            f'sandbox_mode="{receipt.observed_sandbox_policy}"',
            "--json",
            "--output-schema",
            str(dispatch_schema_path),
            "-o",
            str(output_path),
            receipt.runtime_instance_id,
            "-",
        ]
        launched_ns = time.time_ns()
        try:
            completed = subprocess.run(
                command,
                cwd=repository,
                input=_v2_assignment_prompt(stored_task, assignment),
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                shell=False,
                env=workflow.sanitized_environment(os.environ),
            )
        except (OSError, subprocess.TimeoutExpired):
            record_unobserved_side_effect(
                store,
                task_id,
                role=assignment.expected_actor.role,
                permit_id=None,
                reason="unobserved-assignment",
            )
            _fail("REPAIR_ADAPTER_REQUIRED", "controller assignment process could not complete")
        if completed.returncode != 0:
            _fail("REPAIR_ADAPTER_REQUIRED", "controller assignment process failed")
        events = workflow.parse_codex_jsonl(completed.stdout)
        if workflow.extract_codex_thread_id(events) != receipt.runtime_instance_id:
            _fail("ACCEPTANCE_RECEIPT_MISMATCH", "resumed controller thread identity drifted")
        try:
            output_stat = output_path.stat()
            output = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "controller emitted no valid fresh result")
        if output_stat.st_mtime_ns < launched_ns:
            _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "controller result predates assignment launch")
        try:
            after = workflow.capture_repo(repository)
        except RuntimeError:
            _fail("REPAIR_ADAPTER_REQUIRED", "controller cannot snapshot the post-launch repository")
        if before_fs is not None:
            observe_execution_side_effects(
                store,
                task_id,
                role=assignment.expected_actor.role,
                permit_id=None,
                before=before_fs,
                after=capture_fs_snapshot(
                    repository, exclusions=observation_exclusions(repository)
                ),
                rollout_events=tuple(events),
            )
        if after.status:
            _fail("REPAIR_ADAPTER_REQUIRED", "controller rejects uncommitted assignment writes")
        if assignment.phase in _REVIEW_PHASES and after != before:
            _fail("REPAIR_ADAPTER_REQUIRED", "read-only review changed the repository snapshot")
        actual_changed_paths = set(
            workflow.changed_paths(
                repository, assignment.input_candidate_commit, after.head
            )
        ) if after.head != assignment.input_candidate_commit else set()
        output = _v2_validate_controller_result(
            output,
            assignment,
            actual_changed_paths,
            expected_task_id=stored_task["task_id"],
        )
        if assignment.phase in _REPAIR_PHASES:
            with store.lock(task_id):
                completion_replay = replay_acceptance_ledger(store, task_id)
                if completion_replay is None:
                    _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance ledger is not open")
                completion_context = _v2_context(store, task_id)
                _v2_require_issued_assignment(completion_replay, assignment)
                if completion_replay.started_receipts.get(assignment.assignment_id) != receipt:
                    _fail("ACCEPTANCE_SEQUENCE_INVALID", "controller repair receipt drifted")
                output_commit = _v2_sha(
                    after.head, "output_candidate_commit", length=40
                )
                reported_paths = output.get("changed_files")
                actual = _v2_validate_repair_output(
                    stored_task, assignment, output_commit, reported_paths
                )
                fields: dict[str, object] = {
                    "assignment_id": assignment.assignment_id,
                    "attempt_id": assignment.attempt_id,
                    "actor_receipt": _v2_receipt_payload(receipt),
                    "changed_paths": list(actual),
                    "actual_changed_paths": list(actual),
                    "output_candidate_commit": output_commit,
                }
                if assignment.phase == "SOL_XHIGH_TERMINAL_REPAIR":
                    fields.update(
                        {
                            "terminal_state": "TASK_TERMINAL",
                            "terminal_reason": "SOL_XHIGH_TERMINAL_REPAIR_COMPLETED",
                            "whole_project_acceptance_required": "PENDING",
                        }
                    )
                _v2_append(
                    store,
                    task_id,
                    completion_replay,
                    completion_context,
                    "REPAIR_COMPLETED",
                    output_commit,
                    fields,
                )
            return
        status = output.get("status")
        verdict = (
            "ACCEPT"
            if status in {"ACCEPTANCE_RECOMMENDED", "ACCEPTANCE_WITH_NOTES_RECOMMENDED"}
            else "REWORK"
        )
        findings: tuple[RepairFinding, ...] = ()
        if verdict == "REWORK":
            claim_ids = tuple(
                claim.get("id")
                for claim in output.get("claims", ())
                if isinstance(claim, Mapping) and isinstance(claim.get("id"), str)
            )
            if not claim_ids or len(set(claim_ids)) != len(claim_ids):
                _fail("REPAIR_ADAPTER_INVALID_OUTPUT", "rework result lacks canonical findings")
            allowed_paths = assignment.allowed_paths or tuple(stored_task["allowed_write_paths"])
            findings = tuple(
                RepairFinding(finding_id, allowed_paths) for finding_id in sorted(claim_ids)
            )
        evidence = execute_adversarial_evidence(store, task_id, after.head)
        with store.lock(task_id):
            completion_replay = replay_acceptance_ledger(store, task_id)
            if completion_replay is None:
                _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance ledger is not open")
            completion_context = _v2_context(store, task_id)
            _v2_require_issued_assignment(completion_replay, assignment)
            if completion_replay.started_receipts.get(assignment.assignment_id) != receipt:
                _fail("ACCEPTANCE_SEQUENCE_INVALID", "controller review receipt drifted")
            if assignment.phase not in _REVIEW_PHASES:
                _fail("ACCEPTANCE_SEQUENCE_INVALID", "only review phases may record a verdict")
            frozen_findings = _v2_findings(findings)
            if verdict == "ACCEPT" and frozen_findings:
                _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance may not contain findings")
            if verdict == "REWORK":
                _v2_assert_findings_within_task(frozen_findings, stored_task)
                if assignment.phase != "REVIEW_1" and not set(
                    _v2_allowed_paths(frozen_findings)
                ).issubset(assignment.allowed_paths):
                    _fail("ACCEPTANCE_SEQUENCE_INVALID", "rework expanded immutable scope")
            fields = {
                "assignment_id": assignment.assignment_id,
                "attempt_id": assignment.attempt_id,
                "reviewer_receipt": _v2_receipt_payload(receipt),
                "verdict": verdict,
                "findings": _v2_findings_payload(frozen_findings),
                "evidence": asdict(evidence),
                "evidence_sha256": _v2_sha256(asdict(evidence)),
            }
            if verdict == "ACCEPT":
                fields.update(
                    {
                        "terminal_state": "TASK_TERMINAL",
                        "terminal_reason": f"{assignment.phase}_ACCEPTED",
                        "whole_project_acceptance_required": "PENDING",
                    }
                )
            _v2_append(
                store,
                task_id,
                completion_replay,
                completion_context,
                "REVIEW_COMPLETED",
                completion_replay.current_candidate_commit,
                fields,
            )
    except BaseException as exc:
        with store.lock(task_id):
            latest = replay_acceptance_ledger(store, task_id)
            if started and (
                latest is not None
                and latest.active_assignment_id == assignment.assignment_id
                and assignment.assignment_id in latest.started_receipts
            ):
                _v2_fail_attempt(store, task_id, latest, context, assignment, exc)
            raise


def repair_ledger_claims_task(store: WorkflowStore, task_id: str) -> bool:
    """True only for a valid v2 ledger; v1 history never claims this adapter."""

    return replay_acceptance_ledger(store, task_id) is not None


def has_active_repair_assignment(store: WorkflowStore, task_id: str) -> bool:
    """Return whether an uncompleted immutable repair still owns execution.

    Callers that already hold the task lock may use this without re-locking.
    It intentionally exposes no synthetic actor identity: the existing runner
    API has no identity channel from which a round-three Sol fixer could be
    verified safely.
    """

    workflow = _workflow()
    records = workflow._load_event_records(store, task_id)
    if not any(record.get("event_type") in _REPAIR_EVENT_TYPES for record in records):
        return False
    context = _task_context(store, task_id)
    replay = _replay(store, task_id, context)
    if not replay.assignments:
        return False
    latest_round = max(replay.assignments)
    return replay.reviews.get(latest_round) not in _ACCEPTANCE_VERDICTS
