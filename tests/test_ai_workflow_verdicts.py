"""Append-only final verdicts: canonical IDs, issuer attestation, freshness."""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_authorizations as authorizations
from scripts import ai_workflow_candidate_state as candidate_state
from scripts import ai_workflow_repairs as repairs
from scripts import ai_workflow_verdicts as verdicts
from tests import test_ai_workflow_adversarial_acceptance as adversarial_acceptance


ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "AWF-20260803-001"
OTHER_TASK_ID = "AWF-20260803-002"
LEDGER_NAME = "final-verdicts.jsonl"
GOLDEN_VERDICT_ID = "6192f2617ad03cf702053a83c630f6bccb75faea5dea14ca3c405cc08c1f17de"
FROZEN_V2_APPEND_OPENED_FIELDS = frozenset(
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
        "owner_actor",
        "owner_receipt",
        "owner_receipt_sha256",
        "initial_candidate_commit",
    }
)


def _valid_task(*, task_id: str = TASK_ID) -> dict[str, object]:
    return {
        "schema_version": "ai-task-1",
        "task_id": task_id,
        "task_type": "PLAN",
        "objective": "Record a final verdict against a frozen candidate",
        "repository_root": str(ROOT),
        "source_worktree": None,
        "base_commit": None,
        "candidate_commit": None,
        "authoritative_files": ["README.md"],
        "allowed_write_paths": [],
        "forbidden_actions": ["merge", "push", "change_constitution"],
        "risk_flags": [],
        "acceptance_commands": [],
        "verification_level": "L1",
        "human_gates": ["PLAN_APPROVAL"],
    }


def _golden_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": "ai-final-verdict-1",
        "verdict_id": "deadbeef" * 8,
        "task_id": TASK_ID,
        "envelope_hash": "a" * 64,
        "candidate_state": {
            "schema_version": "ai-candidate-state-1",
            "task_id": TASK_ID,
            "envelope_hash": "a" * 64,
            "candidate_commit": "b" * 40,
            "baseline_commit": "c" * 40,
            "tree_digest": "d" * 64,
            "diff_digest": "e" * 64,
            "runtime_evidence_ids": ["ff" * 32, "aa" * 32, "aa" * 32],
            "captured_at_utc": "2026-08-28T00:00:00Z",
        },
        "verdict": "ACCEPT",
        "verdict_source_role": "sol_medium_reviewer",
        "issuer_evidence_id": "1" * 64,
        "recorded_at_utc": "2026-08-28T12:00:00Z",
    }
    record.update(overrides)
    return record


def _issuer_evidence(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": "runtime-evidence-1",
        "attempt_id": "attempt-001",
        "requested_role": "sol_medium_reviewer",
        "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
        "observed_agent_type": None,
        "native_agent_id": None,
        "native_thread_id": None,
        "observed_model": "gpt-5.6-sol",
        "observed_reasoning_effort": "medium",
        "observed_sandbox_policy": "read-only",
        "observed_permission_profile": "read-only",
        "observed_cwd": str(ROOT),
        "evidence_source": "LOCAL_ROLLOUT",
        "observed_at_utc": "2026-08-28T00:00:00Z",
        "verification_status": "VERIFIED",
        "failure_reasons": [],
    }
    record.update(overrides)
    return record


def _evidence_id(record: dict[str, object]) -> str:
    return artifacts.artifact_sha256(record)


def _candidate(
    *,
    task_id: str,
    envelope_hash: str,
    runtime_evidence_ids: tuple[str, ...],
    **overrides: object,
) -> candidate_state.CandidateState:
    payload: dict[str, object] = {
        "schema_version": candidate_state.CANDIDATE_STATE_SCHEMA_VERSION,
        "task_id": task_id,
        "envelope_hash": envelope_hash,
        "candidate_commit": "b" * 40,
        "baseline_commit": "c" * 40,
        "tree_digest": "d" * 64,
        "diff_digest": "e" * 64,
        "runtime_evidence_ids": runtime_evidence_ids,
        "captured_at_utc": "2026-08-28T00:00:00Z",
    }
    payload.update(overrides)
    return candidate_state.CandidateState(
        schema_version=str(payload["schema_version"]),
        task_id=str(payload["task_id"]),
        envelope_hash=str(payload["envelope_hash"]),
        candidate_commit=str(payload["candidate_commit"]),
        baseline_commit=str(payload["baseline_commit"]),
        tree_digest=str(payload["tree_digest"]),
        diff_digest=str(payload["diff_digest"]),
        runtime_evidence_ids=tuple(payload["runtime_evidence_ids"]),
        captured_at_utc=str(payload["captured_at_utc"]),
    )


class GoldenPreimageTest(unittest.TestCase):
    def test_verdict_id_exclude_is_only_verdict_id(self) -> None:
        self.assertEqual(frozenset({"verdict_id"}), verdicts.VERDICT_ID_EXCLUDE)

    def test_frozen_fixture_hashes_to_literal_golden(self) -> None:
        computed = verdicts.compute_verdict_id(_golden_record())
        self.assertEqual(GOLDEN_VERDICT_ID, computed)
        self.assertEqual(64, len(computed))

    def test_recorded_at_utc_change_flips_verdict_id(self) -> None:
        original = verdicts.compute_verdict_id(_golden_record())
        mutated = verdicts.compute_verdict_id(
            _golden_record(recorded_at_utc="2026-08-28T12:00:01Z")
        )
        self.assertNotEqual(original, mutated)

    def test_prefilled_verdict_id_does_not_affect_compute(self) -> None:
        left = verdicts.compute_verdict_id(_golden_record(verdict_id="deadbeef" * 8))
        right = verdicts.compute_verdict_id(_golden_record(verdict_id="cafebabe" * 8))
        self.assertEqual(GOLDEN_VERDICT_ID, left)
        self.assertEqual(left, right)

    def test_verify_accepts_matching_id_and_rejects_tamper(self) -> None:
        record = _golden_record()
        record["verdict_id"] = verdicts.compute_verdict_id(record)
        verdicts.verify_verdict_id(record)
        record["recorded_at_utc"] = "2026-08-28T12:00:01Z"
        with self.assertRaisesRegex(artifacts.WorkflowError, "CONTENT_ID_MISMATCH"):
            verdicts.verify_verdict_id(record)

    def test_verdict_outside_closed_set_is_rejected(self) -> None:
        record = _golden_record()
        record["verdict_id"] = verdicts.compute_verdict_id(record)
        record["verdict"] = "MAYBE"
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_ENUM"):
            verdicts.validate_final_verdict(record)

    def test_missing_candidate_state_is_rejected(self) -> None:
        record = _golden_record()
        del record["candidate_state"]
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            verdicts.validate_final_verdict(record)


class _VerdictStoreMixin:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary.name))
        self.store.create_task(_valid_task())
        self.task = artifacts.load_artifact(
            self.store._require_task(TASK_ID) / "task.json"
        )
        self.envelope_hash = artifacts.artifact_sha256(self.task)
        self.evidence = _issuer_evidence()
        self.issuer_id = _evidence_id(self.evidence)
        self.state = _candidate(
            task_id=TASK_ID,
            envelope_hash=self.envelope_hash,
            runtime_evidence_ids=(self.issuer_id,),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed_issuer(self, evidence: dict[str, object], *, with_event: bool = True) -> str:
        issuer_id = _evidence_id(evidence)
        self.store.append_task_ledger(TASK_ID, "runtime-evidence.jsonl", evidence)
        if with_event:
            self.store.append_event(
                TASK_ID,
                {
                    "event_type": "RUNTIME_EVIDENCE_RECORDED",
                    "attempt_id": evidence["attempt_id"],
                    "requested_role": evidence["requested_role"],
                    "runtime_evidence_sha256": issuer_id,
                },
            )
        return issuer_id

    def _record(
        self,
        *,
        verdict: str = "ACCEPT",
        state: candidate_state.CandidateState | None = None,
        issuer_id: str | None = None,
        recorded_at: str = "2026-08-28T12:00:00Z",
    ) -> Path:
        with self.store.lock(TASK_ID):
            return verdicts.record_final_verdict(
                self.store,
                TASK_ID,
                verdict=verdict,
                candidate_state=state or self.state,
                issuer_evidence_id=issuer_id or self.issuer_id,
                recorded_at=recorded_at,
            )


class VerdictHistoryStoreTest(_VerdictStoreMixin, unittest.TestCase):
    def test_record_round_trips_and_latest_follows_line_order(self) -> None:
        self._seed_issuer(self.evidence)
        first_path = self._record(verdict="ACCEPT", recorded_at="2026-08-28T12:00:00Z")
        first_bytes = first_path.read_bytes()
        self._record(verdict="REJECT", recorded_at="2026-08-28T12:00:01Z")
        history = verdicts.load_verdict_history(self.store, TASK_ID)
        self.assertEqual(2, len(history))
        self.assertEqual("ACCEPT", history[0].verdict)
        self.assertEqual("REJECT", history[1].verdict)
        self.assertEqual("sol_medium_reviewer", history[0].verdict_source_role)
        self.assertEqual("sol_medium_reviewer", history[1].verdict_source_role)
        latest = verdicts.latest_verdict(self.store, TASK_ID)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(history[1].verdict_id, latest.verdict_id)
        self.assertEqual("REJECT", latest.verdict)
        after = first_path.read_bytes()
        self.assertTrue(after.startswith(first_bytes))
        first_line = first_bytes.splitlines()[0]
        self.assertEqual(first_line, after.splitlines()[0])

    def test_record_requires_lock(self) -> None:
        self._seed_issuer(self.evidence)
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            verdicts.record_final_verdict(
                self.store,
                TASK_ID,
                verdict="ACCEPT",
                candidate_state=self.state,
                issuer_evidence_id=self.issuer_id,
                recorded_at="2026-08-28T12:00:00Z",
            )

    def test_signature_has_no_verdict_source_role_parameter(self) -> None:
        parameters = inspect.signature(verdicts.record_final_verdict).parameters
        self.assertNotIn("verdict_source_role", parameters)
        self.assertEqual(
            ("verdict", "candidate_state", "issuer_evidence_id", "recorded_at"),
            tuple(
                name
                for name, param in parameters.items()
                if param.kind is inspect.Parameter.KEYWORD_ONLY
            ),
        )


class IssuerAttestationTest(_VerdictStoreMixin, unittest.TestCase):
    def test_unknown_role_is_forbidden(self) -> None:
        evidence = _issuer_evidence(requested_role="luna")
        issuer_id = self._seed_issuer(evidence)
        state = replace(self.state, runtime_evidence_ids=(issuer_id,))
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "VERDICT_ISSUER_ROLE_FORBIDDEN"
        ):
            self._record(state=state, issuer_id=issuer_id)

    def test_missing_issuer_evidence_is_unknown(self) -> None:
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "VERDICT_ISSUER_EVIDENCE_UNKNOWN"
        ):
            self._record()

    def test_issuer_on_other_task_is_unknown(self) -> None:
        self.store.create_task(_valid_task(task_id=OTHER_TASK_ID))
        self.store.append_task_ledger(
            OTHER_TASK_ID, "runtime-evidence.jsonl", self.evidence
        )
        self.store.append_event(
            OTHER_TASK_ID,
            {
                "event_type": "RUNTIME_EVIDENCE_RECORDED",
                "attempt_id": self.evidence["attempt_id"],
                "requested_role": self.evidence["requested_role"],
                "runtime_evidence_sha256": self.issuer_id,
            },
        )
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "VERDICT_ISSUER_EVIDENCE_UNKNOWN"
        ):
            self._record()

    def test_unverified_issuer_evidence_is_rejected(self) -> None:
        evidence = _issuer_evidence(verification_status="FAILED")
        issuer_id = self._seed_issuer(evidence)
        state = replace(self.state, runtime_evidence_ids=(issuer_id,))
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "VERDICT_ISSUER_EVIDENCE_NOT_VERIFIED"
        ):
            self._record(state=state, issuer_id=issuer_id)

    def test_missing_recorded_event_is_orphan(self) -> None:
        issuer_id = self._seed_issuer(self.evidence, with_event=False)
        state = replace(self.state, runtime_evidence_ids=(issuer_id,))
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "VERDICT_ISSUER_EVIDENCE_ORPHAN"
        ):
            self._record(state=state, issuer_id=issuer_id)

    def test_identity_tuple_mismatch_each_field(self) -> None:
        mutations = (
            {"observed_model": "gpt-5.6-terra"},
            {"observed_reasoning_effort": "xhigh"},
            {"observed_sandbox_policy": "workspace-write"},
            {"observed_permission_profile": "workspace-write"},
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                evidence = _issuer_evidence(
                    attempt_id=f"attempt-mismatch-{index}", **mutation
                )
                issuer_id = self._seed_issuer(evidence)
                state = replace(self.state, runtime_evidence_ids=(issuer_id,))
                with self.assertRaisesRegex(
                    artifacts.WorkflowError, "VERDICT_ISSUER_IDENTITY_MISMATCH"
                ):
                    self._record(state=state, issuer_id=issuer_id)

    def test_accept_and_reject_stamp_role_from_evidence(self) -> None:
        self._seed_issuer(self.evidence)
        self._record(verdict="ACCEPT", recorded_at="2026-08-28T12:00:00Z")
        self._record(verdict="REJECT", recorded_at="2026-08-28T12:00:01Z")
        history = verdicts.load_verdict_history(self.store, TASK_ID)
        self.assertEqual(
            ("sol_medium_reviewer", "sol_medium_reviewer"),
            tuple(item.verdict_source_role for item in history),
        )
        self.assertEqual(self.evidence["requested_role"], history[0].verdict_source_role)
        self.assertEqual(self.evidence["requested_role"], history[1].verdict_source_role)

    def test_caller_inputs_cannot_change_stamped_role(self) -> None:
        self._seed_issuer(self.evidence)
        self._record(
            verdict="ACCEPT",
            recorded_at="2026-08-28T12:00:00Z",
        )
        other_state = replace(self.state, captured_at_utc="2026-08-28T00:00:01Z")
        self._record(
            verdict="REJECT",
            state=other_state,
            recorded_at="2099-01-01T00:00:00Z",
        )
        roles = {
            item.verdict_source_role
            for item in verdicts.load_verdict_history(self.store, TASK_ID)
        }
        self.assertEqual({"sol_medium_reviewer"}, roles)

    def test_issuer_role_contracts_match_acceptance_mapping(self) -> None:
        source = inspect.getsource(repairs._v2_validate_observed_receipt)
        self.assertIn(
            '"sol_medium_reviewer": ("gpt-5.6-sol", "medium", "read-only", "read-only")',
            source,
        )
        self.assertEqual(
            ("gpt-5.6-sol", "medium", "read-only", "read-only"),
            verdicts.ISSUER_ROLE_CONTRACTS["sol_medium_reviewer"],
        )
        self.assertEqual(
            frozenset({"sol_medium_reviewer"}),
            verdicts.FINAL_VERDICT_ISSUER_ROLES,
        )


class FreshnessTest(_VerdictStoreMixin, unittest.TestCase):
    def test_missing_verdict_is_missing(self) -> None:
        status = verdicts.evaluate_verdict_freshness(
            self.store, TASK_ID, current=self.state
        )
        self.assertEqual("MISSING", status)
        self.assertIsNone(verdicts.latest_verdict(self.store, TASK_ID))

    def test_unchanged_candidate_is_fresh(self) -> None:
        self._seed_issuer(self.evidence)
        self._record()
        status = verdicts.evaluate_verdict_freshness(
            self.store, TASK_ID, current=self.state
        )
        self.assertEqual("FRESH", status)

    def test_only_candidate_commit_advance_is_stale(self) -> None:
        self._seed_issuer(self.evidence)
        self._record()
        current = replace(self.state, candidate_commit="1" * 40)
        self.assertEqual(
            "STALE",
            verdicts.evaluate_verdict_freshness(self.store, TASK_ID, current=current),
        )

    def test_only_tree_digest_change_is_stale(self) -> None:
        self._seed_issuer(self.evidence)
        self._record()
        current = replace(self.state, tree_digest="2" * 64)
        self.assertEqual(
            "STALE",
            verdicts.evaluate_verdict_freshness(self.store, TASK_ID, current=current),
        )

    def test_only_diff_digest_change_is_stale(self) -> None:
        self._seed_issuer(self.evidence)
        self._record()
        current = replace(self.state, diff_digest="3" * 64)
        self.assertEqual(
            "STALE",
            verdicts.evaluate_verdict_freshness(self.store, TASK_ID, current=current),
        )

    def test_only_evidence_id_set_change_is_stale(self) -> None:
        self._seed_issuer(self.evidence)
        self._record()
        current = replace(
            self.state,
            runtime_evidence_ids=self.state.runtime_evidence_ids + ("ab" * 32,),
        )
        self.assertEqual(
            "STALE",
            verdicts.evaluate_verdict_freshness(self.store, TASK_ID, current=current),
        )

    def test_new_verdict_can_be_fresh_while_old_stays_stale(self) -> None:
        self._seed_issuer(self.evidence)
        self._record(recorded_at="2026-08-28T12:00:00Z")
        ledger = self.store._require_task(TASK_ID) / LEDGER_NAME
        old_bytes = ledger.read_bytes()
        drifted = replace(self.state, candidate_commit="1" * 40)
        self.assertEqual(
            "STALE",
            verdicts.evaluate_verdict_freshness(self.store, TASK_ID, current=drifted),
        )
        self._record(state=drifted, recorded_at="2026-08-28T13:00:00Z")
        self.assertEqual(
            "FRESH",
            verdicts.evaluate_verdict_freshness(self.store, TASK_ID, current=drifted),
        )
        history = verdicts.load_verdict_history(self.store, TASK_ID)
        self.assertEqual(2, len(history))
        self.assertEqual("b" * 40, history[0].candidate_state.candidate_commit)
        self.assertEqual("1" * 40, history[1].candidate_state.candidate_commit)
        self.assertEqual(old_bytes.splitlines()[0], ledger.read_bytes().splitlines()[0])
        self.assertEqual(
            "STALE",
            verdicts.evaluate_verdict_freshness(
                self.store, TASK_ID, current=self.state
            ),
        )


class VerdictLedgerReplayTest(_VerdictStoreMixin, unittest.TestCase):
    def _valid_line(self) -> bytes:
        self._seed_issuer(self.evidence)
        path = self._record()
        return path.read_bytes()

    def _write_ledger(self, raw: bytes) -> None:
        path = self.store._require_task(TASK_ID) / LEDGER_NAME
        path.write_bytes(raw)

    def test_truncated_trailing_record_is_corrupt(self) -> None:
        line = self._valid_line().rstrip(b"\n")
        self._write_ledger(line)
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_LEDGER_CORRUPT"):
            verdicts.load_verdict_history(self.store, TASK_ID)

    def test_tampered_history_line_is_corrupt(self) -> None:
        raw = self._valid_line()
        record = json.loads(raw.decode("utf-8"))
        record["recorded_at_utc"] = "2099-01-01T00:00:00Z"
        self._write_ledger((artifacts.canonical_json(record) + "\n").encode("utf-8"))
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_LEDGER_CORRUPT"):
            verdicts.load_verdict_history(self.store, TASK_ID)

    def test_foreign_task_id_is_corrupt(self) -> None:
        self._valid_line()
        foreign = _golden_record(task_id=OTHER_TASK_ID)
        foreign["verdict_id"] = verdicts.compute_verdict_id(foreign)
        path = self.store._require_task(TASK_ID) / LEDGER_NAME
        with path.open("a", encoding="utf-8") as handle:
            handle.write(artifacts.canonical_json(foreign) + "\n")
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_LEDGER_CORRUPT"):
            verdicts.load_verdict_history(self.store, TASK_ID)

    def test_non_object_line_is_corrupt(self) -> None:
        raw = self._valid_line()
        self._write_ledger(raw + b"[]\n")
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_LEDGER_CORRUPT"):
            verdicts.load_verdict_history(self.store, TASK_ID)

    def test_duplicate_verdict_id_is_corrupt(self) -> None:
        raw = self._valid_line()
        self._write_ledger(raw + raw)
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_LEDGER_CORRUPT"):
            verdicts.load_verdict_history(self.store, TASK_ID)

    def test_ledger_record_has_no_seq_field(self) -> None:
        self.assertNotIn("seq", verdicts.FINAL_VERDICT_FIELDS)
        self.assertFalse(hasattr(verdicts.FinalVerdict, "seq"))
        self.assertNotIn(
            "seq", inspect.signature(verdicts.load_verdict_history).parameters
        )


def _first_call_name(function) -> str | None:
    tree = ast.parse(inspect.getsource(function))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    for node in func.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
        else:
            return None
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        if isinstance(call.func, ast.Name):
            return call.func.id
        return None
    return None


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


class ReleaseGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "gate@example.test")
        _git(self.repo, "config", "user.name", "Release Gate")
        _git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        _git(self.repo, "add", "README.md")
        _git(self.repo, "commit", "-m", "A")
        self.commit_a = _git(self.repo, "rev-parse", "HEAD")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("v1\n", encoding="utf-8")
        _git(self.repo, "add", "src/app.py")
        _git(self.repo, "commit", "-m", "B")
        self.commit_b = _git(self.repo, "rev-parse", "HEAD")
        self.store = workflow.WorkflowStore(root / "state")
        self.store.create_task(
            {
                "schema_version": "ai-task-1",
                "task_id": TASK_ID,
                "task_type": "PLAN",
                "objective": "Gate release on a fresh ACCEPT verdict",
                "repository_root": str(self.repo),
                "source_worktree": None,
                "base_commit": None,
                "candidate_commit": None,
                "authoritative_files": ["README.md"],
                "allowed_write_paths": [],
                "forbidden_actions": ["merge", "push", "change_constitution"],
                "risk_flags": [],
                "acceptance_commands": [],
                "verification_level": "L1",
                "human_gates": ["PLAN_APPROVAL"],
            }
        )
        self.evidence = _issuer_evidence(observed_cwd=str(self.repo))
        self.issuer_id = _evidence_id(self.evidence)
        self.store.append_task_ledger(TASK_ID, "runtime-evidence.jsonl", self.evidence)
        self.store.append_event(
            TASK_ID,
            {
                "event_type": "RUNTIME_EVIDENCE_RECORDED",
                "attempt_id": self.evidence["attempt_id"],
                "requested_role": self.evidence["requested_role"],
                "runtime_evidence_sha256": self.issuer_id,
            },
        )
        self.actor = "owner"
        self.decision = {
            "event_type": "OWNER_DECISION",
            "decision": "defer",
            "actor": self.actor,
            "timestamp_utc": "2026-08-28T00:00:00Z",
            "previous_state": "AWAITING_OWNER_DECISION",
            "new_state": "DEFERRED",
            "task_sha256": artifacts.artifact_sha256(
                artifacts.load_artifact(self.store._require_task(TASK_ID) / "task.json")
            ),
        }
        self.store.record_decision(TASK_ID, self.decision)
        self.owner_evidence_id = artifacts.artifact_sha256(self.decision)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _evidence_ids(self) -> tuple[str, ...]:
        ids: list[str] = []
        for event in self.store.read_task_ledger(TASK_ID, "events.jsonl"):
            if event.get("event_type") != "RUNTIME_EVIDENCE_RECORDED":
                continue
            digest = event.get("runtime_evidence_sha256")
            if isinstance(digest, str) and digest:
                ids.append(digest)
        return tuple(ids)

    def _capture(self, *, baseline: str) -> candidate_state.CandidateState:
        return candidate_state.capture_candidate_state(
            self.store,
            TASK_ID,
            baseline_commit=baseline,
            runtime_evidence_ids=self._evidence_ids(),
        )

    def _record(
        self,
        *,
        verdict: str,
        state: candidate_state.CandidateState | None = None,
        recorded_at: str = "2026-08-28T12:00:00Z",
    ) -> candidate_state.CandidateState:
        captured = state or self._capture(baseline=self.commit_b)
        with self.store.lock(TASK_ID):
            verdicts.record_final_verdict(
                self.store,
                TASK_ID,
                verdict=verdict,
                candidate_state=captured,
                issuer_evidence_id=self.issuer_id,
                recorded_at=recorded_at,
            )
        return captured

    def _require(self, **kwargs: object) -> None:
        with self.store.lock(TASK_ID):
            verdicts.require_verdict_fresh_locked(self.store, TASK_ID, **kwargs)

    def _issue_override(self, digest: str) -> authorizations.OwnerAuthorization:
        return authorizations.issue_owner_authorization(
            self.store,
            TASK_ID,
            authorization_type="VERDICT_STALE_OVERRIDE",
            actor=self.actor,
            owner_evidence_id=self.owner_evidence_id,
            issued_at_utc="2026-08-28T13:00:00Z",
            candidate_state_digest=digest,
        )

    def _consumptions(self) -> list[dict[str, object]]:
        return [
            row
            for row in authorizations.replay_authorizations(self.store, TASK_ID)
            if row.get("record_kind") == "consumption"
        ]

    def test_signatures_have_no_current_or_baseline_commit(self) -> None:
        for function in (
            verdicts.require_verdict_fresh_locked,
            verdicts.require_verdict_fresh,
        ):
            parameters = inspect.signature(function).parameters
            self.assertNotIn("current", parameters)
            self.assertNotIn("baseline_commit", parameters)
            self.assertEqual(
                ("override_authorization_id",),
                tuple(
                    name
                    for name, param in parameters.items()
                    if param.kind is inspect.Parameter.KEYWORD_ONLY
                ),
            )

    def test_locked_variant_asserts_lock_first_and_wrapper_only_delegates(self) -> None:
        self.assertEqual(
            "_assert_lock_held",
            _first_call_name(verdicts.require_verdict_fresh_locked),
        )
        tree = ast.parse(inspect.getsource(verdicts.require_verdict_fresh))
        func = tree.body[0]
        stmts = [
            node
            for node in func.body
            if not (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            )
        ]
        self.assertEqual(1, len(stmts))
        self.assertIsInstance(stmts[0], ast.With)

    def test_empty_history_is_verdict_missing_without_baseline(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_MISSING"):
            self._require()

    def test_locked_variant_requires_held_lock(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            verdicts.require_verdict_fresh_locked(self.store, TASK_ID)

    def test_fresh_reject_never_releases(self) -> None:
        self._record(verdict="REJECT")
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_REJECTED"):
            self._require()

    def test_stale_reject_never_consumes_override(self) -> None:
        self._record(verdict="REJECT")
        (self.repo / "src" / "app.py").write_text("drift\n", encoding="utf-8")
        current = self._capture(baseline=self.commit_b)
        issued = self._issue_override(current.state_digest())
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_REJECTED"):
            self._require(override_authorization_id=issued.authorization_id)
        self.assertEqual([], self._consumptions())

    def test_stale_accept_without_override_is_blocked(self) -> None:
        self._record(verdict="ACCEPT")
        (self.repo / "src" / "app.py").write_text("drift\n", encoding="utf-8")
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_STALE"):
            self._require()

    def test_fresh_accept_releases(self) -> None:
        self._record(verdict="ACCEPT")
        self._require()

    def test_rerecorded_accept_releases_after_stale(self) -> None:
        self._record(verdict="ACCEPT", recorded_at="2026-08-28T12:00:00Z")
        (self.repo / "src" / "app.py").write_text("drift\n", encoding="utf-8")
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_STALE"):
            self._require()
        self._record(verdict="ACCEPT", recorded_at="2026-08-28T14:00:00Z")
        self._require()

    def test_stale_override_releases_once_then_consumed(self) -> None:
        self._record(verdict="ACCEPT")
        (self.repo / "src" / "app.py").write_text("drift\n", encoding="utf-8")
        current = self._capture(baseline=self.commit_b)
        issued = self._issue_override(current.state_digest())
        self._require(override_authorization_id=issued.authorization_id)
        consumptions = self._consumptions()
        self.assertEqual(1, len(consumptions))
        self.assertEqual(
            {"candidate_state_digest": current.state_digest()},
            consumptions[0]["binding"],
        )
        with self.assertRaisesRegex(artifacts.WorkflowError, "AUTHORIZATION_CONSUMED"):
            self._require(override_authorization_id=issued.authorization_id)

    def test_override_digest_mismatch_is_scope_error(self) -> None:
        self._record(verdict="ACCEPT")
        (self.repo / "src" / "app.py").write_text("drift\n", encoding="utf-8")
        issued = self._issue_override("f" * 64)
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_SCOPE_MISMATCH"
        ):
            self._require(override_authorization_id=issued.authorization_id)
        self.assertEqual([], self._consumptions())

    def test_override_release_then_mutation_is_stale_again(self) -> None:
        self._record(verdict="ACCEPT")
        (self.repo / "src" / "app.py").write_text("drift-1\n", encoding="utf-8")
        current = self._capture(baseline=self.commit_b)
        issued = self._issue_override(current.state_digest())
        self._require(override_authorization_id=issued.authorization_id)
        (self.repo / "src" / "app.py").write_text("drift-2\n", encoding="utf-8")
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_STALE"):
            self._require()

    def test_in_gate_baseline_comes_from_ledger_not_caller(self) -> None:
        captured_a = self._capture(baseline=self.commit_a)
        mixed = replace(captured_a, baseline_commit=self.commit_b)
        self._record(verdict="ACCEPT", state=mixed)
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_STALE"):
            self._require()
        fresh_under_a = verdicts.evaluate_verdict_freshness(
            self.store,
            TASK_ID,
            current=replace(captured_a, baseline_commit=self.commit_b),
        )
        self.assertEqual("FRESH", fresh_under_a)

    def test_tampered_ledger_baseline_is_corrupt(self) -> None:
        self._record(verdict="ACCEPT")
        path = self.store._require_task(TASK_ID) / verdicts.FINAL_VERDICT_LEDGER
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        record["candidate_state"]["baseline_commit"] = self.commit_a
        path.write_text(artifacts.canonical_json(record) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(artifacts.WorkflowError, "VERDICT_LEDGER_CORRUPT"):
            self._require()

    def test_release_completion_phases_constant(self) -> None:
        self.assertEqual(
            frozenset({"SOL_XHIGH_TERMINAL_REPAIR"}),
            verdicts.RELEASE_COMPLETION_PHASES,
        )


class OldAcceptanceLedgerUnchangedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = adversarial_acceptance.AcceptanceLedgerV2ContractTest()
        self.harness.setUp()

    def tearDown(self) -> None:
        self.harness.tearDown()

    def test_replay_fields_and_v2_append_event_set_unchanged(self) -> None:
        self.harness._open_with_owner("owner")
        first = repairs.replay_acceptance_ledger(
            self.harness.store, self.harness.TASK_ID
        )
        second = repairs.replay_acceptance_ledger(
            self.harness.store, self.harness.TASK_ID
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.phase_outcomes, second.phase_outcomes)
        self.assertEqual(
            first.current_candidate_commit, second.current_candidate_commit
        )
        self.assertEqual(first.whole_project_final, second.whole_project_final)
        self.assertEqual({}, first.phase_outcomes)
        self.assertEqual(self.harness.input_candidate, first.current_candidate_commit)
        self.assertFalse(first.whole_project_final)
        events = [
            json.loads(line)
            for line in (
                self.harness.store._require_task(self.harness.TASK_ID) / "events.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        opened = [
            event for event in events if event.get("event_type") == "ACCEPTANCE_OPENED"
        ]
        self.assertEqual(1, len(opened))
        self.assertEqual(FROZEN_V2_APPEND_OPENED_FIELDS, set(opened[0]))
        self.assertEqual("adversarial-acceptance-1", opened[0]["ledger_version"])
        self.assertEqual("adversarial-acceptance-1", repairs._ACCEPTANCE_LEDGER_VERSION)

    def test_acceptance_events_do_not_carry_override_fields(self) -> None:
        self.harness._open_with_owner("owner")
        _, first, receipt = self.harness._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        self.harness._review(first, receipt, "ACCEPT")
        forbidden = {
            "override_authorization_id",
            "authorization_id",
            "verdict_id",
            "freshness",
            "candidate_state_digest",
        }
        for event in self.harness._events():
            self.assertEqual("adversarial-acceptance-1", event["ledger_version"])
            self.assertFalse(forbidden & set(event))


if __name__ == "__main__":
    unittest.main()
