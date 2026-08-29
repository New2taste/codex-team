"""Versioned owner authorizations with per-kind content IDs and transfer leases."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    from .ai_workflow_artifacts import (
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        canonical_json,
        content_id,
        load_artifact,
        sorted_strs,
        verify_content_id,
    )
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        canonical_json,
        content_id,
        load_artifact,
        sorted_strs,
        verify_content_id,
    )


OWNER_AUTHORIZATION_SCHEMA_VERSION = "ai-owner-authorization-1"
OWNER_AUTHORIZATION_LEDGER = "owner-authorizations.jsonl"
OWNER_EVIDENCE_LEDGER = "human-decisions.jsonl"
AUTHORIZATION_TYPES = frozenset({"VERDICT_STALE_OVERRIDE", "OWNERSHIP_TRANSFER"})
AUTHORIZATION_RECORD_KINDS = frozenset({"authorization", "consumption", "transfer_lease"})
OWNER_AUTHORIZATION_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "authorization_id",
        "record_id",
        "authorization_type",
        "task_id",
        "envelope_hash",
        "candidate_state_digest",
        "path",
        "from_role",
        "to_role",
        "allowed_paths",
        "max_dispatches",
        "permit_id",
        "dispatch_seq",
        "binding",
        "actor",
        "owner_evidence_id",
        "issued_at_utc",
    }
)
AUTHORIZATION_ID_EXCLUDE = frozenset({"authorization_id"})
RECORD_ID_EXCLUDE = frozenset({"record_id"})
_AUTHORIZATION_COMMON_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "authorization_id",
        "authorization_type",
        "task_id",
        "envelope_hash",
        "actor",
        "owner_evidence_id",
        "issued_at_utc",
    }
)
_OVERRIDE_FIELDS = _AUTHORIZATION_COMMON_FIELDS | frozenset({"candidate_state_digest"})
_TRANSFER_FIELDS = _AUTHORIZATION_COMMON_FIELDS | frozenset(
    {"path", "from_role", "to_role", "allowed_paths", "max_dispatches"}
)
_CONSUMPTION_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "record_id",
        "authorization_id",
        "task_id",
        "envelope_hash",
        "binding",
        "issued_at_utc",
    }
)
_LEASE_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "record_id",
        "authorization_id",
        "task_id",
        "envelope_hash",
        "permit_id",
        "dispatch_seq",
        "allowed_paths",
        "issued_at_utc",
    }
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_OWNER_DECISION_EVENT = "OWNER_DECISION"


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        _fail("INVALID_TYPE", f"{field} must be a string")
    if not value.strip():
        _fail("EMPTY_FIELD", f"{field} must not be empty")
    return value


def _hex_digest(value: object, field: str) -> str:
    text = _string(value, field)
    if not _HEX64.fullmatch(text):
        _fail("INVALID_TYPE", f"{field} must be a lowercase hexadecimal digest")
    return text


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("INVALID_TYPE", f"{field} must be a positive integer")
    if value < 1:
        _fail("INVALID_TYPE", f"{field} must be a positive integer")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _closed_fields(payload: Mapping[str, object], expected: frozenset[str]) -> None:
    unknown = sorted(set(payload) - expected)
    if unknown:
        _fail("UNKNOWN_FIELD", f"unsupported field {unknown[0]}")
    missing = sorted(expected - set(payload))
    if missing:
        _fail("MISSING_FIELD", f"missing field {missing[0]}")


def _allowed_paths(value: object) -> list[str]:
    paths = sorted_strs(value)
    if any(not item.strip() for item in paths):
        _fail("EMPTY_FIELD", "allowed_paths items must not be empty")
    return paths


def _authorization_preimage(record: Mapping[str, object]) -> dict[str, object]:
    payload = dict(record)
    if "allowed_paths" in payload:
        payload["allowed_paths"] = sorted_strs(payload["allowed_paths"])
    return payload


def _record_preimage(record: Mapping[str, object]) -> dict[str, object]:
    payload = dict(record)
    if "allowed_paths" in payload:
        payload["allowed_paths"] = sorted_strs(payload["allowed_paths"])
    return payload


@dataclass(frozen=True)
class OwnerAuthorization:
    schema_version: str
    record_kind: str
    authorization_id: str
    authorization_type: str
    task_id: str
    envelope_hash: str
    actor: str
    owner_evidence_id: str
    issued_at_utc: str
    candidate_state_digest: str | None = None
    path: str | None = None
    from_role: str | None = None
    to_role: str | None = None
    allowed_paths: tuple[str, ...] | None = None
    max_dispatches: int | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "record_kind": self.record_kind,
            "authorization_id": self.authorization_id,
            "authorization_type": self.authorization_type,
            "task_id": self.task_id,
            "envelope_hash": self.envelope_hash,
            "actor": self.actor,
            "owner_evidence_id": self.owner_evidence_id,
            "issued_at_utc": self.issued_at_utc,
        }
        if self.candidate_state_digest is not None:
            payload["candidate_state_digest"] = self.candidate_state_digest
        if self.path is not None:
            payload["path"] = self.path
        if self.from_role is not None:
            payload["from_role"] = self.from_role
        if self.to_role is not None:
            payload["to_role"] = self.to_role
        if self.allowed_paths is not None:
            payload["allowed_paths"] = list(self.allowed_paths)
        if self.max_dispatches is not None:
            payload["max_dispatches"] = self.max_dispatches
        return payload


def validate_owner_authorization(value: Mapping[str, object]) -> None:
    if not isinstance(value, Mapping):
        _fail("INVALID_TYPE", "owner authorization must be an object")
    payload = dict(value)
    kind = payload.get("record_kind")
    if "record_kind" not in payload:
        _fail("MISSING_FIELD", "missing field record_kind")
    if kind not in AUTHORIZATION_RECORD_KINDS:
        _fail("INVALID_ENUM", "record_kind is not supported")
    if kind == "authorization":
        auth_type = payload.get("authorization_type")
        if auth_type not in AUTHORIZATION_TYPES:
            _fail("INVALID_ENUM", "authorization_type is not supported")
        expected = (
            _OVERRIDE_FIELDS
            if auth_type == "VERDICT_STALE_OVERRIDE"
            else _TRANSFER_FIELDS
        )
        _closed_fields(payload, expected)
        if payload.get("schema_version") != OWNER_AUTHORIZATION_SCHEMA_VERSION:
            _fail(
                "SCHEMA_VERSION",
                f"schema_version must be {OWNER_AUTHORIZATION_SCHEMA_VERSION}",
            )
        _hex_digest(payload["authorization_id"], "authorization_id")
        _string(payload["task_id"], "task_id")
        _hex_digest(payload["envelope_hash"], "envelope_hash")
        _string(payload["actor"], "actor")
        _hex_digest(payload["owner_evidence_id"], "owner_evidence_id")
        _string(payload["issued_at_utc"], "issued_at_utc")
        if auth_type == "VERDICT_STALE_OVERRIDE":
            _hex_digest(payload["candidate_state_digest"], "candidate_state_digest")
        else:
            _string(payload["path"], "path")
            _string(payload["from_role"], "from_role")
            _string(payload["to_role"], "to_role")
            paths = _allowed_paths(payload["allowed_paths"])
            if not paths:
                _fail("EMPTY_ARRAY", "allowed_paths must not be empty")
            _positive_int(payload["max_dispatches"], "max_dispatches")
        return
    if kind == "consumption":
        _closed_fields(payload, _CONSUMPTION_FIELDS)
        if payload.get("schema_version") != OWNER_AUTHORIZATION_SCHEMA_VERSION:
            _fail(
                "SCHEMA_VERSION",
                f"schema_version must be {OWNER_AUTHORIZATION_SCHEMA_VERSION}",
            )
        _hex_digest(payload["record_id"], "record_id")
        _hex_digest(payload["authorization_id"], "authorization_id")
        _string(payload["task_id"], "task_id")
        _hex_digest(payload["envelope_hash"], "envelope_hash")
        if not isinstance(payload["binding"], Mapping):
            _fail("INVALID_TYPE", "binding must be an object")
        _string(payload["issued_at_utc"], "issued_at_utc")
        return
    _closed_fields(payload, _LEASE_FIELDS)
    if payload.get("schema_version") != OWNER_AUTHORIZATION_SCHEMA_VERSION:
        _fail(
            "SCHEMA_VERSION",
            f"schema_version must be {OWNER_AUTHORIZATION_SCHEMA_VERSION}",
        )
    _hex_digest(payload["record_id"], "record_id")
    _hex_digest(payload["authorization_id"], "authorization_id")
    _string(payload["task_id"], "task_id")
    _hex_digest(payload["envelope_hash"], "envelope_hash")
    _string(payload["permit_id"], "permit_id")
    _positive_int(payload["dispatch_seq"], "dispatch_seq")
    _allowed_paths(payload["allowed_paths"])
    _string(payload["issued_at_utc"], "issued_at_utc")


def compute_authorization_id(record: Mapping[str, object]) -> str:
    return content_id(
        OWNER_AUTHORIZATION_SCHEMA_VERSION,
        _authorization_preimage(record),
        exclude=AUTHORIZATION_ID_EXCLUDE,
    )


def verify_authorization_id(record: Mapping[str, object]) -> None:
    verify_content_id(
        OWNER_AUTHORIZATION_SCHEMA_VERSION,
        _authorization_preimage(record),
        exclude=AUTHORIZATION_ID_EXCLUDE,
        id_field="authorization_id",
    )


def compute_record_id(record: Mapping[str, object]) -> str:
    return content_id(
        OWNER_AUTHORIZATION_SCHEMA_VERSION,
        _record_preimage(record),
        exclude=RECORD_ID_EXCLUDE,
    )


def verify_record_id(record: Mapping[str, object]) -> None:
    verify_content_id(
        OWNER_AUTHORIZATION_SCHEMA_VERSION,
        _record_preimage(record),
        exclude=RECORD_ID_EXCLUDE,
        id_field="record_id",
    )


def _authorization_from_mapping(value: Mapping[str, object]) -> OwnerAuthorization:
    validate_owner_authorization(value)
    if value.get("record_kind") != "authorization":
        _fail("INVALID_ENUM", "owner authorization record_kind must be authorization")
    allowed = value.get("allowed_paths")
    return OwnerAuthorization(
        schema_version=_string(value["schema_version"], "schema_version"),
        record_kind=_string(value["record_kind"], "record_kind"),
        authorization_id=_hex_digest(value["authorization_id"], "authorization_id"),
        authorization_type=_string(value["authorization_type"], "authorization_type"),
        task_id=_string(value["task_id"], "task_id"),
        envelope_hash=_hex_digest(value["envelope_hash"], "envelope_hash"),
        actor=_string(value["actor"], "actor"),
        owner_evidence_id=_hex_digest(value["owner_evidence_id"], "owner_evidence_id"),
        issued_at_utc=_string(value["issued_at_utc"], "issued_at_utc"),
        candidate_state_digest=(
            _hex_digest(value["candidate_state_digest"], "candidate_state_digest")
            if "candidate_state_digest" in value
            else None
        ),
        path=_string(value["path"], "path") if "path" in value else None,
        from_role=_string(value["from_role"], "from_role") if "from_role" in value else None,
        to_role=_string(value["to_role"], "to_role") if "to_role" in value else None,
        allowed_paths=tuple(_allowed_paths(allowed)) if allowed is not None else None,
        max_dispatches=(
            _positive_int(value["max_dispatches"], "max_dispatches")
            if "max_dispatches" in value
            else None
        ),
    )


def _task_envelope_hash(store: TaskStoreProtocol, task_id: str) -> str:
    task = load_artifact(store._require_task(task_id) / "task.json")
    if task.get("task_id") != task_id:
        _fail("INVALID_TASK", "task.json task_id does not match")
    return artifact_sha256(task)


def _find_owner_evidence(
    store: TaskStoreProtocol, task_id: str, owner_evidence_id: str, actor: str
) -> Mapping[str, object]:
    evidence_id = _hex_digest(owner_evidence_id, "owner_evidence_id")
    actor_text = _string(actor, "actor")
    try:
        rows = store.read_task_ledger(task_id, OWNER_EVIDENCE_LEDGER)
    except WorkflowError as exc:
        if str(exc.code).endswith("_CORRUPT"):
            _fail("AUTHORIZATION_EVIDENCE_UNKNOWN", exc.message)
        raise
    for row in rows:
        if artifact_sha256(row) != evidence_id:
            continue
        if row.get("event_type") != _OWNER_DECISION_EVENT:
            _fail(
                "AUTHORIZATION_EVIDENCE_UNKNOWN",
                "owner evidence is not an OWNER_DECISION record",
            )
        if row.get("actor") != actor_text:
            _fail(
                "AUTHORIZATION_EVIDENCE_ACTOR_MISMATCH",
                "owner evidence actor does not match",
            )
        return row
    _fail("AUTHORIZATION_EVIDENCE_UNKNOWN", "owner evidence is not in this task ledger")
    raise AssertionError("unreachable")


def _scope_binding(authorization: OwnerAuthorization) -> dict[str, object]:
    if authorization.authorization_type == "VERDICT_STALE_OVERRIDE":
        return {"candidate_state_digest": authorization.candidate_state_digest}
    return {
        "path": authorization.path,
        "from_role": authorization.from_role,
        "to_role": authorization.to_role,
        "allowed_paths": list(authorization.allowed_paths or ()),
        "max_dispatches": authorization.max_dispatches,
    }


def _canonical_binding(value: Mapping[str, object]) -> str:
    payload = dict(value)
    if "allowed_paths" in payload:
        payload["allowed_paths"] = sorted_strs(payload["allowed_paths"])
    return canonical_json(payload)


def _read_authorization_rows(
    store: TaskStoreProtocol, task_id: str
) -> tuple[dict[str, object], ...]:
    try:
        return store.read_task_ledger(task_id, OWNER_AUTHORIZATION_LEDGER)
    except WorkflowError as exc:
        if str(exc.code).endswith("_CORRUPT"):
            _fail("AUTHORIZATION_LEDGER_CORRUPT", exc.message)
        raise


def replay_authorizations(
    store: TaskStoreProtocol, task_id: str
) -> tuple[dict[str, object], ...]:
    rows = _read_authorization_rows(store, task_id)
    seen_record_ids: set[str] = set()
    seen_authorization_ids: set[str] = set()
    lease_seq: dict[str, int] = {}
    replayed: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            _fail("AUTHORIZATION_LEDGER_CORRUPT", "authorization record must be an object")
        payload = dict(row)
        if payload.get("task_id") != task_id:
            _fail("AUTHORIZATION_LEDGER_CORRUPT", "authorization record task_id does not match")
        kind = payload.get("record_kind")
        try:
            if kind == "authorization":
                verify_authorization_id(payload)
            elif kind in {"consumption", "transfer_lease"}:
                verify_record_id(payload)
            else:
                _fail("AUTHORIZATION_LEDGER_CORRUPT", "record_kind is not supported")
            validate_owner_authorization(payload)
        except WorkflowError as exc:
            if exc.code == "AUTHORIZATION_LEDGER_CORRUPT":
                raise
            _fail("AUTHORIZATION_LEDGER_CORRUPT", exc.message)
        record_id = payload.get("record_id")
        if isinstance(record_id, str):
            if record_id in seen_record_ids:
                _fail("AUTHORIZATION_LEDGER_CORRUPT", "duplicate record_id")
            seen_record_ids.add(record_id)
        if kind == "authorization":
            authorization_id = _string(payload.get("authorization_id"), "authorization_id")
            if authorization_id in seen_authorization_ids:
                _fail(
                    "AUTHORIZATION_LEDGER_CORRUPT",
                    "duplicate authorization record",
                )
            seen_authorization_ids.add(authorization_id)
        if kind == "transfer_lease":
            authorization_id = _string(payload.get("authorization_id"), "authorization_id")
            expected = lease_seq.get(authorization_id, 0) + 1
            if payload.get("dispatch_seq") != expected:
                _fail(
                    "AUTHORIZATION_LEDGER_CORRUPT",
                    "dispatch_seq is not locally contiguous",
                )
            lease_seq[authorization_id] = expected
        replayed.append(payload)
    return tuple(replayed)


def issue_owner_authorization(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    authorization_type: str,
    actor: str,
    owner_evidence_id: str,
    issued_at_utc: str,
    candidate_state_digest: str | None = None,
    path: str | None = None,
    from_role: str | None = None,
    to_role: str | None = None,
    allowed_paths: tuple[str, ...] | None = None,
    max_dispatches: int | None = None,
) -> OwnerAuthorization:
    if authorization_type not in AUTHORIZATION_TYPES:
        _fail("INVALID_ENUM", "authorization_type is not supported")
    actor_text = _string(actor, "actor")
    _find_owner_evidence(store, task_id, owner_evidence_id, actor_text)
    record: dict[str, object] = {
        "schema_version": OWNER_AUTHORIZATION_SCHEMA_VERSION,
        "record_kind": "authorization",
        "authorization_id": "",
        "authorization_type": authorization_type,
        "task_id": task_id,
        "envelope_hash": _task_envelope_hash(store, task_id),
        "actor": actor_text,
        "owner_evidence_id": _hex_digest(owner_evidence_id, "owner_evidence_id"),
        "issued_at_utc": _string(issued_at_utc, "issued_at_utc"),
    }
    if authorization_type == "VERDICT_STALE_OVERRIDE":
        if candidate_state_digest is None:
            _fail("MISSING_FIELD", "missing field candidate_state_digest")
        if any(
            item is not None
            for item in (path, from_role, to_role, allowed_paths, max_dispatches)
        ):
            _fail("UNKNOWN_FIELD", "unsupported field for VERDICT_STALE_OVERRIDE")
        record["candidate_state_digest"] = _hex_digest(
            candidate_state_digest, "candidate_state_digest"
        )
    else:
        if candidate_state_digest is not None:
            _fail("UNKNOWN_FIELD", "unsupported field candidate_state_digest")
        if path is None:
            _fail("MISSING_FIELD", "missing field path")
        if from_role is None:
            _fail("MISSING_FIELD", "missing field from_role")
        if to_role is None:
            _fail("MISSING_FIELD", "missing field to_role")
        if allowed_paths is None:
            _fail("MISSING_FIELD", "missing field allowed_paths")
        if max_dispatches is None:
            _fail("MISSING_FIELD", "missing field max_dispatches")
        paths = _allowed_paths(allowed_paths)
        if not paths:
            _fail("EMPTY_ARRAY", "allowed_paths must not be empty")
        record["path"] = _string(path, "path")
        record["from_role"] = _string(from_role, "from_role")
        record["to_role"] = _string(to_role, "to_role")
        record["allowed_paths"] = paths
        record["max_dispatches"] = _positive_int(max_dispatches, "max_dispatches")
    record["authorization_id"] = compute_authorization_id(record)
    verify_authorization_id(record)
    validate_owner_authorization(record)
    store.append_task_ledger(task_id, OWNER_AUTHORIZATION_LEDGER, record)
    return _authorization_from_mapping(record)


def load_owner_authorization(
    store: TaskStoreProtocol, task_id: str, authorization_id: str
) -> OwnerAuthorization | None:
    wanted = _hex_digest(authorization_id, "authorization_id")
    for row in replay_authorizations(store, task_id):
        if row.get("record_kind") != "authorization":
            continue
        if row.get("authorization_id") == wanted:
            return _authorization_from_mapping(row)
    return None


def consume_owner_authorization_locked(
    store: TaskStoreProtocol,
    task_id: str,
    authorization_id: str,
    *,
    binding: Mapping[str, object],
) -> OwnerAuthorization:
    store._assert_lock_held(task_id)
    if not isinstance(binding, Mapping):
        _fail("INVALID_TYPE", "binding must be an object")
    authorization = load_owner_authorization(store, task_id, authorization_id)
    if authorization is None:
        _fail("AUTHORIZATION_UNKNOWN", "authorization is not in this task ledger")
        raise AssertionError("unreachable")
    for row in replay_authorizations(store, task_id):
        if (
            row.get("record_kind") == "consumption"
            and row.get("authorization_id") == authorization.authorization_id
        ):
            _fail("AUTHORIZATION_CONSUMED", "authorization has already been consumed")
    if _canonical_binding(binding) != _canonical_binding(_scope_binding(authorization)):
        _fail("AUTHORIZATION_SCOPE_MISMATCH", "binding does not match authorization scope")
    record: dict[str, object] = {
        "schema_version": OWNER_AUTHORIZATION_SCHEMA_VERSION,
        "record_kind": "consumption",
        "record_id": "",
        "authorization_id": authorization.authorization_id,
        "task_id": task_id,
        "envelope_hash": authorization.envelope_hash,
        "binding": dict(binding),
        "issued_at_utc": _utc_now(),
    }
    record["record_id"] = compute_record_id(record)
    verify_record_id(record)
    validate_owner_authorization(record)
    store.append_task_ledger(task_id, OWNER_AUTHORIZATION_LEDGER, record)
    return authorization


def consume_owner_authorization(
    store: TaskStoreProtocol,
    task_id: str,
    authorization_id: str,
    *,
    binding: Mapping[str, object],
) -> OwnerAuthorization:
    with store.lock(task_id):
        return consume_owner_authorization_locked(
            store, task_id, authorization_id, binding=binding
        )


def count_transfer_leases(
    store: TaskStoreProtocol, task_id: str, authorization_id: str
) -> int:
    wanted = _hex_digest(authorization_id, "authorization_id")
    return sum(
        1
        for row in replay_authorizations(store, task_id)
        if row.get("record_kind") == "transfer_lease"
        and row.get("authorization_id") == wanted
    )


def leases_for_permit(
    store: TaskStoreProtocol, task_id: str, permit_id: str
) -> tuple[Mapping[str, object], ...]:
    wanted = _string(permit_id, "permit_id")
    return tuple(
        row
        for row in replay_authorizations(store, task_id)
        if row.get("record_kind") == "transfer_lease" and row.get("permit_id") == wanted
    )


def record_transfer_lease_locked(
    store: TaskStoreProtocol,
    task_id: str,
    authorization_id: str,
    *,
    permit_id: str,
    paths: tuple[str, ...],
) -> dict[str, object]:
    store._assert_lock_held(task_id)
    authorization = load_owner_authorization(store, task_id, authorization_id)
    if authorization is None:
        _fail("AUTHORIZATION_UNKNOWN", "authorization is not in this task ledger")
        raise AssertionError("unreachable")
    if authorization.authorization_type != "OWNERSHIP_TRANSFER":
        _fail("AUTHORIZATION_SCOPE_MISMATCH", "authorization is not an ownership transfer")
    claimed = _allowed_paths(paths)
    allowed = set(authorization.allowed_paths or ())
    if not set(claimed).issubset(allowed):
        _fail("AUTHORIZATION_SCOPE_MISMATCH", "claimed paths are not in allowed_paths")
    used = count_transfer_leases(store, task_id, authorization.authorization_id)
    if used >= _positive_int(authorization.max_dispatches, "max_dispatches"):
        _fail("AUTHORIZATION_EXHAUSTED", "authorization has no remaining dispatches")
    record: dict[str, object] = {
        "schema_version": OWNER_AUTHORIZATION_SCHEMA_VERSION,
        "record_kind": "transfer_lease",
        "record_id": "",
        "authorization_id": authorization.authorization_id,
        "task_id": task_id,
        "envelope_hash": authorization.envelope_hash,
        "permit_id": _string(permit_id, "permit_id"),
        "dispatch_seq": used + 1,
        "allowed_paths": claimed,
        "issued_at_utc": _utc_now(),
    }
    record["record_id"] = compute_record_id(record)
    verify_record_id(record)
    validate_owner_authorization(record)
    store.append_task_ledger(task_id, OWNER_AUTHORIZATION_LEDGER, record)
    return record
