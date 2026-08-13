"""Acceptance-ledger v2 contract tests.

These tests intentionally exercise the public v2 ledger API rather than the
legacy ``repair-ledger-1`` helpers.  The fixture is a real filesystem-backed
``WorkflowStore`` with deterministic task, actor, candidate, and finding
identities; no v2 behavior is mocked here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from collections import Counter
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_repairs as repairs


class AcceptanceContractMutationTest(unittest.TestCase):
    """Prove the repaired assertions kill the three reviewed unsafe stubs."""

    COMMON_EVENT_FIELDS = {
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

    def test_receipt_identity_contract_kills_assignment_attempt_only_verifier(self):
        expected = {
            "assignment_id": "assignment-001",
            "attempt_id": "attempt-001",
            "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
            "runtime_instance_id": "runtime-review-two",
            "native_agent_uuid": None,
            "codex_thread_id": "thread-review-two",
        }
        forgeries = {
            "execution_surface": {
                **expected,
                "execution_surface": "NATIVE_SUBAGENT",
            },
            "runtime_instance_id": {
                **expected,
                "runtime_instance_id": "runtime-forged",
            },
            "native_or_codex_identity_source": {
                **expected,
                "codex_thread_id": "thread-forged",
            },
        }

        def unsafe_verifier(receipt: dict[str, object]) -> bool:
            return (
                receipt["assignment_id"],
                receipt["attempt_id"],
            ) == (
                expected["assignment_id"],
                expected["attempt_id"],
            )

        accepted_forgeries = [
            dimension
            for dimension, receipt in forgeries.items()
            if unsafe_verifier(receipt)
        ]
        with self.assertRaisesRegex(AssertionError, "execution_surface"):
            self.assertEqual([], accepted_forgeries)
        self.assertEqual(
            {
                "execution_surface",
                "runtime_instance_id",
                "native_or_codex_identity_source",
            },
            set(accepted_forgeries),
        )

    def test_attempt_lifecycle_contract_kills_success_after_failed_same_attempt(self):
        unsafe_events = [
            ("ASSIGNMENT_ATTEMPT_STARTED", "assignment-001", "attempt-001"),
            ("ASSIGNMENT_ATTEMPT_FAILED", "assignment-001", "attempt-001"),
            ("REPAIR_COMPLETED", "assignment-001", "attempt-001"),
        ]
        safe_retry_events = [
            ("ASSIGNMENT_ATTEMPT_STARTED", "assignment-001", "attempt-001"),
            ("ASSIGNMENT_ATTEMPT_FAILED", "assignment-001", "attempt-001"),
            ("ASSIGNMENT_ATTEMPT_STARTED", "assignment-002", "attempt-002"),
            ("REPAIR_COMPLETED", "assignment-002", "attempt-002"),
        ]

        def assert_one_result_per_attempt(
            events: list[tuple[str, str, str]],
        ) -> None:
            terminal_types = {
                "ASSIGNMENT_ATTEMPT_FAILED",
                "REPAIR_COMPLETED",
                "REVIEW_COMPLETED",
            }
            starts = Counter(
                (assignment_id, attempt_id)
                for event_type, assignment_id, attempt_id in events
                if event_type == "ASSIGNMENT_ATTEMPT_STARTED"
            )
            results = Counter(
                (assignment_id, attempt_id)
                for event_type, assignment_id, attempt_id in events
                if event_type in terminal_types
            )
            self.assertEqual(starts, results)

        with self.assertRaises(AssertionError):
            assert_one_result_per_attempt(unsafe_events)
        assert_one_result_per_attempt(safe_retry_events)

    def test_common_event_binding_kills_field_presence_only_validator(self):
        expected_event = {
            "ledger_version": "adversarial-acceptance-1",
            "event_type": "ASSIGNMENT_ATTEMPT_FAILED",
            "event_index": 3,
            "event_id": "event-003",
            "previous_event_id": "event-002",
            "timestamp_utc": "2026-08-09T00:00:00Z",
            "task_id": "AWF-20260809-901",
            "task_sha256": "task-sha256",
            "base_commit": "base-commit",
            "candidate_commit": "candidate-commit",
        }
        forged_cross_task_event = {
            **expected_event,
            "task_id": "AWF-20260809-902",
        }
        expected_bindings = {
            field: expected_event[field]
            for field in (
                "task_id",
                "task_sha256",
                "base_commit",
                "candidate_commit",
            )
        }

        def unsafe_field_presence_only_validator(record: dict[str, object]) -> bool:
            return set(record) == self.COMMON_EVENT_FIELDS

        self.assertTrue(
            unsafe_field_presence_only_validator(forged_cross_task_event),
            "the unsafe stub demonstrates why common-field presence is insufficient",
        )
        self.assertEqual(
            expected_bindings,
            {field: expected_event[field] for field in expected_bindings},
        )
        with self.assertRaisesRegex(AssertionError, "task_id"):
            self.assertEqual(
                expected_bindings,
                {
                    field: forged_cross_task_event[field]
                    for field in expected_bindings
                },
            )


class AcceptanceLedgerV2ContractTest(unittest.TestCase):
    """Contract suite for the approved ``adversarial-acceptance-1`` ledger."""

    TASK_ID = "AWF-20260809-901"
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        temporary_root = Path(self.temporary_directory.name)
        self.repository_root = temporary_root / "repository"
        self.repository_root.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "acceptance-tests@example.invalid")
        self._git("config", "user.name", "Acceptance Contract Tests")
        (self.repository_root / "README.md").write_text(
            "base\n", encoding="utf-8"
        )
        self._git("add", "README.md")
        self._git("commit", "-q", "-m", "base")
        self.base_commit = self._git("rev-parse", "HEAD")
        (self.repository_root / "src").mkdir()
        (self.repository_root / "src" / "alpha.py").write_text(
            "BASE_ALPHA = True\n", encoding="utf-8"
        )
        (self.repository_root / "src" / "beta.py").write_text(
            "BASE_BETA = True\n", encoding="utf-8"
        )
        self._git("add", "src/alpha.py", "src/beta.py")
        self._git("commit", "-q", "-m", "input candidate")
        self.input_candidate = self._git("rev-parse", "HEAD")
        self.state_root = temporary_root / "state"
        self.store = workflow.WorkflowStore(self.state_root)
        self.task = {
            "schema_version": "ai-task-1",
            "task_id": self.TASK_ID,
            "task_type": "REMEDIATION",
            "objective": "Repair the bounded candidate under adversarial acceptance",
            "repository_root": str(self.repository_root),
            "source_worktree": None,
            "base_commit": self.base_commit,
            "candidate_commit": self.input_candidate,
            "authoritative_files": ["README.md"],
            "allowed_write_paths": ["src/"],
            "forbidden_actions": ["merge", "push"],
            "risk_flags": [],
            "acceptance_commands": ["git diff --check"],
            "verification_level": "L2",
            "human_gates": ["PLAN_APPROVAL", "EXECUTION_APPROVAL"],
        }
        self.findings = (
            repairs.RepairFinding("finding-001", ("src/alpha.py",)),
            repairs.RepairFinding("finding-002", ("src/beta.py",)),
        )
        self._recorded_runtime_attempts: set[str] = set()

    def _git(self, *args: str) -> str:
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_DATE": "2026-08-09T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-08-09T00:00:00Z",
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repository_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def _commit_file(self, relative_path: str, content: str) -> str:
        path = self.repository_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", f"change {relative_path}")
        return self._git("rev-parse", "HEAD")

    def _commit_symlink(self, relative_path: str, target: str) -> str:
        path = self.repository_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            path.unlink()
        path.symlink_to(target)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", f"symlink {relative_path}")
        return self._git("rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    # ------------------------------------------------------------------
    # Deterministic local fixtures
    # ------------------------------------------------------------------
    def _v2(self) -> dict[str, object]:
        """Resolve v2 symbols without an import-time crash on the v1 branch."""

        names = (
            "AcceptanceAssignment",
            "VerifiedActorReceipt",
            "AssignmentCapability",
            "AdversarialEvidence",
            "open_task_acceptance",
            "issue_acceptance_assignment",
            "complete_acceptance_assignment",
            "record_adversarial_review",
            "replay_acceptance_ledger",
            "repair_ledger_claims_task",
            "execute_adversarial_evidence",
        )
        missing = [name for name in names if not hasattr(repairs, name)]
        if missing:
            self.fail(
                "v2 acceptance API is not implemented (legacy repair-ledger-1 is not v2): "
                + ", ".join(missing)
            )
        return {name: getattr(repairs, name) for name in names}

    def _receipt(
        self,
        label: str,
        role: str,
        assignment_id: str | None = None,
        *,
        runtime_instance_id: str | None = None,
        attempt_number: int = 1,
        attempt_id: str | None = None,
        execution_surface: str = "CODEX_EXEC_ROLE_CONTRACT",
        native_agent_uuid: str | None = None,
        codex_thread_id: str | None = None,
        observed_sandbox_policy: str | None = None,
        observed_permission_profile: str | None = None,
    ) -> object:
        api = self._v2()
        assignment_id = assignment_id or hashlib.sha256(
            f"fixture:{label}:{role}".encode("utf-8")
        ).hexdigest()
        source_id = (
            native_agent_uuid
            if execution_surface == "NATIVE_SUBAGENT"
            else codex_thread_id
        )
        if source_id is None:
            source_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{'native' if execution_surface == 'NATIVE_SUBAGENT' else 'codex'}:{label}",
                )
            )
        runtime = runtime_instance_id or source_id
        effective_attempt_id = attempt_id or f"{label}-attempt-{attempt_number}"
        if role in {"luna", "terra_xhigh"}:
            model = "gpt-5.6-luna" if role == "luna" else "gpt-5.6-terra"
            effort = "max" if role == "luna" else "xhigh"
            sandbox = "workspace-write"
            permission = "workspace-write"
        elif role == "sol_xhigh":
            model, effort, sandbox, permission = (
                "gpt-5.6-sol",
                "xhigh",
                "workspace-write",
                "assignment-scoped-write",
            )
        else:
            model, effort, sandbox, permission = (
                "gpt-5.6-terra" if role.startswith("terra") else "gpt-5.6-sol",
                "xhigh" if role.startswith("terra") else "medium",
                "read-only",
                "read-only",
            )
        sandbox = observed_sandbox_policy or sandbox
        permission = observed_permission_profile or permission
        native_id = source_id if execution_surface == "NATIVE_SUBAGENT" else None
        codex_id = source_id if execution_surface == "CODEX_EXEC_ROLE_CONTRACT" else None
        evidence = {
            "schema_version": "runtime-evidence-1",
            "attempt_id": effective_attempt_id,
            "requested_role": role,
            "execution_surface": execution_surface,
            "observed_agent_type": role if execution_surface == "NATIVE_SUBAGENT" else None,
            "observed_model": model,
            "observed_reasoning_effort": effort,
            "observed_sandbox_policy": sandbox,
            "observed_permission_profile": permission,
            "observed_cwd": str(self.repository_root),
            "evidence_source": "NATIVE_METADATA" if execution_surface == "NATIVE_SUBAGENT" else "LOCAL_ROLLOUT",
            "observed_at_utc": "2026-08-09T00:00:00+00:00",
            "verification_status": "VERIFIED",
            "failure_reasons": [],
        }
        evidence_hash = hashlib.sha256(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if execution_surface == "NATIVE_SUBAGENT":
            codex_id = None
        else:
            native_id = None
        return api["VerifiedActorReceipt"](
            assignment_id=assignment_id,
            execution_surface=execution_surface,
            runtime_instance_id=runtime,
            attempt_id=effective_attempt_id,
            requested_role=role,
            observed_model=model,
            observed_reasoning_effort=effort,
            observed_sandbox_policy=sandbox,
            observed_permission_profile=permission,
            observed_cwd=str(self.repository_root),
            runtime_evidence_sha256=evidence_hash,
            native_agent_uuid=native_id,
            codex_thread_id=codex_id,
        )

    def _expected_actor(
        self,
        label: str,
        role: str,
        *,
        runtime_instance_id: str | None = None,
        execution_surface: str = "CODEX_EXEC_ROLE_CONTRACT",
    ) -> object:
        runtime = runtime_instance_id or str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{'native' if execution_surface == 'NATIVE_SUBAGENT' else 'codex'}:{label}",
            )
        )
        return repairs.ActorIdentity(
            identity=f"{execution_surface}:{runtime}",
            role=role,
        )

    def _receipt_for(
        self,
        assignment: object,
        label: str,
        role: str,
        **kwargs: object,
    ) -> object:
        return self._receipt(
            label,
            role,
            assignment.assignment_id,
            attempt_id=assignment.attempt_id,
            **kwargs,
        )

    def _open_owner_receipt(self, label: str, role: str = "luna") -> object:
        return self._receipt(
            label,
            role,
            hashlib.sha256(f"open:{self.TASK_ID}".encode("utf-8")).hexdigest(),
        )

    def _open_with_owner(self, label: str, role: str = "luna") -> tuple[object, object]:
        expected = self._expected_actor(label, role)
        receipt = self._open_owner_receipt(label, role)
        self._open(receipt)
        return expected, receipt

    def _evidence(self, label: str = "review") -> object:
        api = self._v2()
        return api["execute_adversarial_evidence"](self.store, self.TASK_ID)

    def _create_task(self, task: dict[str, object] | None = None) -> None:
        self.store.create_task(dict(task or self.task))

    def _record_runtime_evidence(self, receipt: object) -> None:
        self.assertIsInstance(receipt, repairs.VerifiedActorReceipt)
        if receipt.attempt_id in self._recorded_runtime_attempts:
            return
        evidence = {
            "schema_version": "runtime-evidence-1",
            "attempt_id": receipt.attempt_id,
            "requested_role": receipt.requested_role,
            "execution_surface": receipt.execution_surface,
            "observed_agent_type": receipt.requested_role if receipt.execution_surface == "NATIVE_SUBAGENT" else None,
            "observed_model": receipt.observed_model,
            "observed_reasoning_effort": receipt.observed_reasoning_effort,
            "observed_sandbox_policy": receipt.observed_sandbox_policy,
            "observed_permission_profile": receipt.observed_permission_profile,
            "observed_cwd": receipt.observed_cwd,
            "evidence_source": "NATIVE_METADATA" if receipt.execution_surface == "NATIVE_SUBAGENT" else "LOCAL_ROLLOUT",
            "observed_at_utc": "2026-08-09T00:00:00+00:00",
            "verification_status": "VERIFIED",
            "failure_reasons": [],
        }
        encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(encoded.encode("utf-8")).hexdigest(), receipt.runtime_evidence_sha256)
        workflow.write_runtime_evidence(self.store, self.TASK_ID, evidence)
        identity_field = "native_agent_uuid" if receipt.execution_surface == "NATIVE_SUBAGENT" else "thread_id"
        identity = receipt.native_agent_uuid if identity_field == "native_agent_uuid" else receipt.codex_thread_id
        self.store.append_event(
            self.TASK_ID,
            {
                "event_type": "RUNTIME_EVIDENCE_RECORDED",
                "attempt_id": receipt.attempt_id,
                "requested_role": receipt.requested_role,
                "execution_surface": receipt.execution_surface,
                identity_field: identity,
            },
        )
        self._recorded_runtime_attempts.add(receipt.attempt_id)

    def _open(self, owner: object) -> object:
        api = self._v2()
        self._create_task()
        self._record_runtime_evidence(owner)
        return api["open_task_acceptance"](self.store, self.task, owner)

    def _issue(self, phase: str, expected_actor: object) -> object:
        api = self._v2()
        assignment = api["issue_acceptance_assignment"](
            self.store, self.TASK_ID, phase, expected_actor
        )
        self.assertEqual(phase, assignment.phase)
        self.assertEqual(self.TASK_ID, assignment.task_id)
        self.assertEqual(expected_actor, assignment.expected_actor)
        self.assertRegex(assignment.assignment_id, r"^[0-9a-f]{64}$")
        self.assertEqual(assignment.assignment_id, assignment.capability.assignment_id)
        self.assertEqual(self.TASK_ID, assignment.capability.task_id)
        self.assertEqual(assignment.phase, assignment.capability.phase)
        self.assertEqual(assignment.assignment_id, assignment.capability.assignment_id)
        return assignment

    def _issue_with_receipt(
        self,
        phase: str,
        label: str,
        role: str,
        *,
        expected_actor: object | None = None,
        **receipt_kwargs: object,
    ) -> tuple[object, object, object]:
        expected = expected_actor or self._expected_actor(label, role)
        assignment = self._issue(phase, expected)
        if phase == "OWNER_REPAIR":
            surface, runtime = expected.identity.split(":", 1)
            receipt_kwargs = {
                **receipt_kwargs,
                "runtime_instance_id": runtime,
                "execution_surface": surface,
                **(
                    {"native_agent_uuid": runtime}
                    if surface == "NATIVE_SUBAGENT"
                    else {"codex_thread_id": runtime}
                ),
            }
        receipt = self._receipt_for(
            assignment,
            label,
            role,
            **(
                {
                    **receipt_kwargs,
                    "observed_sandbox_policy": "workspace-write",
                    "observed_permission_profile": "assignment-scoped-write",
                }
                if phase == "SOL_MEDIUM_REPAIR"
                else receipt_kwargs
            ),
        )
        self._record_runtime_evidence(receipt)
        return expected, assignment, receipt

    def _complete(
        self,
        assignment: object,
        actor: object,
        output_candidate: str,
        changed_paths: tuple[str, ...] = ("src/alpha.py",),
    ) -> object:
        with self.store.lock(self.TASK_ID):
            replay = repairs.replay_acceptance_ledger(self.store, self.TASK_ID)
            self.assertIsNotNone(replay)
            stored_task = workflow.load_task(
                self.store._require_task(self.TASK_ID) / "task.json"
            )
            attestation = repairs.ControllerExecutionAttestation(
                task_id=self.TASK_ID,
                task_sha256=assignment.capability.task_sha256,
                assignment_id=assignment.assignment_id,
                capability_id=assignment.capability.capability_id,
                candidate_commit=replay.current_candidate_commit,
                actor_receipt=actor,
            )
            repairs._v2_start_attempt(
                self.store,
                self.TASK_ID,
                replay,
                repairs._v2_context(self.store, self.TASK_ID),
                assignment,
                actor,
                stored_task,
                attestation,
            )
        with self.store.lock(self.TASK_ID):
            replay = repairs.replay_acceptance_ledger(self.store, self.TASK_ID)
            self.assertIsNotNone(replay)
            context = repairs._v2_context(self.store, self.TASK_ID)
            stored_task = workflow.load_task(
                self.store._require_task(self.TASK_ID) / "task.json"
            )
            try:
                repairs._v2_require_issued_assignment(replay, assignment)
                if replay.started_receipts.get(assignment.assignment_id) != actor:
                    raise workflow.WorkflowError(
                        "ACCEPTANCE_SEQUENCE_INVALID", "test controller receipt drifted"
                    )
                actual = repairs._v2_validate_repair_output(
                    stored_task, assignment, output_candidate, changed_paths
                )
                fields = {
                    "assignment_id": assignment.assignment_id,
                    "attempt_id": assignment.attempt_id,
                    "actor_receipt": repairs._v2_receipt_payload(actor),
                    "changed_paths": list(actual),
                    "actual_changed_paths": list(actual),
                    "output_candidate_commit": output_candidate,
                }
                if assignment.phase == "SOL_XHIGH_TERMINAL_REPAIR":
                    fields.update(
                        {
                            "terminal_state": "TASK_TERMINAL",
                            "terminal_reason": "SOL_XHIGH_TERMINAL_REPAIR_COMPLETED",
                            "whole_project_acceptance_required": "PENDING",
                        }
                    )
                repairs._v2_append(
                    self.store,
                    self.TASK_ID,
                    replay,
                    context,
                    "REPAIR_COMPLETED",
                    output_candidate,
                    fields,
                )
            except RuntimeError as exc:
                repairs._v2_fail_attempt(
                    self.store,
                    self.TASK_ID,
                    replay,
                    context,
                    assignment,
                    exc,
                )
                raise

    def _review(
        self,
        assignment: object,
        reviewer: object,
        verdict: str,
        findings: tuple[object, ...] = (),
        evidence: object | None = None,
    ) -> object:
        with self.store.lock(self.TASK_ID):
            replay = repairs.replay_acceptance_ledger(self.store, self.TASK_ID)
            self.assertIsNotNone(replay)
            stored_task = workflow.load_task(
                self.store._require_task(self.TASK_ID) / "task.json"
            )
            attestation = repairs.ControllerExecutionAttestation(
                task_id=self.TASK_ID,
                task_sha256=assignment.capability.task_sha256,
                assignment_id=assignment.assignment_id,
                capability_id=assignment.capability.capability_id,
                candidate_commit=replay.current_candidate_commit,
                actor_receipt=reviewer,
            )
            repairs._v2_start_attempt(
                self.store,
                self.TASK_ID,
                replay,
                repairs._v2_context(self.store, self.TASK_ID),
                assignment,
                reviewer,
                stored_task,
                attestation,
            )
        with self.store.lock(self.TASK_ID):
            replay = repairs.replay_acceptance_ledger(self.store, self.TASK_ID)
            self.assertIsNotNone(replay)
            context = repairs._v2_context(self.store, self.TASK_ID)
            stored_task = workflow.load_task(
                self.store._require_task(self.TASK_ID) / "task.json"
            )
            try:
                repairs._v2_require_issued_assignment(replay, assignment)
                if replay.started_receipts.get(assignment.assignment_id) != reviewer:
                    raise workflow.WorkflowError(
                        "ACCEPTANCE_SEQUENCE_INVALID", "test controller receipt drifted"
                    )
                if assignment.phase not in repairs._REVIEW_PHASES or verdict not in {
                    "ACCEPT",
                    "REWORK",
                }:
                    raise workflow.WorkflowError(
                        "ACCEPTANCE_SEQUENCE_INVALID", "test controller verdict is invalid"
                    )
                controller_evidence = repairs.execute_adversarial_evidence(
                    self.store, self.TASK_ID, replay.current_candidate_commit
                )
                supplied_evidence = evidence or controller_evidence
                if supplied_evidence != controller_evidence:
                    raise workflow.WorkflowError(
                        "ACCEPTANCE_EVIDENCE_INVALID", "test evidence was not controller-run"
                    )
                frozen_findings = repairs._v2_findings(findings)
                if verdict == "ACCEPT" and frozen_findings:
                    raise workflow.WorkflowError(
                        "ACCEPTANCE_SEQUENCE_INVALID", "acceptance has findings"
                    )
                if verdict == "REWORK":
                    if not frozen_findings:
                        raise workflow.WorkflowError(
                            "ACCEPTANCE_SEQUENCE_INVALID", "rework lacks findings"
                        )
                    repairs._v2_assert_findings_within_task(
                        frozen_findings, stored_task
                    )
                    if assignment.phase != "REVIEW_1" and not set(
                        repairs._v2_allowed_paths(frozen_findings)
                    ).issubset(assignment.allowed_paths):
                        raise workflow.WorkflowError(
                            "ACCEPTANCE_SEQUENCE_INVALID", "rework expanded scope"
                        )
                fields = {
                    "assignment_id": assignment.assignment_id,
                    "attempt_id": assignment.attempt_id,
                    "reviewer_receipt": repairs._v2_receipt_payload(reviewer),
                    "verdict": verdict,
                    "findings": repairs._v2_findings_payload(frozen_findings),
                    "evidence": dataclasses.asdict(controller_evidence),
                    "evidence_sha256": repairs._v2_sha256(
                        dataclasses.asdict(controller_evidence)
                    ),
                }
                if verdict == "ACCEPT":
                    fields.update(
                        {
                            "terminal_state": "TASK_TERMINAL",
                            "terminal_reason": f"{assignment.phase}_ACCEPTED",
                            "whole_project_acceptance_required": "PENDING",
                        }
                    )
                repairs._v2_append(
                    self.store,
                    self.TASK_ID,
                    replay,
                    context,
                    "REVIEW_COMPLETED",
                    replay.current_candidate_commit,
                    fields,
                )
            except RuntimeError as exc:
                repairs._v2_fail_attempt(
                    self.store,
                    self.TASK_ID,
                    replay,
                    context,
                    assignment,
                    exc,
                )
                raise

    def _events(self) -> list[dict[str, object]]:
        path = self.state_root / self.TASK_ID / "events.jsonl"
        return [
            event
            for line in path.read_text(encoding="utf-8").splitlines()
            if (event := json.loads(line)).get("ledger_version") == "adversarial-acceptance-1"
        ]

    def _assert_chain(self) -> list[dict[str, object]]:
        events = self._events()
        self.assertTrue(events)
        allowed_types = {
            "ACCEPTANCE_OPENED",
            "ASSIGNMENT_ISSUED",
            "ASSIGNMENT_ATTEMPT_STARTED",
            "ASSIGNMENT_ATTEMPT_FAILED",
            "REPAIR_COMPLETED",
            "REVIEW_COMPLETED",
        }
        previous_id: str | None = None
        indices: list[int] = []
        for event in events:
            self.assertEqual("adversarial-acceptance-1", event["ledger_version"])
            self.assertIn(event["event_type"], allowed_types)
            self.assertEqual(self.TASK_ID, event["task_id"])
            self.assertRegex(event["task_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(self.task["base_commit"], event["base_commit"])
            self.assertRegex(event["candidate_commit"], r"^[0-9a-f]{40}$")
            self.assertIsInstance(event["event_index"], int)
            indices.append(event["event_index"])
            self.assertEqual(previous_id, event["previous_event_id"])
            self.assertRegex(event["event_id"], r"^[0-9a-f]{64}$")
            without_id = {key: value for key, value in event.items() if key != "event_id"}
            encoded = json.dumps(
                without_id, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            self.assertEqual(
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(), event["event_id"]
            )
            previous_id = event["event_id"]
        self.assertEqual(list(range(indices[0], indices[0] + len(indices))), indices)
        self.assertIn(
            "ASSIGNMENT_ATTEMPT_STARTED",
            {event["event_type"] for event in events},
            "each issued assignment must launch through the v2 attempt guard",
        )
        self._assert_common_event_fields(events)
        self._assert_attempt_lifecycle(events)
        return events

    def _assert_terminal(self, events: list[dict[str, object]]) -> None:
        terminal_events = [
            event
            for event in events
            if event.get("whole_project_acceptance_required") == "PENDING"
        ]
        self.assertTrue(
            terminal_events,
            "every successful terminal path must explicitly remain pending whole-project acceptance",
        )
        terminal_states = [
            event
            for event in events
            if event.get("terminal_state") == "TASK_TERMINAL"
        ]
        self.assertEqual(1, len(terminal_states))
        self.assertTrue(terminal_states[0].get("terminal_reason"))
        self.assertEqual("PENDING", terminal_states[0]["whole_project_acceptance_required"])

    def _assert_terminal_ladder_closed(self) -> None:
        self.assertTrue(repairs.repair_ledger_claims_task(self.store, self.TASK_ID))
        candidates = (
            ("REVIEW_1", "terra-review-after-terminal", "terra_xhigh_reviewer"),
            ("OWNER_REPAIR", "owner-after-terminal", "luna"),
            ("REVIEW_2", "terra-review-two-after-terminal", "terra_xhigh_reviewer"),
            ("SOL_MEDIUM_REPAIR", "sol-repair-after-terminal", "sol_medium_reviewer"),
            (
                "SOL_MEDIUM_PEER_REVIEW",
                "sol-peer-after-terminal",
                "sol_medium_reviewer",
            ),
            ("SOL_XHIGH_TERMINAL_REPAIR", "sol-xhigh-after-terminal", "sol_xhigh"),
        )
        for phase, label, role in candidates:
            with self.subTest(phase=phase):
                with self.assertRaises(workflow.WorkflowError):
                    self._issue(phase, self._expected_actor(label, role))

    def _assert_assignment_binding(
        self,
        assignment: object,
        phase: str,
        expected_actor: object,
        candidate: str | None = None,
    ) -> None:
        self.assertEqual(phase, assignment.phase)
        self.assertEqual(self.TASK_ID, assignment.task_id)
        self.assertEqual(expected_actor, assignment.expected_actor)
        self.assertEqual(self.task["base_commit"], assignment.base_commit)
        self.assertEqual(candidate or self.input_candidate, assignment.input_candidate_commit)
        self.assertEqual(self.TASK_ID, assignment.capability.task_id)
        self.assertEqual(assignment.attempt_id, assignment.capability.attempt_id)
        self.assertTrue(assignment.capability.capability_id)
        capability = assignment.capability
        self.assertRegex(capability.task_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(self.task["base_commit"], capability.base_commit)
        self.assertEqual(assignment.input_candidate_commit, capability.input_candidate_commit)
        self.assertEqual(
            tuple(finding.finding_id for finding in assignment.findings),
            tuple(capability.finding_ids),
        )
        self.assertEqual(tuple(assignment.allowed_paths), tuple(capability.allowed_paths))
        self.assertIn("merge", tuple(capability.forbidden_actions))
        self.assertIn("push", tuple(capability.forbidden_actions))
        self.assertRegex(capability.issuing_event_id, r"^[0-9a-f]{64}$")

    def _assert_common_event_fields(self, events: list[dict[str, object]]) -> None:
        common_fields = {
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
        for event in events:
            self.assertTrue(
                common_fields.issubset(event),
                f"{event.get('event_type')} lacks an approved common ledger field",
            )

    def _assert_attempt_lifecycle(self, events: list[dict[str, object]]) -> None:
        issued = [
            (index, event)
            for index, event in enumerate(events)
            if event["event_type"] == "ASSIGNMENT_ISSUED"
        ]
        self.assertTrue(issued)
        terminal_types = {
            "ASSIGNMENT_ATTEMPT_FAILED",
            "REPAIR_COMPLETED",
            "REVIEW_COMPLETED",
        }
        issued_attempts = Counter(
            (event["assignment_id"], event["attempt_id"])
            for _, event in issued
        )
        terminal_attempts = Counter(
            (event["assignment_id"], event["attempt_id"])
            for event in events
            if event["event_type"] in terminal_types
        )
        self.assertEqual(issued_attempts, terminal_attempts)
        for issued_index, issue_event in issued:
            assignment_id = issue_event["assignment_id"]
            attempt_id = issue_event["attempt_id"]
            started = [
                (index, event)
                for index, event in enumerate(events)
                if event.get("event_type") == "ASSIGNMENT_ATTEMPT_STARTED"
                and event.get("assignment_id") == assignment_id
                and event.get("attempt_id") == attempt_id
            ]
            terminal = [
                (index, event)
                for index, event in enumerate(events)
                if event.get("event_type") in terminal_types
                and event.get("assignment_id") == assignment_id
                and event.get("attempt_id") == attempt_id
            ]
            self.assertEqual(1, len(started))
            self.assertEqual(1, len(terminal))
            self.assertLess(issued_index, started[0][0])
            self.assertLess(started[0][0], terminal[0][0])

    def _open_review_one(
        self,
        owner_label: str = "luna-owner",
        owner_role: str = "luna",
    ) -> tuple[object, object, object, object, object]:
        owner_actor, owner_open_receipt = self._open_with_owner(owner_label, owner_role)
        reviewer_actor, first, reviewer_receipt = self._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        return owner_actor, owner_open_receipt, reviewer_actor, first, reviewer_receipt

    # ------------------------------------------------------------------
    # Public API and successful terminal paths
    # ------------------------------------------------------------------
    def test_v2_types_are_frozen_and_public_without_legacy_fallback(self):
        api = self._v2()
        for name in (
            "AcceptanceAssignment",
            "VerifiedActorReceipt",
            "AssignmentCapability",
            "AdversarialEvidence",
        ):
            self.assertTrue(callable(api[name]), name)

        receipt = self._receipt(
            "frozen",
            "luna",
            hashlib.sha256(b"frozen-assignment").hexdigest(),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            receipt.runtime_instance_id = "mutated"  # type: ignore[misc]

    def test_luna_owner_review_one_accepts_to_terminal(self):
        self._open_with_owner("luna-owner", "luna")
        reviewer_actor, first, reviewer_receipt = self._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        self._assert_assignment_binding(first, "REVIEW_1", reviewer_actor)
        self.assertEqual((), tuple(first.findings))

        evidence = self._evidence("review-one-accept")
        self._review(first, reviewer_receipt, "ACCEPT", evidence=evidence)
        events = self._assert_chain()
        self._assert_terminal(events)
        review_event = next(event for event in events if event["event_type"] == "REVIEW_COMPLETED")
        self.assertEqual(review_event["evidence"]["verification_commands"], list(evidence.verification_commands))
        self.assertEqual(review_event["evidence"]["negative_checks"], list(evidence.negative_checks))
        self.assertRegex(review_event["evidence_sha256"], r"^[0-9a-f]{64}$")
        evidence_payload = json.dumps(
            dataclasses.asdict(evidence), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        self.assertEqual(
            hashlib.sha256(evidence_payload.encode("utf-8")).hexdigest(),
            review_event["evidence_sha256"],
        )
        self.assertTrue(repairs.repair_ledger_claims_task(self.store, self.TASK_ID))
        self._assert_terminal_ladder_closed()

    def test_luna_owner_rework_then_distinct_terra_review_two_accepts(self):
        owner_actor, _ = self._open_with_owner("luna-owner", "luna")
        reviewer_one_actor, first, reviewer_one_receipt = self._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        self._review(first, reviewer_one_receipt, "REWORK", self.findings)

        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        self._assert_assignment_binding(owner_repair, "OWNER_REPAIR", owner_actor)
        self.assertEqual(self.findings, tuple(owner_repair.findings))
        owner_candidate = self._commit_file("src/alpha.py", "OWNER_ALPHA = 1\n")
        self._complete(owner_repair, owner_receipt, owner_candidate, ("src/alpha.py",))

        reviewer_two_actor, second, reviewer_two_receipt = self._issue_with_receipt(
            "REVIEW_2", "terra-review-two", "terra_xhigh_reviewer"
        )
        self._assert_assignment_binding(second, "REVIEW_2", reviewer_two_actor, owner_candidate)
        self.assertNotEqual(
            (reviewer_one_receipt.execution_surface, reviewer_one_receipt.runtime_instance_id),
            (reviewer_two_receipt.execution_surface, reviewer_two_receipt.runtime_instance_id),
        )
        self.assertNotEqual(
            (owner_receipt.execution_surface, owner_receipt.runtime_instance_id),
            (reviewer_two_receipt.execution_surface, reviewer_two_receipt.runtime_instance_id),
        )
        self._review(second, reviewer_two_receipt, "ACCEPT")
        events = self._assert_chain()
        self._assert_terminal(events)
        self._assert_terminal_ladder_closed()

    def test_owner_repair_requires_the_issued_owner_runtime_receipt(self):
        owner_actor, _, _, first, first_receipt = self._open_review_one()
        self._review(first, first_receipt, "REWORK", self.findings)
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        self.assertEqual(owner_actor, owner_receipt.actor_identity)
        forged = self._receipt_for(owner_repair, "luna-owner-repair", "luna")
        candidate = self._commit_file("src/alpha.py", "OWNER_ALPHA = 1\n")
        with self.assertRaises(workflow.WorkflowError):
            self._complete(owner_repair, forged, candidate, ("src/alpha.py",))

    def test_review_rejects_model_claimed_evidence_not_executed_by_controller(self):
        self._open_with_owner("luna-owner", "luna")
        _, review, reviewer_receipt = self._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        forged = self._v2()["AdversarialEvidence"](
            verification_commands=("model says verification passed",),
            negative_checks=("model says mutation was rejected",),
            outputs=("model supplied output",),
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "ACCEPTANCE_EVIDENCE_INVALID"):
            self._review(review, reviewer_receipt, "ACCEPT", evidence=forged)
        events = self._events()
        self.assertEqual(
            1,
            sum(event["event_type"] == "ASSIGNMENT_ATTEMPT_FAILED" for event in events),
        )

    def test_orphaned_started_attempt_is_interrupted_once_before_retry(self):
        self._open_with_owner("luna-owner", "luna")
        _, review, reviewer_receipt = self._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        with self.store.lock(self.TASK_ID):
            replay = repairs.replay_acceptance_ledger(self.store, self.TASK_ID)
            self.assertIsNotNone(replay)
            repairs._v2_start_attempt(
                self.store,
                self.TASK_ID,
                replay,
                repairs._v2_context(self.store, self.TASK_ID),
                review,
                reviewer_receipt,
                workflow.load_task(self.store._require_task(self.TASK_ID) / "task.json"),
            )

        case = self

        class MustNotLaunch(repairs.ControllerAssignmentBoundary):
            def attest_execution(self, capability):
                raise AssertionError("orphan recovery must not attest a second launch")

            def execute_capability(self, capability):
                case.fail("orphan recovery must not launch a second attempt")

        with self.assertRaisesRegex(workflow.WorkflowError, "ASSIGNMENT_ATTEMPT_INTERRUPTED"):
            repairs.run_assignment(self.store, self.TASK_ID, review, MustNotLaunch())
        events = self._events()
        failures = [
            event
            for event in events
            if event["event_type"] == "ASSIGNMENT_ATTEMPT_FAILED"
        ]
        self.assertEqual(1, len(failures))
        self.assertEqual("ASSIGNMENT_ATTEMPT_INTERRUPTED", failures[0]["failure_code"])
        replacement = self._issue(
            "REVIEW_1", self._expected_actor("terra-review-retry", "terra_xhigh_reviewer")
        )
        self.assertNotEqual(review.assignment_id, replacement.assignment_id)
        self.assertEqual("review_1-attempt-2", replacement.attempt_id)

    def test_direct_review_cannot_complete_an_orphaned_started_attempt(self):
        self._open_with_owner("luna-owner", "luna")
        _, review, reviewer_receipt = self._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        with self.store.lock(self.TASK_ID):
            replay = repairs.replay_acceptance_ledger(self.store, self.TASK_ID)
            self.assertIsNotNone(replay)
            repairs._v2_start_attempt(
                self.store,
                self.TASK_ID,
                replay,
                repairs._v2_context(self.store, self.TASK_ID),
                review,
                reviewer_receipt,
                workflow.load_task(self.store._require_task(self.TASK_ID) / "task.json"),
            )
        with self.assertRaisesRegex(workflow.WorkflowError, "ASSIGNMENT_ATTEMPT_INTERRUPTED"):
            repairs.record_adversarial_review(
                self.store,
                self.TASK_ID,
                review,
                reviewer_receipt,
                "ACCEPT",
                (),
                self._evidence("accept"),
            )
        events = self._events()
        self.assertEqual(
            1,
            sum(event["event_type"] == "ASSIGNMENT_ATTEMPT_FAILED" for event in events),
        )
        self.assertFalse(
            any(event["event_type"] == "REVIEW_COMPLETED" for event in events),
        )

    def test_controller_evidence_rejects_shell_command_before_it_runs(self):
        task = dict(self.task)
        probe = self.repository_root / "shell-escape-probe"
        task["acceptance_commands"] = [f"sh -c 'touch {probe.name}'"]
        self._create_task(task)
        try:
            with self.assertRaisesRegex(workflow.WorkflowError, "ACCEPTANCE_EVIDENCE_INVALID"):
                repairs.execute_adversarial_evidence(self.store, self.TASK_ID)
            self.assertFalse(probe.exists(), "controller must not execute a shell escape")
        finally:
            probe.unlink(missing_ok=True)

    def test_controller_evidence_rejects_metacharacters_and_non_targets_before_launch(self):
        unsafe_commands = (
            "python3 -m unittest |",
            "python3 -m unittest &",
            "python3 -m unittest tests.test_alpha |",
            "python3 -m unittest tests.test_alpha &",
            "python3 -m unittest tests.test_alpha # comment",
            "python3 -m unittest tests.*",
            "python3 -m unittest ${TEST_TARGET}",
            "python3 -m unittest tests[.]test_alpha",
            "python3 -m unittest --help",
            "python3 -m unittest",
            "python3 -m unittest ''",
            "python3 -m unittest tests.test_alpha\x01",
        )
        real_run = subprocess.run

        def reject_python_launch(argv, *args, **kwargs):
            if Path(argv[0]).name.startswith("python"):
                raise AssertionError("unsafe command reached subprocess launch")
            return real_run(argv, *args, **kwargs)

        for index, command in enumerate(unsafe_commands, start=920):
            with self.subTest(command=command):
                task_id = f"AWF-20260809-{index}"
                task = dict(self.task)
                task["task_id"] = task_id
                task["acceptance_commands"] = [command]
                self.store.create_task(task)
                with mock.patch.object(
                    repairs.subprocess,
                    "run",
                    side_effect=reject_python_launch,
                ) as launched:
                    with self.assertRaisesRegex(
                        workflow.WorkflowError, "ACCEPTANCE_EVIDENCE_INVALID"
                    ):
                        repairs.execute_adversarial_evidence(self.store, task_id)
                self.assertFalse(
                    any(
                        Path(call.args[0][0]).name.startswith("python")
                        for call in launched.call_args_list
                    )
                )

    def test_public_runtime_records_and_arbitrary_boundary_cannot_authorize_accept(self):
        self._open_with_owner("luna-owner", "luna")
        _, review, reviewer_receipt = self._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        forged_evidence = repairs.execute_adversarial_evidence(
            self.store, self.TASK_ID, review.input_candidate_commit
        )
        case = self

        class CallerBoundary(repairs.ControllerAssignmentBoundary):
            def __init__(self) -> None:
                self.attestation_calls = 0
                self.execution_calls = 0

            def attest_execution(self, capability):
                self.attestation_calls += 1
                return repairs.ControllerExecutionAttestation(
                    task_id=case.TASK_ID,
                    task_sha256=capability.task_sha256,
                    assignment_id=review.assignment_id,
                    capability_id=capability.capability_id,
                    candidate_commit=review.input_candidate_commit,
                    actor_receipt=reviewer_receipt,
                )

            def execute_capability(self, capability):
                self.execution_calls += 1
                return {
                    "verdict": "ACCEPT",
                    "findings": (),
                    "evidence": forged_evidence,
                }

        boundary = CallerBoundary()
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_ADAPTER_REQUIRED"):
            repairs.run_assignment(self.store, self.TASK_ID, review, boundary)
        self.assertEqual(0, boundary.attestation_calls)
        self.assertEqual(0, boundary.execution_calls)
        sessions = self.repository_root.parent / "forged-sessions"
        sessions.mkdir()
        (sessions / f"rollout-{reviewer_receipt.runtime_instance_id}").write_text(
            json.dumps(
                {
                    "thread_id": reviewer_receipt.runtime_instance_id,
                    "agent_type": None,
                    "model": reviewer_receipt.observed_model,
                    "reasoning_effort": reviewer_receipt.observed_reasoning_effort,
                    "sandbox_policy": reviewer_receipt.observed_sandbox_policy,
                    "permission_profile": reviewer_receipt.observed_permission_profile,
                    "cwd": reviewer_receipt.observed_cwd,
                }
            ),
            encoding="utf-8",
        )
        real_run = subprocess.run

        def no_codex_launch(command, *args, **kwargs):
            if Path(command[0]).name == "codex":
                raise AssertionError("caller-authored evidence reached Codex execution")
            return real_run(command, *args, **kwargs)

        with mock.patch.object(
            workflow.subprocess, "run", side_effect=no_codex_launch
        ) as launched:
            with self.assertRaises(workflow.WorkflowError):
                repairs.run_assignment(self.store, self.TASK_ID, review, sessions)
        self.assertFalse(
            any(Path(call.args[0][0]).name == "codex" for call in launched.call_args_list)
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_ADAPTER_REQUIRED"):
            repairs.record_adversarial_review(
                self.store,
                self.TASK_ID,
                review,
                reviewer_receipt,
                "ACCEPT",
                (),
                forged_evidence,
            )
        replay = repairs.replay_acceptance_ledger(self.store, self.TASK_ID)
        self.assertIsNotNone(replay)
        self.assertFalse(replay.terminal)

    def test_controller_fixed_executor_resumes_issued_runtime_and_accepts(self):
        self._open_with_owner("luna-owner", "luna")
        reviewer_thread = str(uuid.uuid4())
        review = self._issue(
            "REVIEW_1",
            self._expected_actor(
                "terra-controller-review",
                "terra_xhigh_reviewer",
                runtime_instance_id=reviewer_thread,
            ),
        )
        sessions = self.repository_root.parent / "sessions"
        sessions.mkdir()
        (sessions / f"rollout-{reviewer_thread}").write_text(
            json.dumps(
                {
                    "thread_id": reviewer_thread,
                    "agent_type": None,
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "xhigh",
                    "sandbox_policy": "read-only",
                    "permission_profile": "read-only",
                    "cwd": str(self.repository_root),
                }
            ),
            encoding="utf-8",
        )
        real_run = subprocess.run

        def controller_process(command, *args, **kwargs):
            if Path(command[0]).name == "codex":
                self.assertIn('sandbox_mode="read-only"', command)
                output_path = Path(command[command.index("-o") + 1])
                output_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "ai-result-1",
                            "role": "terra_xhigh_reviewer",
                            "status": "ACCEPTANCE_RECOMMENDED",
                            "summary": "The issued candidate meets its frozen contract.",
                            "claims": [],
                            "evidence": [],
                            "counter_checks": [],
                            "changed_files": [],
                            "blind_spots": [],
                            "unresolved_questions": [],
                            "recommended_next_state": "AWAITING_OWNER_DECISION",
                        }
                    ),
                    encoding="utf-8",
                )
                events = "\n".join(
                    (
                        json.dumps(
                            {"type": "thread.started", "thread_id": reviewer_thread}
                        ),
                        json.dumps({"type": "turn.completed"}),
                    )
                )
                return subprocess.CompletedProcess(
                    command, 0, stdout=events + "\n", stderr=""
                )
            return real_run(command, *args, **kwargs)

        with mock.patch.object(
            workflow.subprocess, "run", side_effect=controller_process
        ):
            repairs.run_assignment(self.store, self.TASK_ID, review, sessions)
        replay = repairs.replay_acceptance_ledger(self.store, self.TASK_ID)
        self.assertIsNotNone(replay)
        self.assertTrue(replay.terminal)
        events = self._events()
        started = next(
            event
            for event in events
            if event["event_type"] == "ASSIGNMENT_ATTEMPT_STARTED"
        )
        self.assertEqual(reviewer_thread, started["actor_receipt"]["codex_thread_id"])

    def test_controller_fixed_executor_commits_only_the_issued_repair_scope(self):
        owner_actor, _ = self._open_with_owner("luna-owner", "luna")
        _, review, reviewer_receipt = self._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        self._review(review, reviewer_receipt, "REWORK", (self.findings[0],))
        owner_repair = self._issue("OWNER_REPAIR", owner_actor)
        owner_thread = owner_actor.identity.split(":", 1)[1]
        sessions = self.repository_root.parent / "owner-sessions"
        sessions.mkdir()
        (sessions / f"rollout-{owner_thread}").write_text(
            json.dumps(
                {
                    "thread_id": owner_thread,
                    "agent_type": None,
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                    "sandbox_policy": "workspace-write",
                    "permission_profile": "workspace-write",
                    "cwd": str(self.repository_root),
                }
            ),
            encoding="utf-8",
        )
        real_run = subprocess.run

        def controller_process(command, *args, **kwargs):
            if Path(command[0]).name == "codex":
                self.assertIn('sandbox_mode="workspace-write"', command)
                (self.repository_root / "src" / "alpha.py").write_text(
                    "CONTROLLER_REPAIR = True\n", encoding="utf-8"
                )
                real_run(
                    ["git", "add", "src/alpha.py"],
                    cwd=self.repository_root,
                    check=True,
                )
                real_run(
                    ["git", "commit", "-q", "-m", "controller repair"],
                    cwd=self.repository_root,
                    env={
                        **os.environ,
                        "GIT_AUTHOR_NAME": "Acceptance Contract Tests",
                        "GIT_AUTHOR_EMAIL": "acceptance-tests@example.invalid",
                        "GIT_COMMITTER_NAME": "Acceptance Contract Tests",
                        "GIT_COMMITTER_EMAIL": "acceptance-tests@example.invalid",
                    },
                    check=True,
                )
                output_path = Path(command[command.index("-o") + 1])
                output_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "ai-result-1",
                            "role": "luna",
                            "status": "IMPLEMENTED_CANDIDATE",
                            "summary": "Repaired the issued finding only.",
                            "claims": [],
                            "evidence": [],
                            "counter_checks": [],
                            "changed_files": ["src/alpha.py"],
                            "blind_spots": [],
                            "unresolved_questions": [],
                            "recommended_next_state": "PRECHECK_RUNNING",
                        }
                    ),
                    encoding="utf-8",
                )
                events = "\n".join(
                    (
                        json.dumps(
                            {"type": "thread.started", "thread_id": owner_thread}
                        ),
                        json.dumps({"type": "turn.completed"}),
                    )
                )
                return subprocess.CompletedProcess(
                    command, 0, stdout=events + "\n", stderr=""
                )
            return real_run(command, *args, **kwargs)

        with mock.patch.object(
            workflow.subprocess, "run", side_effect=controller_process
        ):
            repairs.run_assignment(
                self.store, self.TASK_ID, owner_repair, sessions
            )
        replay = repairs.replay_acceptance_ledger(self.store, self.TASK_ID)
        self.assertIsNotNone(replay)
        self.assertEqual("COMPLETED", replay.phase_outcomes["OWNER_REPAIR"])
        completion = next(
            event
            for event in reversed(self._events())
            if event["event_type"] == "REPAIR_COMPLETED"
        )
        self.assertEqual(["src/alpha.py"], completion["actual_changed_paths"])

    def test_controller_fixed_executor_binds_sol_medium_repair_write_contract(self):
        owner_actor, _ = self._open_with_owner("terra-owner", "terra_xhigh")
        _, first, reviewer_one_receipt = self._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        self._review(first, reviewer_one_receipt, "REWORK", self.findings)
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "terra-owner-repair", "terra_xhigh", expected_actor=owner_actor
        )
        owner_candidate = self._commit_file("src/alpha.py", "TERRA_OWNER_ALPHA = 1\n")
        self._complete(owner_repair, owner_receipt, owner_candidate, ("src/alpha.py",))
        _, second, reviewer_two_receipt = self._issue_with_receipt(
            "REVIEW_2", "terra-review-two", "terra_xhigh_reviewer"
        )
        self._review(second, reviewer_two_receipt, "REWORK", self.findings)
        fixer_thread = str(uuid.uuid4())
        fixer = self._issue(
            "SOL_MEDIUM_REPAIR",
            self._expected_actor(
                "sol-fixed-repair",
                "sol_medium_reviewer",
                runtime_instance_id=fixer_thread,
            ),
        )
        sessions = self.repository_root.parent / "sol-medium-sessions"
        sessions.mkdir()
        (sessions / f"rollout-{fixer_thread}").write_text(
            json.dumps(
                {
                    "thread_id": fixer_thread,
                    "agent_type": None,
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "medium",
                    "sandbox_policy": "workspace-write",
                    "permission_profile": "assignment-scoped-write",
                    "cwd": str(self.repository_root),
                }
            ),
            encoding="utf-8",
        )
        receipt = repairs._v2_controller_runtime_receipt(
            self.store, self.TASK_ID, fixer, self.task, sessions
        )
        self.assertEqual("workspace-write", receipt.observed_sandbox_policy)
        self.assertEqual(
            "assignment-scoped-write", receipt.observed_permission_profile
        )
        with self.store.lock(self.TASK_ID):
            replay = repairs.replay_acceptance_ledger(self.store, self.TASK_ID)
            self.assertIsNotNone(replay)
            repairs._v2_start_attempt(
                self.store,
                self.TASK_ID,
                replay,
                repairs._v2_context(self.store, self.TASK_ID),
                fixer,
                receipt,
                workflow.load_task(
                    self.store._require_task(self.TASK_ID) / "task.json"
                ),
                repairs.ControllerExecutionAttestation(
                    task_id=self.TASK_ID,
                    task_sha256=fixer.capability.task_sha256,
                    assignment_id=fixer.assignment_id,
                    capability_id=fixer.capability.capability_id,
                    candidate_commit=fixer.input_candidate_commit,
                    actor_receipt=receipt,
                ),
            )

    def test_controller_evidence_rejects_safe_argv_that_mutates_the_repository(self):
        tests = self.repository_root / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_mutator.py").write_text(
            "from pathlib import Path\nPath('python-mutation-probe').touch()\n",
            encoding="utf-8",
        )
        self._git("add", "tests")
        self._git("commit", "-q", "-m", "add controller mutation fixture")
        task = dict(self.task)
        task["candidate_commit"] = self._git("rev-parse", "HEAD")
        task["acceptance_commands"] = ["python3 -m unittest tests.test_mutator"]
        probe = self.repository_root / "python-mutation-probe"
        self._create_task(task)
        try:
            with self.assertRaisesRegex(workflow.WorkflowError, "ACCEPTANCE_EVIDENCE_INVALID"):
                repairs.execute_adversarial_evidence(self.store, self.TASK_ID)
        finally:
            probe.unlink(missing_ok=True)

    def test_controller_evidence_rejects_transcript_over_the_cap(self):
        tests = self.repository_root / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_noisy.py").write_text(
            "print('x' * 70000)\n",
            encoding="utf-8",
        )
        self._git("add", "tests")
        self._git("commit", "-q", "-m", "add controller transcript fixture")
        task = dict(self.task)
        task["candidate_commit"] = self._git("rev-parse", "HEAD")
        task["acceptance_commands"] = ["python3 -m unittest tests.test_noisy"]
        self._create_task(task)
        with self.assertRaisesRegex(workflow.WorkflowError, "ACCEPTANCE_EVIDENCE_INVALID"):
            repairs.execute_adversarial_evidence(self.store, self.TASK_ID)

    def test_terra_owner_rework_then_sol_peer_accepts(self):
        owner_actor, _ = self._open_with_owner("terra-owner", "terra_xhigh")
        _, first, reviewer_one_receipt = self._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        self._review(first, reviewer_one_receipt, "REWORK", self.findings)
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "terra-owner-repair", "terra_xhigh", expected_actor=owner_actor
        )
        owner_candidate = self._commit_file("src/alpha.py", "TERRA_OWNER_ALPHA = 1\n")
        self._complete(owner_repair, owner_receipt, owner_candidate, ("src/alpha.py",))
        _, second, reviewer_two_receipt = self._issue_with_receipt(
            "REVIEW_2", "terra-review-two", "terra_xhigh_reviewer"
        )
        self._review(second, reviewer_two_receipt, "REWORK", self.findings)

        sol_actor, sol_repair, sol_receipt = self._issue_with_receipt(
            "SOL_MEDIUM_REPAIR", "sol-fixer", "sol_medium_reviewer"
        )
        self._assert_assignment_binding(
            sol_repair, "SOL_MEDIUM_REPAIR", sol_actor, owner_candidate
        )
        self.assertIn("assignment", sol_repair.capability.write_authority)
        self.assertEqual(self.findings, tuple(sol_repair.findings))
        sol_candidate = self._commit_file("src/beta.py", "SOL_MEDIUM_BETA = 1\n")
        self._complete(sol_repair, sol_receipt, sol_candidate, ("src/beta.py",))
        peer_actor, peer_review, peer_receipt = self._issue_with_receipt(
            "SOL_MEDIUM_PEER_REVIEW", "sol-peer", "sol_medium_reviewer"
        )
        self._assert_assignment_binding(
            peer_review, "SOL_MEDIUM_PEER_REVIEW", peer_actor, sol_candidate
        )
        self.assertNotEqual(
            (sol_receipt.execution_surface, sol_receipt.runtime_instance_id),
            (peer_receipt.execution_surface, peer_receipt.runtime_instance_id),
        )
        self._review(peer_review, peer_receipt, "ACCEPT")
        events = self._assert_chain()
        self._assert_terminal(events)
        self._assert_terminal_ladder_closed()

    def test_sol_peer_rework_creates_one_sol_xhigh_terminal_repair(self):
        owner_actor, _ = self._open_with_owner("luna-owner", "luna")
        _, first, reviewer_one_receipt = self._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        self._review(first, reviewer_one_receipt, "REWORK", self.findings)
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        owner_candidate = self._commit_file("src/alpha.py", "LUNA_OWNER_ALPHA = 1\n")
        self._complete(owner_repair, owner_receipt, owner_candidate, ("src/alpha.py",))
        _, second, reviewer_two_receipt = self._issue_with_receipt(
            "REVIEW_2", "terra-review-two", "terra_xhigh_reviewer"
        )
        self._review(second, reviewer_two_receipt, "REWORK", self.findings)
        _, sol_repair, sol_receipt = self._issue_with_receipt(
            "SOL_MEDIUM_REPAIR", "sol-fixer", "sol_medium_reviewer"
        )
        self._assert_assignment_binding(
            sol_repair, "SOL_MEDIUM_REPAIR", sol_repair.expected_actor, owner_candidate
        )
        sol_candidate = self._commit_file("src/alpha.py", "SOL_MEDIUM_ALPHA = 1\n")
        self._complete(sol_repair, sol_receipt, sol_candidate, ("src/alpha.py",))
        peer_actor, peer_review, peer_receipt = self._issue_with_receipt(
            "SOL_MEDIUM_PEER_REVIEW", "sol-peer", "sol_medium_reviewer"
        )
        self._assert_assignment_binding(
            peer_review, "SOL_MEDIUM_PEER_REVIEW", peer_actor, sol_candidate
        )
        self._review(peer_review, peer_receipt, "REWORK", self.findings)
        with self.assertRaises(workflow.WorkflowError):
            self._issue("SOL_MEDIUM_REPAIR", self._expected_actor("sol-fixer-again", "sol_medium_reviewer"))

        terminal_thread = str(uuid.uuid4())
        terminal_actor = self._expected_actor(
            "sol-xhigh", "sol_xhigh", runtime_instance_id=terminal_thread
        )
        terminal = self._issue(
            "SOL_XHIGH_TERMINAL_REPAIR", terminal_actor
        )
        self._assert_assignment_binding(
            terminal, "SOL_XHIGH_TERMINAL_REPAIR", terminal_actor, sol_candidate
        )
        self.assertEqual("assignment-scoped-write", terminal.capability.write_authority)
        sessions = self.repository_root.parent / "sol-xhigh-sessions"
        sessions.mkdir()
        (sessions / f"rollout-{terminal_thread}").write_text(
            json.dumps(
                {
                    "thread_id": terminal_thread,
                    "agent_type": None,
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "xhigh",
                    "sandbox_policy": "workspace-write",
                    "permission_profile": "assignment-scoped-write",
                    "cwd": str(self.repository_root),
                }
            ),
            encoding="utf-8",
        )
        real_run = subprocess.run

        def terminal_controller_process(command, *args, **kwargs):
            if Path(command[0]).name == "codex":
                self.assertIn('sandbox_mode="workspace-write"', command)
                (self.repository_root / "src" / "alpha.py").write_text(
                    "SOL_XHIGH_ALPHA = 1\n", encoding="utf-8"
                )
                real_run(
                    ["git", "add", "src/alpha.py"],
                    cwd=self.repository_root,
                    check=True,
                )
                real_run(
                    ["git", "commit", "-q", "-m", "sol terminal repair"],
                    cwd=self.repository_root,
                    env={
                        **os.environ,
                        "GIT_AUTHOR_NAME": "Acceptance Contract Tests",
                        "GIT_AUTHOR_EMAIL": "acceptance-tests@example.invalid",
                        "GIT_COMMITTER_NAME": "Acceptance Contract Tests",
                        "GIT_COMMITTER_EMAIL": "acceptance-tests@example.invalid",
                    },
                    check=True,
                )
                output_path = Path(command[command.index("-o") + 1])
                output_path.write_text(
                    json.dumps(
                        {
                            "schema_version": "ai-result-1",
                            "role": "sol_xhigh",
                            "status": "IMPLEMENTED_CANDIDATE",
                            "summary": "Completed the one terminal repair scope.",
                            "claims": [],
                            "evidence": [],
                            "counter_checks": [],
                            "changed_files": ["src/alpha.py"],
                            "blind_spots": [],
                            "unresolved_questions": [],
                            "recommended_next_state": "AWAITING_OWNER_DECISION",
                        }
                    ),
                    encoding="utf-8",
                )
                events = "\n".join(
                    (
                        json.dumps(
                            {"type": "thread.started", "thread_id": terminal_thread}
                        ),
                        json.dumps({"type": "turn.completed"}),
                    )
                )
                return subprocess.CompletedProcess(
                    command, 0, stdout=events + "\n", stderr=""
                )
            return real_run(command, *args, **kwargs)

        with mock.patch.object(
            workflow.subprocess, "run", side_effect=terminal_controller_process
        ):
            repairs.run_assignment(self.store, self.TASK_ID, terminal, sessions)
        events = self._assert_chain()
        self._assert_terminal(events)
        self.assertEqual(
            1,
            sum(event.get("phase") == "SOL_XHIGH_TERMINAL_REPAIR" for event in events),
        )
        self.assertFalse(
            any(
                event.get("event_type") == "REVIEW_COMPLETED"
                and event.get("phase") == "TASK_TERMINAL"
                for event in events
            )
        )
        self._assert_terminal_ladder_closed()

    # ------------------------------------------------------------------
    # Binding, replay, ownership, and fail-closed counterexamples
    # ------------------------------------------------------------------
    def test_task_candidate_and_cross_task_bindings_reject_stale_capabilities(self):
        _, _, _, first, reviewer_receipt = self._open_review_one()

        with self.assertRaises(workflow.WorkflowError):
            self._review(first, reviewer_receipt, "ACCEPT", self.findings)
        with self.assertRaises(workflow.WorkflowError):
            self._review(first, reviewer_receipt, "REWORK", tuple(reversed(self.findings)))

        mutated_task = dict(self.task)
        mutated_task["candidate_commit"] = "f" * 40
        workflow.atomic_write_json(
            self.state_root / self.TASK_ID / "task.json", mutated_task
        )
        with self.assertRaises(workflow.WorkflowError):
            self._review(first, reviewer_receipt, "ACCEPT")

        # A task-specific capability cannot be replayed against another task.
        second_task = dict(self.task)
        second_task["task_id"] = "AWF-20260809-902"
        self.store.create_task(second_task)
        with self.assertRaises(workflow.WorkflowError):
            self._v2()["record_adversarial_review"](
                self.store,
                second_task["task_id"],
                first,
                reviewer_receipt,
                "ACCEPT",
                (),
                self._evidence("cross-task"),
            )

    def test_finding_binding_and_actual_diff_scope_are_immutable(self):
        owner_actor, _, _, first, reviewer_receipt = self._open_review_one()
        self._review(first, reviewer_receipt, "REWORK", self.findings)
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        outside_candidate = self._commit_file("README.md", "scope escape\n")
        with self.assertRaises(workflow.WorkflowError):
            self._complete(owner_repair, owner_receipt, outside_candidate, ("src/alpha.py",))
        with self.assertRaises((workflow.WorkflowError, dataclasses.FrozenInstanceError)):
            owner_repair.findings = (self.findings[0],)  # type: ignore[misc]

    def test_actual_git_diff_is_authoritative_over_reported_changed_paths(self):
        owner_actor, _, _, first, reviewer_receipt = self._open_review_one()
        self._review(first, reviewer_receipt, "REWORK", self.findings)
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        output_candidate = self._commit_file("src/alpha.py", "OWNER_ALPHA = 1\n")
        with self.assertRaises(workflow.WorkflowError):
            self._complete(owner_repair, owner_receipt, output_candidate, ("src/beta.py",))

        failed_events = self._events()
        failed_attempt_results = [
            event
            for event in failed_events
            if event.get("assignment_id") == owner_repair.assignment_id
            and event.get("attempt_id") == owner_repair.attempt_id
            and event.get("event_type")
            in {"ASSIGNMENT_ATTEMPT_FAILED", "REPAIR_COMPLETED", "REVIEW_COMPLETED"}
        ]
        self.assertEqual(1, len(failed_attempt_results))
        self.assertEqual(
            "ASSIGNMENT_ATTEMPT_FAILED", failed_attempt_results[0]["event_type"]
        )

        _, retry_repair, retry_receipt = self._issue_with_receipt(
            "OWNER_REPAIR",
            "luna-owner-repair-retry",
            "luna",
            expected_actor=owner_actor,
        )
        self.assertNotEqual(owner_repair.attempt_id, retry_repair.attempt_id)
        self._complete(retry_repair, retry_receipt, output_candidate, ("src/alpha.py",))
        events = self._assert_chain()
        completion = [event for event in events if event["event_type"] == "REPAIR_COMPLETED"][-1]
        self.assertEqual(["src/alpha.py"], completion["actual_changed_paths"])
        self.assertNotEqual(completion["changed_paths"], ["src/beta.py"])

    def test_actual_git_diff_rejects_traversal_and_prefix_scope_escape(self):
        owner_actor, _, _, first, reviewer_receipt = self._open_review_one()
        self._review(first, reviewer_receipt, "REWORK", self.findings)
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        escaped_candidate = self._commit_file("src2/escape.py", "ESCAPE = 1\n")
        with self.assertRaises(workflow.WorkflowError):
            self._complete(owner_repair, owner_receipt, escaped_candidate, ("src2/escape.py",))
        with self.assertRaises(workflow.WorkflowError):
            self._complete(
                owner_repair,
                owner_receipt,
                escaped_candidate,
                ("src/../src2/escape.py",),
            )

    def test_actual_git_diff_rejects_symlink_scope_escape(self):
        escape_target = self.repository_root.parent / "symlink-target.txt"
        escape_target.write_text("outside\n", encoding="utf-8")
        owner_actor, _, _, first, reviewer_receipt = self._open_review_one()
        self._review(first, reviewer_receipt, "REWORK", self.findings)
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        symlink_candidate = self._commit_symlink("src/link.py", "../symlink-target.txt")
        with self.assertRaises(workflow.WorkflowError):
            self._complete(owner_repair, owner_receipt, symlink_candidate, ("src/link.py",))

    def _write_event_records(self, records: list[dict[str, object]]) -> None:
        path = self.state_root / self.TASK_ID / "events.jsonl"
        controller_records = [
            event
            for line in path.read_text(encoding="utf-8").splitlines()
            if (event := json.loads(line)).get("ledger_version") != "adversarial-acceptance-1"
        ]
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
                for record in [*controller_records, *records]
            ),
            encoding="utf-8",
        )

    def _rechain_event_records(self, records: list[dict[str, object]]) -> None:
        previous_id: str | None = None
        for index, record in enumerate(records):
            record["event_index"] = index
            record["previous_event_id"] = previous_id
            record.pop("event_id", None)
            encoded = json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            record["event_id"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            previous_id = record["event_id"]

    def _assert_replay_rejects_rechained_mutation(
        self,
        original: list[dict[str, object]],
        mutate: object,
    ) -> None:
        records = json.loads(json.dumps(original))
        mutate(records)
        self._rechain_event_records(records)
        self._write_event_records(records)
        with self.assertRaises(workflow.WorkflowError):
            self._v2()["replay_acceptance_ledger"](self.store, self.TASK_ID)
        self._write_event_records(json.loads(json.dumps(original)))
        self._v2()["replay_acceptance_ledger"](self.store, self.TASK_ID)

    def test_replay_rejects_rechained_identity_binding_and_evidence_mutations(self):
        _, _, _, first, reviewer_receipt = self._open_review_one()
        self._review(first, reviewer_receipt, "ACCEPT")
        original = self._events()
        self._assert_chain()

        def mutate_actor(records: list[dict[str, object]]) -> None:
            event = next(item for item in records if item["event_type"] == "REVIEW_COMPLETED")
            event["reviewer_receipt"]["execution_surface"] = "NATIVE_SUBAGENT"

        def mutate_runtime(records: list[dict[str, object]]) -> None:
            event = next(item for item in records if item["event_type"] == "REVIEW_COMPLETED")
            event["reviewer_receipt"]["runtime_instance_id"] = "runtime-forged"

        def mutate_receipt_identity(records: list[dict[str, object]]) -> None:
            event = next(item for item in records if item["event_type"] == "REVIEW_COMPLETED")
            event["reviewer_receipt"]["codex_thread_id"] = str(uuid.uuid4())

        def mutate_assignment(records: list[dict[str, object]]) -> None:
            event = next(item for item in records if item["event_type"] == "ASSIGNMENT_ISSUED")
            event["assignment_id"] = "0" * 64

        def mutate_capability(records: list[dict[str, object]]) -> None:
            event = next(item for item in records if item["event_type"] == "ASSIGNMENT_ISSUED")
            event["capability"]["allowed_paths"] = ["src/escape.py"]

        def mutate_finding(records: list[dict[str, object]]) -> None:
            event = next(item for item in records if item["event_type"] == "ASSIGNMENT_ISSUED")
            event["findings"] = [{"finding_id": "finding-new", "allowed_paths": ["src/new.py"]}]

        def mutate_candidate(records: list[dict[str, object]]) -> None:
            event = next(item for item in records if item["event_type"] == "ASSIGNMENT_ISSUED")
            event["candidate_commit"] = "f" * 40

        def mutate_evidence(records: list[dict[str, object]]) -> None:
            event = next(item for item in records if item["event_type"] == "REVIEW_COMPLETED")
            event["evidence"]["outputs"] = ["forged evidence output"]

        for mutation in (
            mutate_actor,
            mutate_runtime,
            mutate_receipt_identity,
            mutate_assignment,
            mutate_capability,
            mutate_finding,
            mutate_candidate,
            mutate_evidence,
        ):
            with self.subTest(mutation=mutation.__name__):
                self._assert_replay_rejects_rechained_mutation(original, mutation)

    def test_replay_rejects_missing_common_reordered_broken_and_duplicate_records(self):
        _, _, _, first, reviewer_receipt = self._open_review_one()
        self._review(first, reviewer_receipt, "ACCEPT")
        original = self._events()
        self._assert_chain()

        def missing_field(records: list[dict[str, object]]) -> None:
            records[0].pop("timestamp_utc")

        def reordered(records: list[dict[str, object]]) -> None:
            records[1], records[2] = records[2], records[1]

        def broken_chain(records: list[dict[str, object]]) -> None:
            records[-1]["previous_event_id"] = "1" * 64

        def duplicate_completion(records: list[dict[str, object]]) -> None:
            completion = next(
                item for item in records if item["event_type"] == "REVIEW_COMPLETED"
            )
            records.append(json.loads(json.dumps(completion)))

        for mutation in (missing_field, reordered, broken_chain, duplicate_completion):
            with self.subTest(mutation=mutation.__name__):
                records = json.loads(json.dumps(original))
                mutation(records)
                if mutation is not broken_chain:
                    self._rechain_event_records(records)
                self._write_event_records(records)
                with self.assertRaises(workflow.WorkflowError):
                    self._v2()["replay_acceptance_ledger"](self.store, self.TASK_ID)
                self._write_event_records(json.loads(json.dumps(original)))
                self._v2()["replay_acceptance_ledger"](self.store, self.TASK_ID)

    def test_replay_rejects_duplicate_repair_completion(self):
        owner_actor, _, _, first, reviewer_receipt = self._open_review_one()
        self._review(first, reviewer_receipt, "REWORK", self.findings)
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        owner_candidate = self._commit_file("src/alpha.py", "OWNER_ALPHA = 1\n")
        self._complete(owner_repair, owner_receipt, owner_candidate, ("src/alpha.py",))
        original = self._events()
        duplicate = json.loads(json.dumps(original))
        completion = next(
            item for item in duplicate if item["event_type"] == "REPAIR_COMPLETED"
        )
        duplicate.append(json.loads(json.dumps(completion)))
        self._rechain_event_records(duplicate)
        self._write_event_records(duplicate)
        with self.assertRaises(workflow.WorkflowError):
            self._v2()["replay_acceptance_ledger"](self.store, self.TASK_ID)

    def test_capability_and_evidence_tamper_fail_completion_review_and_replay(self):
        owner_actor, _, _, first, reviewer_receipt = self._open_review_one()
        issued_records = self._events()
        tampered_issue = json.loads(json.dumps(issued_records))
        issue_event = next(
            item for item in tampered_issue if item["event_type"] == "ASSIGNMENT_ISSUED"
        )
        issue_event["capability"]["forbidden_actions"] = ["merge"]
        self._rechain_event_records(tampered_issue)
        self._write_event_records(tampered_issue)
        with self.assertRaises(workflow.WorkflowError):
            self._review(first, reviewer_receipt, "REWORK", self.findings)
        self._write_event_records(issued_records)

        self._review(first, reviewer_receipt, "REWORK", self.findings)
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        owner_records = self._events()
        tampered_owner = json.loads(json.dumps(owner_records))
        owner_issue = [
            item for item in tampered_owner if item["event_type"] == "ASSIGNMENT_ISSUED"
        ][-1]
        owner_issue["capability"]["input_candidate_commit"] = "f" * 40
        self._rechain_event_records(tampered_owner)
        self._write_event_records(tampered_owner)
        output_candidate = self._commit_file("src/alpha.py", "OWNER_ALPHA = 1\n")
        with self.assertRaises(workflow.WorkflowError):
            self._complete(owner_repair, owner_receipt, output_candidate, ("src/alpha.py",))
        self._write_event_records(owner_records)

        # A semantically forged evidence payload cannot be made valid by merely
        # recomputing the enclosing event hash.
        valid_completion_records = self._events()
        forged_evidence = json.loads(json.dumps(valid_completion_records))
        review_event = next(
            item for item in forged_evidence if item["event_type"] == "REVIEW_COMPLETED"
        )
        review_event["evidence"]["outputs"] = ["forged after review"]
        self._rechain_event_records(forged_evidence)
        self._write_event_records(forged_evidence)
        with self.assertRaises(workflow.WorkflowError):
            self._v2()["replay_acceptance_ledger"](self.store, self.TASK_ID)
        with self.assertRaises(workflow.WorkflowError):
            self._review(first, reviewer_receipt, "REWORK", self.findings)

    def test_canonical_replay_rejects_one_mutated_ledger_identity(self):
        _, _, _, first, reviewer_receipt = self._open_review_one()
        self._review(first, reviewer_receipt, "ACCEPT")
        self._assert_chain()
        replay = self._v2()["replay_acceptance_ledger"](self.store, self.TASK_ID)
        self.assertTrue(replay)

        path = self.state_root / self.TASK_ID / "events.jsonl"
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        records[-1]["event_id"] = "0" * 64
        path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        with self.assertRaises(workflow.WorkflowError):
            self._v2()["replay_acceptance_ledger"](self.store, self.TASK_ID)

    def test_v2_terminal_ledger_claims_task_from_generic_runner(self):
        _, _, _, first, reviewer_receipt = self._open_review_one()
        self._review(first, reviewer_receipt, "ACCEPT")
        self._assert_terminal(self._assert_chain())
        self.assertTrue(self._v2()["repair_ledger_claims_task"](self.store, self.TASK_ID))

        class UnexpectedGenericRunner:
            is_live_model = False

            def __init__(self) -> None:
                self.calls: list[str] = []

            def run(self, role: str, task: object) -> dict[str, object]:
                self.calls.append(role)
                return {}

        runner = UnexpectedGenericRunner()
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            try:
                workflow.run_until_gate(
                    self.TASK_ID,
                    runner=runner,
                    allow_live_model=False,
                    state_root=self.state_root,
                )
            except workflow.WorkflowError as raised:
                self.assertEqual("REPAIR_ADAPTER_REQUIRED", raised.code)
            else:
                self.fail("generic runner must stop with REPAIR_ADAPTER_REQUIRED")
        self.assertEqual([], runner.calls)

    def test_identity_reuse_self_review_and_skipped_transition_fail_closed(self):
        owner_actor, _, reviewer_one_actor, first, reviewer_one_receipt = self._open_review_one()
        with self.assertRaises(workflow.WorkflowError):
            self._issue("REVIEW_1", owner_actor)
        with self.assertRaises(workflow.WorkflowError):
            self._issue("REVIEW_1", self._expected_actor("sol-xhigh", "sol_xhigh"))
        self._review(first, reviewer_one_receipt, "REWORK", self.findings)
        with self.assertRaises(workflow.WorkflowError):
            self._issue(
                "SOL_MEDIUM_REPAIR",
                self._expected_actor("sol-fixer", "sol_medium_reviewer"),
            )
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        owner_candidate = self._commit_file("src/alpha.py", "OWNER_ALPHA = 1\n")
        self._complete(owner_repair, owner_receipt, owner_candidate, ("src/alpha.py",))
        with self.assertRaises(workflow.WorkflowError):
            self._issue(
                "SOL_MEDIUM_REPAIR",
                self._expected_actor("sol-fixer-again", "sol_medium_reviewer"),
            )
        reviewer_two_actor, second, reviewer_two_receipt = self._issue_with_receipt(
            "REVIEW_2", "terra-review-two", "terra_xhigh_reviewer"
        )
        self.assertNotEqual(reviewer_one_actor, reviewer_two_actor)
        same_runtime_other_surface = dataclasses.replace(
            reviewer_two_receipt,
            execution_surface="NATIVE_SUBAGENT",
        )
        with self.assertRaises(workflow.WorkflowError):
            self._review(second, same_runtime_other_surface, "ACCEPT")
        same_surface_other_runtime = dataclasses.replace(
            reviewer_two_receipt,
            runtime_instance_id="runtime-terra-review-two-other",
        )
        with self.assertRaises(workflow.WorkflowError):
            self._review(second, same_surface_other_runtime, "ACCEPT")
        retry_receipt = self._receipt_for(
            second,
            "terra-review-two-retry",
            "terra_xhigh_reviewer",
            runtime_instance_id=reviewer_two_receipt.runtime_instance_id,
            attempt_number=2,
        )
        with self.assertRaises(workflow.WorkflowError):
            self._review(second, retry_receipt, "ACCEPT")
        forged_identity_source = dataclasses.replace(
            reviewer_two_receipt,
            codex_thread_id=str(uuid.uuid4()),
        )
        identity_fields = (
            "execution_surface",
            "runtime_instance_id",
            "native_agent_uuid",
            "codex_thread_id",
        )
        for changed_field, forged_receipt in (
            ("execution_surface", same_runtime_other_surface),
            ("runtime_instance_id", same_surface_other_runtime),
            ("codex_thread_id", forged_identity_source),
        ):
            with self.subTest(identity_dimension=changed_field):
                self.assertEqual(
                    reviewer_two_receipt.assignment_id,
                    forged_receipt.assignment_id,
                )
                self.assertEqual(
                    reviewer_two_receipt.attempt_id,
                    forged_receipt.attempt_id,
                )
                self.assertEqual(
                    {changed_field},
                    {
                        field
                        for field in identity_fields
                        if getattr(reviewer_two_receipt, field)
                        != getattr(forged_receipt, field)
                    },
                )
        with self.assertRaises(workflow.WorkflowError):
            self._review(second, forged_identity_source, "ACCEPT")
        wrong_assignment = self._receipt(
            "terra-review-two-wrong-assignment",
            "terra_xhigh_reviewer",
            hashlib.sha256(b"different-assignment").hexdigest(),
            runtime_instance_id=reviewer_two_receipt.runtime_instance_id,
            execution_surface=reviewer_two_receipt.execution_surface,
        )
        with self.assertRaises(workflow.WorkflowError):
            self._review(second, wrong_assignment, "ACCEPT")
        self._review(second, reviewer_two_receipt, "ACCEPT")

    def test_sol_fixer_cannot_be_its_own_peer_and_v1_history_is_not_v2(self):
        owner_actor, _, _, first, reviewer_one_receipt = self._open_review_one()
        self._review(first, reviewer_one_receipt, "REWORK", self.findings)
        _, owner_repair, owner_receipt = self._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        owner_candidate = self._commit_file("src/alpha.py", "OWNER_ALPHA = 1\n")
        self._complete(owner_repair, owner_receipt, owner_candidate, ("src/alpha.py",))
        _, second, reviewer_two_receipt = self._issue_with_receipt(
            "REVIEW_2", "terra-review-two", "terra_xhigh_reviewer"
        )
        self._review(second, reviewer_two_receipt, "REWORK", self.findings)
        sol_actor, sol_repair, sol_receipt = self._issue_with_receipt(
            "SOL_MEDIUM_REPAIR", "sol-fixer", "sol_medium_reviewer"
        )
        sol_candidate = self._commit_file("src/beta.py", "SOL_BETA = 1\n")
        self._complete(sol_repair, sol_receipt, sol_candidate, ("src/beta.py",))
        with self.assertRaises(workflow.WorkflowError):
            self._issue("SOL_MEDIUM_PEER_REVIEW", sol_actor)

        # The legacy v1 event is not an adversarial-acceptance-1 ledger.
        legacy_task_id = "AWF-20260809-903"
        legacy_task = dict(self.task)
        legacy_task["task_id"] = legacy_task_id
        self.store.create_task(legacy_task)
        # A historical v1 record remains readable but cannot create a new
        # assignment or claim this task from the v2 generic-runner guard.
        self.store.append_event(
            legacy_task_id,
            {"event_type": "REPAIR_ASSIGNED", "repair_round": 1},
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_PROTOCOL_V1_DISABLED"):
            repairs.assign_repair(
                self.findings,
                1,
                repairs.ActorIdentity("sol-medium", "sol_medium_reviewer"),
                None,
            )
        self.assertFalse(
            self._v2()["repair_ledger_claims_task"](self.store, legacy_task_id),
            "repair-ledger-1 history must not be reported as v2 acceptance ownership",
        )


if __name__ == "__main__":
    unittest.main()
