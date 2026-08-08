"""Immutable, append-only repair assignments for the Terra OS workflow.

This module deliberately owns only the repair ledger.  It does not widen any
role's normal repository permissions or alter the versioned workflow wires.
The workflow module imports these helpers after it has defined its store and
error type; lazy access below avoids an import cycle for direct script use.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from .ai_workflow import WorkflowStore


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


def _workflow():
    try:
        from . import ai_workflow as workflow
    except (ImportError, ModuleNotFoundError):
        import ai_workflow as workflow
    return workflow


def _fail(code: str, message: str) -> None:
    raise _workflow().WorkflowError(code, message)


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


def _task_context(store: WorkflowStore, task_id: str) -> _TaskContext:
    workflow = _workflow()
    task = workflow.load_task(store._require_task(task_id) / "task.json")
    if task.get("task_type") != "REMEDIATION":
        _fail("REPAIR_INPUT_INVALID", "repairs require a REMEDIATION task")
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
