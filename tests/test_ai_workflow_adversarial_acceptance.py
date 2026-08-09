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
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_repairs as repairs


class AcceptanceLedgerV2ContractTest(unittest.TestCase):
    """Contract suite for the approved ``adversarial-acceptance-1`` ledger."""

    TASK_ID = "AWF-20260809-901"
    BASE_COMMIT = "a" * 40
    INITIAL_CANDIDATE = "b" * 40
    OWNER_CANDIDATE = "c" * 40
    SOL_CANDIDATE = "d" * 40
    TERMINAL_CANDIDATE = "e" * 40

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        temporary_root = Path(self.temporary_directory.name)
        self.repository_root = temporary_root / "repository"
        self.repository_root.mkdir()
        self.state_root = temporary_root / "state"
        self.store = workflow.WorkflowStore(self.state_root)
        self.task = {
            "schema_version": "ai-task-1",
            "task_id": self.TASK_ID,
            "task_type": "REMEDIATION",
            "objective": "Repair the bounded candidate under adversarial acceptance",
            "repository_root": str(self.repository_root),
            "source_worktree": None,
            "base_commit": self.BASE_COMMIT,
            "candidate_commit": self.INITIAL_CANDIDATE,
            "authoritative_files": ["README.md"],
            "allowed_write_paths": ["src/"],
            "forbidden_actions": ["merge", "push"],
            "risk_flags": [],
            "acceptance_commands": [
                "python3.11 -m unittest tests.test_ai_workflow_adversarial_acceptance -v"
            ],
            "verification_level": "L2",
            "human_gates": ["PLAN_APPROVAL", "EXECUTION_APPROVAL"],
        }
        self.findings = (
            repairs.RepairFinding("finding-001", ("src/alpha.py",)),
            repairs.RepairFinding("finding-002", ("src/beta.py",)),
        )

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
        *,
        runtime_instance_id: str | None = None,
        attempt_number: int = 1,
        execution_surface: str = "codex-subagent",
    ) -> object:
        api = self._v2()
        runtime = runtime_instance_id or f"runtime-{label}"
        attempt_id = f"{label}-attempt-{attempt_number}"
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
        evidence_hash = hashlib.sha256(
            f"{label}:{runtime}:{attempt_id}".encode("utf-8")
        ).hexdigest()
        return api["VerifiedActorReceipt"](
            execution_surface=execution_surface,
            runtime_instance_id=runtime,
            attempt_id=attempt_id,
            requested_role=role,
            observed_model=model,
            observed_reasoning_effort=effort,
            observed_sandbox_policy=sandbox,
            observed_permission_profile=permission,
            observed_cwd=str(self.repository_root),
            runtime_evidence_sha256=evidence_hash,
        )

    def _evidence(self, label: str = "review") -> object:
        api = self._v2()
        return api["AdversarialEvidence"](
            verification_commands=(
                "python3.11 -m unittest tests.test_ai_workflow_adversarial_acceptance -v",
            ),
            negative_checks=(
                f"mutation-{label}: reject an out-of-scope path and stale candidate",
            ),
            outputs=(f"{label}: verification and negative mutation were observed",),
        )

    def _create_task(self, task: dict[str, object] | None = None) -> None:
        self.store.create_task(dict(task or self.task))

    def _open(self, owner: object) -> object:
        api = self._v2()
        self._create_task()
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
        return assignment

    def _complete(
        self,
        assignment: object,
        actor: object,
        output_candidate: str,
        changed_paths: tuple[str, ...] = ("src/alpha.py",),
    ) -> object:
        api = self._v2()
        return api["complete_acceptance_assignment"](
            self.store,
            self.TASK_ID,
            assignment,
            actor,
            output_candidate,
            changed_paths,
        )

    def _review(
        self,
        assignment: object,
        reviewer: object,
        verdict: str,
        findings: tuple[object, ...] = (),
        evidence: object | None = None,
    ) -> object:
        api = self._v2()
        return api["record_adversarial_review"](
            self.store,
            self.TASK_ID,
            assignment,
            reviewer,
            verdict,
            findings,
            evidence or self._evidence(verdict.lower()),
        )

    def _events(self) -> list[dict[str, object]]:
        path = self.state_root / self.TASK_ID / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

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
            self.assertEqual(self.BASE_COMMIT, event["base_commit"])
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

    def _assert_assignment_binding(
        self,
        assignment: object,
        phase: str,
        expected_actor: object,
        candidate: str = INITIAL_CANDIDATE,
    ) -> None:
        self.assertEqual(phase, assignment.phase)
        self.assertEqual(self.TASK_ID, assignment.task_id)
        self.assertEqual(expected_actor, assignment.expected_actor)
        self.assertEqual(self.BASE_COMMIT, assignment.base_commit)
        self.assertEqual(candidate, assignment.input_candidate_commit)
        self.assertEqual(self.TASK_ID, assignment.capability.task_id)
        self.assertEqual(assignment.attempt_id, assignment.capability.attempt_id)
        self.assertTrue(assignment.capability.capability_id)

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

        receipt = self._receipt("frozen", "luna")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            receipt.runtime_instance_id = "mutated"  # type: ignore[misc]

    def test_luna_owner_review_one_accepts_to_terminal(self):
        owner = self._receipt("luna-owner", "luna")
        reviewer_one = self._receipt("terra-review-one", "terra_xhigh_reviewer")
        self._open(owner)
        first = self._issue("REVIEW_1", reviewer_one)
        self._assert_assignment_binding(first, "REVIEW_1", reviewer_one)
        self.assertEqual((), tuple(first.findings))

        self._review(first, reviewer_one, "ACCEPT")
        events = self._assert_chain()
        self._assert_terminal(events)
        self.assertTrue(repairs.repair_ledger_claims_task(self.store, self.TASK_ID))
        with self.assertRaises(workflow.WorkflowError):
            self._issue("OWNER_REPAIR", owner)

    def test_luna_owner_rework_then_distinct_terra_review_two_accepts(self):
        owner = self._receipt("luna-owner", "luna")
        reviewer_one = self._receipt("terra-review-one", "terra_xhigh_reviewer")
        reviewer_two = self._receipt("terra-review-two", "terra_xhigh_reviewer")
        self._open(owner)
        first = self._issue("REVIEW_1", reviewer_one)
        self._review(first, reviewer_one, "REWORK", self.findings)

        owner_repair = self._issue("OWNER_REPAIR", owner)
        self._assert_assignment_binding(owner_repair, "OWNER_REPAIR", owner)
        self.assertEqual(self.findings, tuple(owner_repair.findings))
        self._complete(owner_repair, owner, self.OWNER_CANDIDATE)

        second = self._issue("REVIEW_2", reviewer_two)
        self._assert_assignment_binding(second, "REVIEW_2", reviewer_two, self.OWNER_CANDIDATE)
        self.assertNotEqual(
            (reviewer_one.execution_surface, reviewer_one.runtime_instance_id),
            (reviewer_two.execution_surface, reviewer_two.runtime_instance_id),
        )
        self.assertNotEqual(
            (owner.execution_surface, owner.runtime_instance_id),
            (reviewer_two.execution_surface, reviewer_two.runtime_instance_id),
        )
        self._review(second, reviewer_two, "ACCEPT")
        self._assert_terminal(self._assert_chain())

    def test_terra_owner_rework_then_sol_peer_accepts(self):
        owner = self._receipt("terra-owner", "terra_xhigh")
        reviewer_one = self._receipt("terra-review-one", "terra_xhigh_reviewer")
        reviewer_two = self._receipt("terra-review-two", "terra_xhigh_reviewer")
        sol_fixer = self._receipt("sol-fixer", "sol_medium_reviewer")
        sol_peer = self._receipt("sol-peer", "sol_medium_reviewer")
        self._open(owner)
        first = self._issue("REVIEW_1", reviewer_one)
        self._review(first, reviewer_one, "REWORK", self.findings)
        owner_repair = self._issue("OWNER_REPAIR", owner)
        self._complete(owner_repair, owner, self.OWNER_CANDIDATE)
        second = self._issue("REVIEW_2", reviewer_two)
        self._review(second, reviewer_two, "REWORK", self.findings)

        sol_repair = self._issue("SOL_MEDIUM_REPAIR", sol_fixer)
        self._assert_assignment_binding(
            sol_repair, "SOL_MEDIUM_REPAIR", sol_fixer, self.OWNER_CANDIDATE
        )
        self.assertIn("assignment", sol_repair.capability.write_authority)
        self.assertEqual(self.findings, tuple(sol_repair.findings))
        self._complete(sol_repair, sol_fixer, self.SOL_CANDIDATE, ("src/beta.py",))
        peer_review = self._issue("SOL_MEDIUM_PEER_REVIEW", sol_peer)
        self._assert_assignment_binding(
            peer_review, "SOL_MEDIUM_PEER_REVIEW", sol_peer, self.SOL_CANDIDATE
        )
        self.assertNotEqual(
            (sol_fixer.execution_surface, sol_fixer.runtime_instance_id),
            (sol_peer.execution_surface, sol_peer.runtime_instance_id),
        )
        self._review(peer_review, sol_peer, "ACCEPT")
        self._assert_terminal(self._assert_chain())

    def test_sol_peer_rework_creates_one_sol_xhigh_terminal_repair(self):
        owner = self._receipt("luna-owner", "luna")
        reviewer_one = self._receipt("terra-review-one", "terra_xhigh_reviewer")
        reviewer_two = self._receipt("terra-review-two", "terra_xhigh_reviewer")
        sol_fixer = self._receipt("sol-fixer", "sol_medium_reviewer")
        sol_peer = self._receipt("sol-peer", "sol_medium_reviewer")
        sol_xhigh = self._receipt("sol-xhigh", "sol_xhigh")
        self._open(owner)
        first = self._issue("REVIEW_1", reviewer_one)
        self._review(first, reviewer_one, "REWORK", self.findings)
        owner_repair = self._issue("OWNER_REPAIR", owner)
        self._complete(owner_repair, owner, self.OWNER_CANDIDATE)
        second = self._issue("REVIEW_2", reviewer_two)
        self._review(second, reviewer_two, "REWORK", self.findings)
        sol_repair = self._issue("SOL_MEDIUM_REPAIR", sol_fixer)
        self._assert_assignment_binding(
            sol_repair, "SOL_MEDIUM_REPAIR", sol_fixer, self.OWNER_CANDIDATE
        )
        self._complete(sol_repair, sol_fixer, self.SOL_CANDIDATE)
        peer_review = self._issue("SOL_MEDIUM_PEER_REVIEW", sol_peer)
        self._assert_assignment_binding(
            peer_review, "SOL_MEDIUM_PEER_REVIEW", sol_peer, self.SOL_CANDIDATE
        )
        self._review(peer_review, sol_peer, "REWORK", self.findings)

        terminal = self._issue("SOL_XHIGH_TERMINAL_REPAIR", sol_xhigh)
        self._assert_assignment_binding(
            terminal,
            "SOL_XHIGH_TERMINAL_REPAIR",
            sol_xhigh,
            self.SOL_CANDIDATE,
        )
        self.assertEqual("assignment-scoped-write", terminal.capability.write_authority)
        self._complete(terminal, sol_xhigh, self.TERMINAL_CANDIDATE)
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
        with self.assertRaises(workflow.WorkflowError):
            self._issue("SOL_XHIGH_TERMINAL_REPAIR", sol_xhigh)

    # ------------------------------------------------------------------
    # Binding, replay, ownership, and fail-closed counterexamples
    # ------------------------------------------------------------------
    def test_task_candidate_and_cross_task_bindings_reject_stale_capabilities(self):
        owner = self._receipt("luna-owner", "luna")
        reviewer_one = self._receipt("terra-review-one", "terra_xhigh_reviewer")
        self._open(owner)
        first = self._issue("REVIEW_1", reviewer_one)

        with self.assertRaises(workflow.WorkflowError):
            self._review(first, reviewer_one, "ACCEPT", self.findings)
        with self.assertRaises(workflow.WorkflowError):
            self._review(first, reviewer_one, "REWORK", tuple(reversed(self.findings)))

        mutated_task = dict(self.task)
        mutated_task["candidate_commit"] = "f" * 40
        workflow.atomic_write_json(
            self.state_root / self.TASK_ID / "task.json", mutated_task
        )
        with self.assertRaises(workflow.WorkflowError):
            self._review(first, reviewer_one, "ACCEPT")

        # A task-specific capability cannot be replayed against another task.
        second_task = dict(self.task)
        second_task["task_id"] = "AWF-20260809-902"
        self.store.create_task(second_task)
        with self.assertRaises(workflow.WorkflowError):
            self._v2()["record_adversarial_review"](
                self.store,
                second_task["task_id"],
                first,
                reviewer_one,
                "ACCEPT",
                (),
                self._evidence("cross-task"),
            )

    def test_finding_binding_and_actual_diff_scope_are_immutable(self):
        owner = self._receipt("luna-owner", "luna")
        reviewer_one = self._receipt("terra-review-one", "terra_xhigh_reviewer")
        self._open(owner)
        first = self._issue("REVIEW_1", reviewer_one)
        self._review(first, reviewer_one, "REWORK", self.findings)
        owner_repair = self._issue("OWNER_REPAIR", owner)
        with self.assertRaises(workflow.WorkflowError):
            self._complete(owner_repair, owner, self.OWNER_CANDIDATE, ("outside.py",))
        with self.assertRaises((workflow.WorkflowError, dataclasses.FrozenInstanceError)):
            owner_repair.findings = (self.findings[0],)  # type: ignore[misc]

    def test_canonical_replay_rejects_one_mutated_ledger_identity(self):
        owner = self._receipt("luna-owner", "luna")
        reviewer_one = self._receipt("terra-review-one", "terra_xhigh_reviewer")
        self._open(owner)
        first = self._issue("REVIEW_1", reviewer_one)
        self._review(first, reviewer_one, "ACCEPT")
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
        owner = self._receipt("luna-owner", "luna")
        reviewer_one = self._receipt("terra-review-one", "terra_xhigh_reviewer")
        self._open(owner)
        first = self._issue("REVIEW_1", reviewer_one)
        self._review(first, reviewer_one, "ACCEPT")
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
        owner = self._receipt("luna-owner", "luna")
        reviewer_one = self._receipt("terra-review-one", "terra_xhigh_reviewer")
        self._open(owner)
        with self.assertRaises(workflow.WorkflowError):
            self._issue("REVIEW_1", owner)
        with self.assertRaises(workflow.WorkflowError):
            self._issue("REVIEW_1", self._receipt("sol-xhigh", "sol_xhigh"))
        first = self._issue("REVIEW_1", reviewer_one)
        self._review(first, reviewer_one, "REWORK", self.findings)
        with self.assertRaises(workflow.WorkflowError):
            self._issue("SOL_MEDIUM_REPAIR", self._receipt("sol-fixer", "sol_medium_reviewer"))
        owner_repair = self._issue("OWNER_REPAIR", owner)
        self._complete(owner_repair, owner, self.OWNER_CANDIDATE)
        with self.assertRaises(workflow.WorkflowError):
            self._issue("SOL_MEDIUM_REPAIR", self._receipt("sol-fixer", "sol_medium_reviewer"))
        reused_review = self._receipt(
            "terra-review-one-retry",
            "terra_xhigh_reviewer",
            runtime_instance_id=reviewer_one.runtime_instance_id,
            attempt_number=2,
        )
        with self.assertRaises(workflow.WorkflowError):
            self._issue("REVIEW_2", reused_review)

    def test_sol_fixer_cannot_be_its_own_peer_and_v1_history_is_not_v2(self):
        owner = self._receipt("luna-owner", "luna")
        reviewer_one = self._receipt("terra-review-one", "terra_xhigh_reviewer")
        reviewer_two = self._receipt("terra-review-two", "terra_xhigh_reviewer")
        sol_fixer = self._receipt("sol-fixer", "sol_medium_reviewer")
        self._open(owner)
        first = self._issue("REVIEW_1", reviewer_one)
        self._review(first, reviewer_one, "REWORK", self.findings)
        owner_repair = self._issue("OWNER_REPAIR", owner)
        self._complete(owner_repair, owner, self.OWNER_CANDIDATE)
        second = self._issue("REVIEW_2", reviewer_two)
        self._review(second, reviewer_two, "REWORK", self.findings)
        sol_repair = self._issue("SOL_MEDIUM_REPAIR", sol_fixer)
        self._complete(sol_repair, sol_fixer, self.SOL_CANDIDATE)
        with self.assertRaises(workflow.WorkflowError):
            self._issue("SOL_MEDIUM_PEER_REVIEW", sol_fixer)

        # The legacy v1 event is not an adversarial-acceptance-1 ledger.
        legacy_task_id = "AWF-20260809-903"
        legacy_task = dict(self.task)
        legacy_task["task_id"] = legacy_task_id
        self.store.create_task(legacy_task)
        legacy_reviewer = repairs.ActorIdentity("sol-medium", "sol_medium_reviewer")
        legacy_assignment = repairs.assign_repair(
            self.findings, 1, legacy_reviewer, None
        )
        repairs.record_repair_assignment(self.store, legacy_task_id, legacy_assignment)
        self.assertFalse(
            self._v2()["repair_ledger_claims_task"](self.store, legacy_task_id),
            "repair-ledger-1 history must not be reported as v2 acceptance ownership",
        )


if __name__ == "__main__":
    unittest.main()
