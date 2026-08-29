"""Append-only final-verdict history with evidence-derived issuer attestation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

try:
    from .ai_workflow_artifacts import (
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        content_id,
        load_artifact,
        sorted_strs,
        verify_content_id,
    )
    from .ai_workflow_authorizations import consume_owner_authorization_locked
    from .ai_workflow_candidate_state import (
        CandidateState,
        capture_candidate_state,
        validate_candidate_state,
    )
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        content_id,
        load_artifact,
        sorted_strs,
        verify_content_id,
    )
    from ai_workflow_authorizations import consume_owner_authorization_locked
    from ai_workflow_candidate_state import (
        CandidateState,
        capture_candidate_state,
        validate_candidate_state,
    )


FINAL_VERDICT_SCHEMA_VERSION = "ai-final-verdict-1"
FINAL_VERDICT_LEDGER = "final-verdicts.jsonl"
FINAL_VERDICT_FIELDS = frozenset(
    {
        "schema_version",
        "verdict_id",
        "task_id",
        "envelope_hash",
        "candidate_state",
        "verdict",
        "verdict_source_role",
        "issuer_evidence_id",
        "recorded_at_utc",
    }
)
VERDICT_VALUES = frozenset({"ACCEPT", "REJECT"})
FINAL_VERDICT_ISSUER_ROLES = frozenset({"sol_medium_reviewer"})
ISSUER_ROLE_CONTRACTS: Mapping[str, tuple[str, str, str, str]] = {
    "sol_medium_reviewer": ("gpt-5.6-sol", "medium", "read-only", "read-only"),
}
FRESHNESS_VALUES = frozenset({"FRESH", "STALE", "MISSING"})
RELEASE_COMPLETION_PHASES = frozenset({"SOL_XHIGH_TERMINAL_REPAIR"})
VERDICT_ID_EXCLUDE = frozenset({"verdict_id"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


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


def _candidate_state_from_mapping(value: object) -> CandidateState:
    if not isinstance(value, Mapping):
        _fail("INVALID_TYPE", "candidate_state must be an object")
    payload = dict(value)
    validate_candidate_state(payload)
    ids = payload["runtime_evidence_ids"]
    if not isinstance(ids, list):
        _fail("INVALID_TYPE", "runtime_evidence_ids must be an array")
    return CandidateState(
        schema_version=_string(payload["schema_version"], "schema_version"),
        task_id=_string(payload["task_id"], "task_id"),
        envelope_hash=_hex_digest(payload["envelope_hash"], "envelope_hash"),
        candidate_commit=_string(payload["candidate_commit"], "candidate_commit"),
        baseline_commit=_string(payload["baseline_commit"], "baseline_commit"),
        tree_digest=_hex_digest(payload["tree_digest"], "tree_digest"),
        diff_digest=_hex_digest(payload["diff_digest"], "diff_digest"),
        runtime_evidence_ids=tuple(_string(item, "runtime_evidence_ids") for item in ids),
        captured_at_utc=_string(payload["captured_at_utc"], "captured_at_utc"),
    )


def _verdict_preimage(record: Mapping[str, object]) -> dict[str, object]:
    payload = dict(record)
    state = payload.get("candidate_state")
    if isinstance(state, Mapping):
        projected = dict(state)
        if "runtime_evidence_ids" in projected:
            projected["runtime_evidence_ids"] = sorted_strs(projected["runtime_evidence_ids"])
        payload["candidate_state"] = projected
    return payload


def _freshness_key(state: CandidateState) -> tuple[object, ...]:
    return (
        state.candidate_commit,
        state.baseline_commit,
        state.tree_digest,
        state.diff_digest,
        tuple(sorted_strs(state.runtime_evidence_ids)),
    )


@dataclass(frozen=True)
class FinalVerdict:
    schema_version: str
    verdict_id: str
    task_id: str
    envelope_hash: str
    candidate_state: CandidateState
    verdict: str
    verdict_source_role: str
    issuer_evidence_id: str
    recorded_at_utc: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "verdict_id": self.verdict_id,
            "task_id": self.task_id,
            "envelope_hash": self.envelope_hash,
            "candidate_state": self.candidate_state.to_dict(),
            "verdict": self.verdict,
            "verdict_source_role": self.verdict_source_role,
            "issuer_evidence_id": self.issuer_evidence_id,
            "recorded_at_utc": self.recorded_at_utc,
        }


def validate_final_verdict(value: Mapping[str, object]) -> None:
    if not isinstance(value, Mapping):
        _fail("INVALID_TYPE", "final verdict must be an object")
    payload = dict(value)
    unknown = sorted(set(payload) - FINAL_VERDICT_FIELDS)
    if unknown:
        _fail("UNKNOWN_FIELD", f"unsupported field {unknown[0]}")
    missing = sorted(FINAL_VERDICT_FIELDS - set(payload))
    if missing:
        _fail("MISSING_FIELD", f"missing field {missing[0]}")
    if payload.get("schema_version") != FINAL_VERDICT_SCHEMA_VERSION:
        _fail("SCHEMA_VERSION", f"schema_version must be {FINAL_VERDICT_SCHEMA_VERSION}")
    _string(payload["task_id"], "task_id")
    _hex_digest(payload["verdict_id"], "verdict_id")
    _hex_digest(payload["envelope_hash"], "envelope_hash")
    _hex_digest(payload["issuer_evidence_id"], "issuer_evidence_id")
    _string(payload["recorded_at_utc"], "recorded_at_utc")
    verdict = payload["verdict"]
    if verdict not in VERDICT_VALUES:
        _fail("INVALID_ENUM", "verdict must be ACCEPT or REJECT")
    role = payload["verdict_source_role"]
    if role not in FINAL_VERDICT_ISSUER_ROLES:
        _fail("INVALID_ENUM", "verdict_source_role is not an allowed issuer role")
    _candidate_state_from_mapping(payload["candidate_state"])


def compute_verdict_id(record: Mapping[str, object]) -> str:
    return content_id(
        FINAL_VERDICT_SCHEMA_VERSION,
        _verdict_preimage(record),
        exclude=VERDICT_ID_EXCLUDE,
    )


def verify_verdict_id(record: Mapping[str, object]) -> None:
    verify_content_id(
        FINAL_VERDICT_SCHEMA_VERSION,
        _verdict_preimage(record),
        exclude=VERDICT_ID_EXCLUDE,
        id_field="verdict_id",
    )


def _verdict_from_mapping(value: Mapping[str, object]) -> FinalVerdict:
    validate_final_verdict(value)
    return FinalVerdict(
        schema_version=_string(value["schema_version"], "schema_version"),
        verdict_id=_hex_digest(value["verdict_id"], "verdict_id"),
        task_id=_string(value["task_id"], "task_id"),
        envelope_hash=_hex_digest(value["envelope_hash"], "envelope_hash"),
        candidate_state=_candidate_state_from_mapping(value["candidate_state"]),
        verdict=_string(value["verdict"], "verdict"),
        verdict_source_role=_string(value["verdict_source_role"], "verdict_source_role"),
        issuer_evidence_id=_hex_digest(value["issuer_evidence_id"], "issuer_evidence_id"),
        recorded_at_utc=_string(value["recorded_at_utc"], "recorded_at_utc"),
    )


def _find_issuer_evidence(
    store: TaskStoreProtocol, task_id: str, issuer_evidence_id: str
) -> Mapping[str, object] | None:
    for record in store.read_task_ledger(task_id, "runtime-evidence.jsonl"):
        if artifact_sha256(record) == issuer_evidence_id:
            return record
    return None


def _attest_issuer(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    candidate: CandidateState,
    issuer_evidence_id: str,
) -> str:
    evidence = _find_issuer_evidence(store, task_id, issuer_evidence_id)
    if evidence is None:
        _fail(
            "VERDICT_ISSUER_EVIDENCE_UNKNOWN",
            "issuer evidence is not in this task runtime-evidence ledger",
        )
        raise AssertionError("unreachable")
    if evidence.get("verification_status") != "VERIFIED":
        _fail(
            "VERDICT_ISSUER_EVIDENCE_NOT_VERIFIED",
            "issuer evidence is not VERIFIED",
        )
    role = evidence.get("requested_role")
    if not isinstance(role, str) or role not in FINAL_VERDICT_ISSUER_ROLES:
        _fail(
            "VERDICT_ISSUER_ROLE_FORBIDDEN",
            "issuer requested_role is not an allowed final-verdict issuer",
        )
        raise AssertionError("unreachable")
    observed = (
        evidence.get("observed_model"),
        evidence.get("observed_reasoning_effort"),
        evidence.get("observed_sandbox_policy"),
        evidence.get("observed_permission_profile"),
    )
    if observed != ISSUER_ROLE_CONTRACTS[role]:
        _fail(
            "VERDICT_ISSUER_IDENTITY_MISMATCH",
            "issuer observed identity does not match the role contract",
        )
    events = store.read_task_ledger(task_id, "events.jsonl")
    if not any(
        event.get("event_type") == "RUNTIME_EVIDENCE_RECORDED"
        and event.get("runtime_evidence_sha256") == issuer_evidence_id
        for event in events
    ):
        _fail(
            "VERDICT_ISSUER_EVIDENCE_ORPHAN",
            "issuer evidence has no RUNTIME_EVIDENCE_RECORDED event",
        )
    ledger_ids = {
        artifact_sha256(record)
        for record in store.read_task_ledger(task_id, "runtime-evidence.jsonl")
    }
    for evidence_id in candidate.runtime_evidence_ids:
        if evidence_id not in ledger_ids:
            _fail(
                "VERDICT_CANDIDATE_EVIDENCE_UNKNOWN",
                "candidate runtime_evidence_ids must belong to this task",
            )
    task = load_artifact(store._require_task(task_id) / "task.json")
    if candidate.envelope_hash != artifact_sha256(task):
        _fail("VERDICT_ENVELOPE_MISMATCH", "candidate envelope_hash does not match task.json")
    if candidate.task_id != task_id:
        _fail("VERDICT_ENVELOPE_MISMATCH", "candidate task_id does not match the locked task")
    return role


def record_final_verdict(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    verdict: str,
    candidate_state: CandidateState,
    issuer_evidence_id: str,
    recorded_at: str,
) -> Path:
    store._assert_lock_held(task_id)
    if verdict not in VERDICT_VALUES:
        _fail("INVALID_ENUM", "verdict must be ACCEPT or REJECT")
    recorded_at_utc = _string(recorded_at, "recorded_at")
    issuer_id = _hex_digest(issuer_evidence_id, "issuer_evidence_id")
    validate_candidate_state(candidate_state.to_dict())
    role = _attest_issuer(
        store,
        task_id,
        candidate=candidate_state,
        issuer_evidence_id=issuer_id,
    )
    record: dict[str, object] = {
        "schema_version": FINAL_VERDICT_SCHEMA_VERSION,
        "verdict_id": "",
        "task_id": task_id,
        "envelope_hash": candidate_state.envelope_hash,
        "candidate_state": candidate_state.to_dict(),
        "verdict": verdict,
        "verdict_source_role": role,
        "issuer_evidence_id": issuer_id,
        "recorded_at_utc": recorded_at_utc,
    }
    record["verdict_id"] = compute_verdict_id(record)
    verify_verdict_id(record)
    validate_final_verdict(record)
    store.append_task_ledger(task_id, FINAL_VERDICT_LEDGER, record)
    return store._require_task(task_id) / FINAL_VERDICT_LEDGER


def _read_verdict_rows(
    store: TaskStoreProtocol, task_id: str
) -> tuple[dict[str, object], ...]:
    try:
        return store.read_task_ledger(task_id, FINAL_VERDICT_LEDGER)
    except WorkflowError as exc:
        if str(exc.code).endswith("_CORRUPT"):
            _fail("VERDICT_LEDGER_CORRUPT", exc.message)
        raise


def load_verdict_history(
    store: TaskStoreProtocol, task_id: str
) -> tuple[FinalVerdict, ...]:
    rows = _read_verdict_rows(store, task_id)
    seen: set[str] = set()
    history: list[FinalVerdict] = []
    for row in rows:
        try:
            verify_verdict_id(row)
            validate_final_verdict(row)
        except WorkflowError as exc:
            if exc.code == "VERDICT_LEDGER_CORRUPT":
                raise
            _fail("VERDICT_LEDGER_CORRUPT", exc.message)
        if row.get("task_id") != task_id:
            _fail("VERDICT_LEDGER_CORRUPT", "verdict record task_id does not match")
        verdict_id = _string(row.get("verdict_id"), "verdict_id")
        if verdict_id in seen:
            _fail("VERDICT_LEDGER_CORRUPT", "duplicate verdict_id")
        seen.add(verdict_id)
        history.append(_verdict_from_mapping(row))
    return tuple(history)


def latest_verdict(store: TaskStoreProtocol, task_id: str) -> FinalVerdict | None:
    history = load_verdict_history(store, task_id)
    if not history:
        return None
    return history[-1]


def evaluate_verdict_freshness(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    current: CandidateState,
) -> str:
    latest = latest_verdict(store, task_id)
    if latest is None:
        return "MISSING"
    if _freshness_key(latest.candidate_state) != _freshness_key(current):
        return "STALE"
    return "FRESH"


def _runtime_evidence_ids_from_events(
    store: TaskStoreProtocol, task_id: str
) -> tuple[str, ...]:
    ids: list[str] = []
    for event in store.read_task_ledger(task_id, "events.jsonl"):
        if event.get("event_type") != "RUNTIME_EVIDENCE_RECORDED":
            continue
        digest = event.get("runtime_evidence_sha256")
        if isinstance(digest, str) and digest:
            ids.append(digest)
    return tuple(ids)


def require_verdict_fresh_locked(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    override_authorization_id: str | None = None,
) -> None:
    store._assert_lock_held(task_id)
    history = load_verdict_history(store, task_id)
    if not history:
        _fail("VERDICT_MISSING", "final verdict is missing")
    latest = history[-1]
    if latest.verdict == "REJECT":
        _fail("VERDICT_REJECTED", "latest final verdict is REJECT")
    current = capture_candidate_state(
        store,
        task_id,
        baseline_commit=latest.candidate_state.baseline_commit,
        runtime_evidence_ids=_runtime_evidence_ids_from_events(store, task_id),
    )
    freshness = evaluate_verdict_freshness(store, task_id, current=current)
    if freshness == "FRESH":
        return
    if freshness != "STALE":
        _fail("VERDICT_MISSING", "final verdict is missing")
    if not override_authorization_id:
        _fail("VERDICT_STALE", "latest ACCEPT verdict is stale")
    consume_owner_authorization_locked(
        store,
        task_id,
        override_authorization_id,
        binding={"candidate_state_digest": current.state_digest()},
    )


def require_verdict_fresh(
    store: TaskStoreProtocol,
    task_id: str,
    *,
    override_authorization_id: str | None = None,
) -> None:
    with store.lock(task_id):
        require_verdict_fresh_locked(
            store, task_id, override_authorization_id=override_authorization_id
        )
