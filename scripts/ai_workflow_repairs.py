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
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
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
    verification_commands: tuple[str, ...]
    negative_checks: tuple[str, ...]
    outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in ("verification_commands", "negative_checks", "outputs"):
            values = _tuple(getattr(self, field), field)
            if not values or any(not isinstance(value, str) or not value.strip() for value in values):
                _fail("ACCEPTANCE_EVIDENCE_INVALID", f"{field} must contain non-empty evidence")
            object.__setattr__(self, field, tuple(values))


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
    finished_assignment_ids: set[str]
    repairer_identities: dict[str, str]


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
    if any("ledger_version" in record and record.get("ledger_version") != _ACCEPTANCE_LEDGER_VERSION for record in records):
        _fail("ACCEPTANCE_LEDGER_INVALID", "unknown acceptance ledger version")
    return v2


def _v2_context(store: WorkflowStore, task_id: str) -> _TaskContext:
    return _task_context(store, task_id)


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


def _v2_expected_receipt_attempt(actor: ActorIdentity) -> str:
    identity = actor.identity
    if ":" not in identity:
        _fail("ACCEPTANCE_LEDGER_INVALID", "expected actor identity is not runtime-bound")
    _, runtime = identity.split(":", 1)
    label = runtime.removeprefix("runtime-")
    return f"{label}-attempt-1"


def _v2_validate_observed_receipt(receipt: VerifiedActorReceipt, task: Mapping[str, object]) -> None:
    expected = {
        "luna": ("gpt-5.6-luna", "max", "workspace-write", "workspace-write"),
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


def _v2_validate_assignment_receipt(
    assignment: AcceptanceAssignment,
    receipt: VerifiedActorReceipt,
    task: Mapping[str, object],
    owner_actor: ActorIdentity,
) -> None:
    if receipt.assignment_id != assignment.assignment_id:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt belongs to another assignment")
    _v2_validate_observed_receipt(receipt, task)
    if assignment.phase == "OWNER_REPAIR":
        if receipt.requested_role != owner_actor.role:
            _fail("ACCEPTANCE_RECEIPT_MISMATCH", "owner repair changed the owner role")
        return
    if receipt.actor_identity != assignment.expected_actor:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt identity does not match the issued actor")
    if receipt.attempt_id != _v2_expected_receipt_attempt(assignment.expected_actor):
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt attempt does not match its issued actor")
    _, runtime = assignment.expected_actor.identity.split(":", 1)
    label = runtime.removeprefix("runtime-")
    expected_identity = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{'native' if receipt.execution_surface == 'NATIVE_SUBAGENT' else 'codex'}:{label}:{runtime}",
        )
    )
    observed_identity = (
        receipt.native_agent_uuid
        if receipt.execution_surface == "NATIVE_SUBAGENT"
        else receipt.codex_thread_id
    )
    if observed_identity != expected_identity:
        _fail("ACCEPTANCE_RECEIPT_MISMATCH", "receipt runtime identity source is not issuance-bound")


def _v2_evidence(value: object) -> AdversarialEvidence:
    if not isinstance(value, Mapping) or set(value) != {
        "verification_commands", "negative_checks", "outputs"
    }:
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "review evidence shape is invalid")
    try:
        return AdversarialEvidence(
            tuple(value["verification_commands"]),
            tuple(value["negative_checks"]),
            tuple(value["outputs"]),
        )
    except (TypeError, RuntimeError):
        _fail("ACCEPTANCE_EVIDENCE_INVALID", "review evidence is invalid")
    raise AssertionError("unreachable")


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
        if owner_receipt.requested_role not in {"luna", "terra_xhigh"}:
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

    context = _v2_context(store, task_id)
    records = _v2_event_records(store, task_id)
    if not records:
        return None
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
            _v2_validate_observed_receipt(owner_receipt, _workflow().load_task(store._require_task(task_id) / "task.json"))
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
                set(),
                {},
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
                _v2_validate_assignment_receipt(assignment, receipt, stored_task, replay.owner_actor)
                if event.get("receipt_sha256") != _v2_sha256(_v2_receipt_payload(receipt)):
                    _fail("ACCEPTANCE_LEDGER_INVALID", "attempt receipt binding drifted")
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


def _v2_next_phase(replay: _AcceptanceReplay) -> tuple[str, tuple[RepairFinding, ...]]:
    if replay.terminal or replay.active_assignment_id is not None:
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance ladder has no issuable step")
    outcomes = replay.phase_outcomes
    if "REVIEW_1" not in outcomes:
        return "REVIEW_1", ()
    if outcomes["REVIEW_1"] != "REWORK":
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "accepted review already terminally closed the task")
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


def _v2_validate_phase_actor(
    replay: _AcceptanceReplay, assignment: AcceptanceAssignment
) -> None:
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
        expected_phase, findings = _v2_next_phase(replay)
        if phase != expected_phase:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "requested phase does not match the capped repair ladder")
        context = _v2_context(store, task_id)
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
) -> _AcceptanceReplay:
    _v2_require_issued_assignment(replay, assignment)
    _v2_validate_assignment_receipt(assignment, receipt, stored_task, replay.owner_actor)
    if assignment.assignment_id in replay.started_receipts:
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "assignment attempt has already started")
    _v2_append(
        store,
        task_id,
        replay,
        context,
        "ASSIGNMENT_ATTEMPT_STARTED",
        replay.current_candidate_commit,
        {
            "assignment_id": assignment.assignment_id,
            "attempt_id": assignment.attempt_id,
            "actor_receipt": _v2_receipt_payload(receipt),
            "receipt_sha256": _v2_sha256(_v2_receipt_payload(receipt)),
        },
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


def record_adversarial_review(
    store: WorkflowStore,
    task_id: str,
    assignment: AcceptanceAssignment,
    reviewer_receipt: VerifiedActorReceipt,
    verdict: str,
    findings: Iterable[RepairFinding],
    evidence: AdversarialEvidence,
) -> None:
    """Record one independent review with exact findings and evidence binding."""

    if not isinstance(assignment, AcceptanceAssignment) or not isinstance(
        reviewer_receipt, VerifiedActorReceipt
    ) or not isinstance(evidence, AdversarialEvidence):
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "review input is not a verified v2 value")
    with store.lock(task_id):
        replay = replay_acceptance_ledger(store, task_id)
        if replay is None:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance ledger is not open")
        context = _v2_context(store, task_id)
        stored_task = _workflow().load_task(store._require_task(task_id) / "task.json")
        replay = _v2_start_attempt(
            store, task_id, replay, context, assignment, reviewer_receipt, stored_task
        )
        try:
            _v2_require_issued_assignment(replay, assignment)
            if assignment.phase not in _REVIEW_PHASES or verdict not in {"ACCEPT", "REWORK"}:
                _fail("ACCEPTANCE_SEQUENCE_INVALID", "review verdict or phase is invalid")
            frozen_findings = _v2_findings(findings)
            if verdict == "ACCEPT" and frozen_findings:
                _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance may not contain open findings")
            if verdict == "REWORK":
                if not frozen_findings:
                    _fail("ACCEPTANCE_SEQUENCE_INVALID", "rework requires non-empty canonical findings")
                _v2_assert_findings_within_task(frozen_findings, stored_task)
                if assignment.phase != "REVIEW_1" and not set(_v2_allowed_paths(frozen_findings)).issubset(
                    assignment.allowed_paths
                ):
                    _fail("ACCEPTANCE_SEQUENCE_INVALID", "rework may not expand a review's immutable scope")
            terminal = verdict == "ACCEPT"
            fields: dict[str, object] = {
                "assignment_id": assignment.assignment_id,
                "attempt_id": assignment.attempt_id,
                "reviewer_receipt": _v2_receipt_payload(reviewer_receipt),
                "verdict": verdict,
                "findings": _v2_findings_payload(frozen_findings),
                "evidence": asdict(evidence),
                "evidence_sha256": _v2_sha256(asdict(evidence)),
            }
            if terminal:
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
                replay,
                context,
                "REVIEW_COMPLETED",
                replay.current_candidate_commit,
                fields,
            )
        except RuntimeError as exc:
            _v2_fail_attempt(store, task_id, replay, context, assignment, exc)
            raise


def complete_acceptance_assignment(
    store: WorkflowStore,
    task_id: str,
    assignment: AcceptanceAssignment,
    actor_receipt: VerifiedActorReceipt,
    output_candidate_commit: str,
    changed_paths: Iterable[str],
) -> None:
    """Accept one actual Git-bounded repair result for the active capability."""

    if not isinstance(assignment, AcceptanceAssignment) or not isinstance(
        actor_receipt, VerifiedActorReceipt
    ):
        _fail("ACCEPTANCE_SEQUENCE_INVALID", "repair input is not a verified v2 value")
    with store.lock(task_id):
        replay = replay_acceptance_ledger(store, task_id)
        if replay is None:
            _fail("ACCEPTANCE_SEQUENCE_INVALID", "acceptance ledger is not open")
        context = _v2_context(store, task_id)
        stored_task = _workflow().load_task(store._require_task(task_id) / "task.json")
        replay = _v2_start_attempt(
            store, task_id, replay, context, assignment, actor_receipt, stored_task
        )
        try:
            _v2_require_issued_assignment(replay, assignment)
            if assignment.phase not in _REPAIR_PHASES:
                _fail("ACCEPTANCE_SEQUENCE_INVALID", "only repair phases may complete a candidate")
            output = _v2_sha(output_candidate_commit, "output_candidate_commit", length=40)
            actual = _v2_validate_repair_output(stored_task, assignment, output, changed_paths)
            fields: dict[str, object] = {
                "assignment_id": assignment.assignment_id,
                "attempt_id": assignment.attempt_id,
                "actor_receipt": _v2_receipt_payload(actor_receipt),
                "changed_paths": list(tuple(sorted(_path(path, "changed_paths") for path in _tuple(changed_paths, "changed_paths")))),
                "actual_changed_paths": list(actual),
                "output_candidate_commit": output,
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
                replay,
                context,
                "REPAIR_COMPLETED",
                output,
                fields,
            )
        except RuntimeError as exc:
            _v2_fail_attempt(store, task_id, replay, context, assignment, exc)
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
