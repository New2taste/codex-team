"""Read-only evidence-chain auditor. Production workflow must not import this."""

from __future__ import annotations

import argparse
import contextlib
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

try:
    from .ai_workflow_artifacts import (
        ArtifactError,
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        load_artifact,
        read_jsonl,
        verify_content_id,
    )
    from .ai_workflow_candidate_state import capture_candidate_state
    from .ai_workflow_declarations import DECLARATION_FILENAME, load_route_declaration
    from .ai_workflow_evidence import (
        FORK_STATES,
        LAUNCH_INTENT_EVENT_FIELDS,
        LAUNCH_INTENT_EVENT_TYPE,
        LAUNCH_INTENT_ID_EXCLUDE,
        LAUNCH_INTENT_SCHEMA_KIND,
        NESTED_STATES,
        RUNTIME_EVIDENCE_V2_LEDGER,
        replay_runtime_evidence_v2,
        verify_evidence_id,
        verify_launch_intent_id,
    )
    from .ai_workflow_verdicts import (
        evaluate_verdict_freshness,
        load_verdict_history,
        verify_verdict_id,
    )
except ImportError:  # direct script execution
    from ai_workflow_artifacts import (
        ArtifactError,
        TaskStoreProtocol,
        WorkflowError,
        artifact_sha256,
        load_artifact,
        read_jsonl,
        verify_content_id,
    )
    from ai_workflow_candidate_state import capture_candidate_state
    from ai_workflow_declarations import DECLARATION_FILENAME, load_route_declaration
    from ai_workflow_evidence import (
        FORK_STATES,
        LAUNCH_INTENT_EVENT_FIELDS,
        LAUNCH_INTENT_EVENT_TYPE,
        LAUNCH_INTENT_ID_EXCLUDE,
        LAUNCH_INTENT_SCHEMA_KIND,
        NESTED_STATES,
        RUNTIME_EVIDENCE_V2_LEDGER,
        replay_runtime_evidence_v2,
        verify_evidence_id,
        verify_launch_intent_id,
    )
    from ai_workflow_verdicts import (
        evaluate_verdict_freshness,
        load_verdict_history,
        verify_verdict_id,
    )


EVIDENCE_CHAIN_LINKS = (
    "route_declaration",
    "launch_intent",
    "rollout_identity",
    "fork_state",
    "final_verdict",
)
EVIDENCE_CHAIN_GAP_CODES = frozenset(
    {
        "CHAIN_MISSING_ROUTE_DECLARATION",
        "CHAIN_ENVELOPE_MISMATCH",
        "CHAIN_MISSING_LAUNCH_INTENT",
        "CHAIN_MISSING_ROLLOUT_IDENTITY",
        "CHAIN_FORK_STATE_UNVERIFIED",
        "CHAIN_MISSING_FINAL_VERDICT",
        "CHAIN_VERDICT_STALE",
        "CHAIN_EVIDENCE_ORPHAN",
    }
)
_GAP_ORDER = (
    "CHAIN_MISSING_ROUTE_DECLARATION",
    "CHAIN_ENVELOPE_MISMATCH",
    "CHAIN_MISSING_LAUNCH_INTENT",
    "CHAIN_MISSING_ROLLOUT_IDENTITY",
    "CHAIN_FORK_STATE_UNVERIFIED",
    "CHAIN_MISSING_FINAL_VERDICT",
    "CHAIN_VERDICT_STALE",
    "CHAIN_EVIDENCE_ORPHAN",
)
_VERIFIED_STATES = frozenset({"VERIFIED_NONE", "VERIFIED_PRESENT"})
_TASK_ID_PATTERN = re.compile(r"^AWF-[0-9]{8}-[0-9]{3,}$")
_LEDGER_NAME_PATTERN = re.compile(r"^[a-z0-9-]+\.jsonl$")


def _fail(code: str, message: str) -> None:
    raise WorkflowError(code, message)


@dataclass(frozen=True)
class EvidenceChainLink:
    task_id: str | None
    envelope_hash: str | None
    source_path: str | None
    event_index: int | None
    fork_state: str | None = None
    nested_state: str | None = None


@dataclass(frozen=True)
class EvidenceChain:
    task_id: str
    envelope_hash: str
    route_declaration: EvidenceChainLink | None
    launch_intent: EvidenceChainLink | None
    rollout_identity: EvidenceChainLink | None
    fork_state: EvidenceChainLink | None
    final_verdict: EvidenceChainLink | None
    verdict_freshness: str | None = None
    evidence_orphan: bool = False


class _ReadOnlyStore:
    """Reject writes while delegating reads to an inner TaskStoreProtocol."""

    def __init__(self, inner: TaskStoreProtocol):
        self._inner = inner

    def lock(self, task_id: str) -> contextlib.AbstractContextManager[None]:
        return self._inner.lock(task_id)

    def _require_task(self, task_id: str) -> Path:
        return self._inner._require_task(task_id)

    def read_task_ledger(
        self, task_id: str, name: str
    ) -> tuple[dict[str, object], ...]:
        return self._inner.read_task_ledger(task_id, name)

    def _assert_lock_held(self, task_id: str) -> None:
        self._inner._assert_lock_held(task_id)

    def append_event(self, task_id: str, event: dict) -> None:
        _fail("READ_ONLY", "evidence chain auditor cannot write")

    def write_task_artifact_once(
        self,
        task_id: str,
        name: str,
        value: Mapping[str, object],
        *,
        conflict_code: str,
    ) -> Path:
        _fail("READ_ONLY", "evidence chain auditor cannot write")
        raise AssertionError("unreachable")

    def append_task_ledger(
        self, task_id: str, name: str, record: Mapping[str, object]
    ) -> None:
        _fail("READ_ONLY", "evidence chain auditor cannot write")


class _FilesystemReadOnlyStore:
    """Filesystem-backed read-only store for the auditor CLI."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._held: set[str] = set()

    def _validate_task_id(self, task_id: str) -> None:
        if not isinstance(task_id, str) or not _TASK_ID_PATTERN.fullmatch(task_id):
            _fail("INVALID_TASK_ID", "task_id must match AWF-YYYYMMDD-NNN")

    def _require_task(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        task_dir = self.root / task_id
        if not task_dir.is_dir():
            _fail("TASK_NOT_FOUND", f"task {task_id} does not exist")
        return task_dir

    def _assert_lock_held(self, task_id: str) -> None:
        if task_id not in self._held:
            _fail("LOCK_REQUIRED", f"task lock is required for {task_id}")

    @contextlib.contextmanager
    def lock(self, task_id: str) -> Iterator[None]:
        if task_id in self._held:
            _fail("TASK_ALREADY_RUNNING", f"task {task_id} is already running")
        self._require_task(task_id)
        self._held.add(task_id)
        try:
            yield
        finally:
            self._held.discard(task_id)

    def read_task_ledger(
        self, task_id: str, name: str
    ) -> tuple[dict[str, object], ...]:
        if not isinstance(name, str) or not _LEDGER_NAME_PATTERN.fullmatch(name):
            _fail("INVALID_RECORD", f"ledger name is invalid: {name}")
        path = self._require_task(task_id) / name
        code = name[: -len(".jsonl")].replace("-", "_").upper()
        return read_jsonl(path, code=code)

    def append_event(self, task_id: str, event: dict) -> None:
        _fail("READ_ONLY", "evidence chain auditor cannot write")

    def write_task_artifact_once(
        self,
        task_id: str,
        name: str,
        value: Mapping[str, object],
        *,
        conflict_code: str,
    ) -> Path:
        _fail("READ_ONLY", "evidence chain auditor cannot write")
        raise AssertionError("unreachable")

    def append_task_ledger(
        self, task_id: str, name: str, record: Mapping[str, object]
    ) -> None:
        _fail("READ_ONLY", "evidence chain auditor cannot write")


def _load_task(store: TaskStoreProtocol, task_id: str) -> dict[str, object]:
    try:
        return load_artifact(store._require_task(task_id) / "task.json")
    except ArtifactError as exc:
        raise WorkflowError(exc.code, exc.message) from exc


def _load_declaration(store: TaskStoreProtocol, task_id: str) -> Mapping[str, object] | None:
    try:
        declaration = load_route_declaration(store, task_id)
    except WorkflowError as exc:
        if exc.code == "ROUTE_DECLARATION_CORRUPT":
            return None
        if exc.code == "READ_ONLY":
            declaration = None
        else:
            raise
    if declaration is not None:
        return declaration.to_dict()
    path = store._require_task(task_id) / DECLARATION_FILENAME
    try:
        payload = load_artifact(path)
    except (ArtifactError, FileNotFoundError):
        return None
    except OSError:
        return None
    if not isinstance(payload, Mapping):
        return None
    return dict(payload)


def _events(store: TaskStoreProtocol, task_id: str) -> tuple[dict[str, object], ...]:
    return store.read_task_ledger(task_id, "events.jsonl")


def _event_ids(events: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    ids: list[str] = []
    for event in events:
        if event.get("event_type") != "RUNTIME_EVIDENCE_RECORDED":
            continue
        digest = event.get("runtime_evidence_sha256")
        if isinstance(digest, str) and digest:
            ids.append(digest)
    return tuple(ids)


def _ledger_hashes(store: TaskStoreProtocol, task_id: str) -> set[str]:
    hashes: set[str] = set()
    for record in store.read_task_ledger(task_id, "runtime-evidence.jsonl"):
        hashes.add(artifact_sha256(record))
    return hashes


def _first_event(
    events: Sequence[Mapping[str, object]], event_type: str
) -> tuple[int, dict[str, object]] | None:
    for index, event in enumerate(events):
        if event.get("event_type") == event_type:
            return index, dict(event)
    return None


def _replay_v2(
    store: TaskStoreProtocol, task_id: str
) -> tuple[dict[str, object], ...]:
    try:
        return replay_runtime_evidence_v2(store, task_id)
    except WorkflowError:
        return ()


def _axis_unverified(value: object, closed: frozenset[str]) -> bool:
    return value not in _VERIFIED_STATES or value not in closed


def build_evidence_chain(store: TaskStoreProtocol, task_id: str) -> EvidenceChain:
    readonly = _ReadOnlyStore(store)
    task = _load_task(readonly, task_id)
    envelope_hash = artifact_sha256(task)
    task_dir = readonly._require_task(task_id)
    events = _events(readonly, task_id)
    event_ids = _event_ids(events)
    ledger_ids = _ledger_hashes(readonly, task_id)

    declaration = _load_declaration(readonly, task_id)
    route_link: EvidenceChainLink | None = None
    if declaration is not None:
        route_link = EvidenceChainLink(
            task_id=str(declaration.get("task_id") or ""),
            envelope_hash=str(declaration.get("envelope_hash") or ""),
            source_path=str(task_dir / DECLARATION_FILENAME),
            event_index=None,
        )

    intent_found = _first_event(events, LAUNCH_INTENT_EVENT_TYPE)
    intent_link: EvidenceChainLink | None = None
    if intent_found is not None:
        index, event = intent_found
        try:
            verify_launch_intent_id(event)
            verify_content_id(
                LAUNCH_INTENT_SCHEMA_KIND,
                event,
                exclude=LAUNCH_INTENT_ID_EXCLUDE,
                id_field="event_id",
            )
        except WorkflowError:
            intent_link = None
        else:
            extra = set(event) - LAUNCH_INTENT_EVENT_FIELDS
            if extra:
                intent_link = None
            else:
                intent_link = EvidenceChainLink(
                    task_id=str(event.get("task_id") or ""),
                    envelope_hash=str(event.get("envelope_hash") or ""),
                    source_path=str(task_dir / "events.jsonl"),
                    event_index=index,
                )

    rollout_found = _first_event(events, "RUNTIME_EVIDENCE_RECORDED")
    rollout_link: EvidenceChainLink | None = None
    if rollout_found is not None:
        index, event = rollout_found
        rollout_link = EvidenceChainLink(
            task_id=str(event.get("task_id") or task.get("task_id") or task_id),
            envelope_hash=envelope_hash,
            source_path=str(task_dir / "events.jsonl"),
            event_index=index,
        )

    v2_rows = _replay_v2(readonly, task_id)
    fork_link: EvidenceChainLink | None = None
    if v2_rows:
        fork_axis = "VERIFIED_NONE"
        nested_axis = "VERIFIED_NONE"
        chosen = v2_rows[-1]
        for row in v2_rows:
            try:
                verify_evidence_id(row)
            except WorkflowError:
                fork_axis = "AUTHORITY_UNAVAILABLE"
                nested_axis = "AUTHORITY_UNAVAILABLE"
                chosen = row
                break
            if _axis_unverified(row.get("fork_state"), FORK_STATES):
                fork_axis = "AUTHORITY_UNAVAILABLE"
            elif fork_axis != "AUTHORITY_UNAVAILABLE" and row.get("fork_state") == "VERIFIED_PRESENT":
                fork_axis = "VERIFIED_PRESENT"
            if _axis_unverified(row.get("nested_state"), NESTED_STATES):
                nested_axis = "AUTHORITY_UNAVAILABLE"
            elif nested_axis != "AUTHORITY_UNAVAILABLE" and row.get("nested_state") == "VERIFIED_PRESENT":
                nested_axis = "VERIFIED_PRESENT"
            chosen = row
        fork_link = EvidenceChainLink(
            task_id=str(chosen.get("task_id") or ""),
            envelope_hash=str(chosen.get("envelope_hash") or ""),
            source_path=str(task_dir / RUNTIME_EVIDENCE_V2_LEDGER),
            event_index=chosen.get("event_index") if isinstance(chosen.get("event_index"), int) else None,
            fork_state=fork_axis,
            nested_state=nested_axis,
        )

    history = load_verdict_history(readonly, task_id)
    verdict_link: EvidenceChainLink | None = None
    freshness: str | None = None
    orphan = False
    if history:
        latest = history[-1]
        verify_verdict_id(latest.to_dict())
        verdict_ids = tuple(latest.candidate_state.runtime_evidence_ids)
        event_set = set(event_ids)
        ledger_set = set(ledger_ids)
        if any(item not in event_set or item not in ledger_set for item in verdict_ids):
            orphan = True
        for digest in event_ids:
            if digest not in ledger_set:
                orphan = True
        current = capture_candidate_state(
            readonly,
            task_id,
            baseline_commit=latest.candidate_state.baseline_commit,
            runtime_evidence_ids=event_ids,
        )
        freshness = evaluate_verdict_freshness(readonly, task_id, current=current)
        verdict_link = EvidenceChainLink(
            task_id=latest.task_id,
            envelope_hash=latest.envelope_hash,
            source_path=str(task_dir / "final-verdicts.jsonl"),
            event_index=len(history) - 1,
        )

    return EvidenceChain(
        task_id=str(task.get("task_id") or task_id),
        envelope_hash=envelope_hash,
        route_declaration=route_link,
        launch_intent=intent_link,
        rollout_identity=rollout_link,
        fork_state=fork_link,
        final_verdict=verdict_link,
        verdict_freshness=freshness,
        evidence_orphan=orphan,
    )


def validate_evidence_chain(built: EvidenceChain) -> tuple[str, ...]:
    found: set[str] = set()
    if built.route_declaration is None:
        found.add("CHAIN_MISSING_ROUTE_DECLARATION")
    if built.launch_intent is None:
        found.add("CHAIN_MISSING_LAUNCH_INTENT")
    if built.rollout_identity is None:
        found.add("CHAIN_MISSING_ROLLOUT_IDENTITY")
    fork = built.fork_state
    if (
        fork is None
        or fork.fork_state == "AUTHORITY_UNAVAILABLE"
        or fork.nested_state == "AUTHORITY_UNAVAILABLE"
        or fork.fork_state not in _VERIFIED_STATES
        or fork.nested_state not in _VERIFIED_STATES
    ):
        found.add("CHAIN_FORK_STATE_UNVERIFIED")
    if built.final_verdict is None:
        found.add("CHAIN_MISSING_FINAL_VERDICT")
    else:
        if built.verdict_freshness == "STALE":
            found.add("CHAIN_VERDICT_STALE")
        if built.evidence_orphan:
            found.add("CHAIN_EVIDENCE_ORPHAN")
    for name in EVIDENCE_CHAIN_LINKS:
        link = getattr(built, name)
        if link is None:
            continue
        if link.task_id != built.task_id or link.envelope_hash != built.envelope_hash:
            found.add("CHAIN_ENVELOPE_MISMATCH")
            break
    return tuple(code for code in _GAP_ORDER if code in found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-workflow-evidence-chain")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args(argv)
    store = _FilesystemReadOnlyStore(args.root)
    built = build_evidence_chain(store, args.task_id)
    gaps = validate_evidence_chain(built)
    for name in EVIDENCE_CHAIN_LINKS:
        link = getattr(built, name)
        if link is None:
            print(f"{name} missing")
            continue
        detail = ""
        if name == "fork_state" and link.fork_state:
            detail = f" {link.fork_state}"
        elif name == "final_verdict" and built.verdict_freshness:
            detail = f" {built.verdict_freshness}"
        print(f"{name} {link.task_id} {link.envelope_hash}{detail}")
    for gap in gaps:
        print(gap)
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
