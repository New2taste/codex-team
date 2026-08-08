import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_repairs as repairs


class RepairProtocolTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.root)
        self.task_id = "AWF-20260808-002"
        self.store.create_task(
            {
                "schema_version": "ai-task-1",
                "task_id": self.task_id,
                "task_type": "REMEDIATION",
                "objective": "Repair the frozen findings",
                "repository_root": str(Path(self.temporary_directory.name)),
                "source_worktree": None,
                "base_commit": "a" * 40,
                "candidate_commit": "b" * 40,
                "authoritative_files": ["README.md"],
                "allowed_write_paths": ["scripts/"],
                "forbidden_actions": ["merge", "push"],
                "risk_flags": [],
                "acceptance_commands": ["python -m unittest"],
                "verification_level": "L1",
                "human_gates": ["PLAN_APPROVAL", "EXECUTION_APPROVAL"],
            }
        )
        self.original_reviewer = repairs.ActorIdentity(
            identity="sol-reviewer-original", role="sol_medium_reviewer"
        )
        self.peer_reviewer = repairs.ActorIdentity(
            identity="sol-reviewer-peer", role="sol_medium_reviewer"
        )
        self.findings = (
            repairs.RepairFinding("finding-1", ("scripts/ai_workflow.py",)),
            repairs.RepairFinding("finding-2", ("scripts/ai_workflow_repairs.py",)),
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def assignment(self, round_number):
        return repairs.assign_repair(
            self.findings,
            round_number,
            self.original_reviewer,
            self.peer_reviewer if round_number == 3 else None,
        )

    def complete_and_reject(self, assignment):
        repairs.record_repair_completion(
            self.store,
            self.task_id,
            assignment,
            assignment.fixer_identity,
            [path for finding in assignment.findings for path in finding.allowed_paths],
        )
        repairs.record_repair_review(
            self.store,
            self.task_id,
            assignment,
            assignment.reviewer_identity,
            "REWORK_RECOMMENDED",
        )

    def test_assignments_are_immutable_and_cap_terra_at_two_rounds(self):
        findings = list(self.findings)
        first = repairs.assign_repair(findings, 1, self.original_reviewer, None)
        findings.clear()
        second = self.assignment(2)
        third = self.assignment(3)

        self.assertEqual("terra_xhigh", first.fixer_identity.role)
        self.assertEqual("terra_xhigh", second.fixer_identity.role)
        self.assertEqual(self.original_reviewer, first.reviewer_identity)
        self.assertIsNone(first.peer_reviewer_identity)
        self.assertEqual(self.original_reviewer, third.fixer_identity)
        self.assertEqual(self.peer_reviewer, third.reviewer_identity)
        self.assertEqual(self.peer_reviewer, third.peer_reviewer_identity)
        self.assertEqual(self.findings, first.findings)
        self.assertRegex(first.assignment_id, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first.assignment_id, second.assignment_id)
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_BUDGET_EXHAUSTED"):
            self.assignment(4)

    def test_direct_assignment_construction_retains_no_mutable_finding_input(self):
        assigned = self.assignment(1)
        mutable_findings = list(assigned.findings)
        reconstructed = repairs.RepairAssignment(
            assigned.assignment_id,
            assigned.repair_round,
            assigned.fixer_identity,
            assigned.reviewer_identity,
            assigned.peer_reviewer_identity,
            mutable_findings,
        )
        mutable_findings.clear()

        self.assertIsInstance(reconstructed.findings, tuple)
        self.assertEqual(assigned.findings, reconstructed.findings)

    def test_persisted_rounds_require_completed_rejections_and_replay_after_restart(self):
        first = self.assignment(1)
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_SEQUENCE_INVALID"):
            repairs.record_repair_assignment(self.store, self.task_id, self.assignment(2))

        repairs.record_repair_assignment(self.store, self.task_id, first)
        self.complete_and_reject(first)

        restarted = workflow.WorkflowStore(self.root)
        second = self.assignment(2)
        repairs.record_repair_assignment(restarted, self.task_id, second)
        self.complete_and_reject(second)
        with self.assertRaisesRegex(workflow.WorkflowError, "SOL_REPAIR_NOT_AUTHORIZED"):
            repairs.record_repair_assignment(restarted, self.task_id, self.assignment(3))

        repairs.record_sol_repair_authorization(restarted, self.task_id, self.original_reviewer)
        third = self.assignment(3)
        repairs.record_repair_assignment(restarted, self.task_id, third)
        self.complete_and_reject(third)

        events = [
            json.loads(line)
            for line in (self.root / self.task_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [
                "REPAIR_ASSIGNED",
                "REPAIR_COMPLETED",
                "REPAIR_REVIEWED",
                "REPAIR_ASSIGNED",
                "REPAIR_COMPLETED",
                "REPAIR_REVIEWED",
                "SOL_REPAIR_AUTHORIZED",
                "REPAIR_ASSIGNED",
                "REPAIR_COMPLETED",
                "REPAIR_REVIEWED",
                "REPAIR_BUDGET_EXHAUSTED",
            ],
            [event["event_type"] for event in events],
        )
        self.assertEqual("BLOCKED", events[-1]["new_state"])
        self.assertEqual(self.task_id, events[0].get("task_id", self.task_id))
        self.assertEqual("a" * 40, events[0]["base_commit"])
        self.assertEqual("b" * 40, events[0]["candidate_commit"])
        self.assertRegex(events[0]["task_sha256"], r"^[0-9a-f]{64}$")

    def test_protocol_rejects_actor_scope_finding_commit_and_replay_drift(self):
        first = self.assignment(1)
        repairs.record_repair_assignment(self.store, self.task_id, first)
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_REPLAY"):
            repairs.record_repair_assignment(self.store, self.task_id, first)
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_ACTOR_MISMATCH"):
            repairs.validate_repair_result(
                first,
                repairs.ActorIdentity("not-terra", "terra_xhigh"),
                ["scripts/ai_workflow.py"],
            )
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_SCOPE_VIOLATION"):
            repairs.validate_repair_result(
                first, first.fixer_identity, ["outside.py"]
            )
        self.complete_and_reject(first)
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_FINDING_DRIFT"):
            repairs.record_repair_assignment(
                self.store,
                self.task_id,
                repairs.assign_repair(
                    self.findings + (repairs.RepairFinding("finding-3", ("scripts/new.py",)),),
                    2,
                    self.original_reviewer,
                    None,
                ),
            )
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_FINDING_DRIFT"):
            repairs.record_repair_assignment(
                self.store,
                self.task_id,
                repairs.assign_repair(
                    (
                        repairs.RepairFinding(
                            "finding-1", ("scripts/ai_workflow_repairs.py",)
                        ),
                        repairs.RepairFinding("finding-2", ("scripts/ai_workflow.py",)),
                    ),
                    2,
                    self.original_reviewer,
                    None,
                ),
            )
        drifted = self.assignment(2)
        task_path = self.root / self.task_id / "task.json"
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task["candidate_commit"] = "c" * 40
        workflow.atomic_write_json(task_path, task)
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_COMMIT_DRIFT"):
            repairs.record_repair_assignment(self.store, self.task_id, drifted)

    def test_repair_finding_scope_must_stay_within_the_parent_task_write_scope(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_SCOPE_VIOLATION"):
            repairs.record_repair_assignment(
                self.store,
                self.task_id,
                repairs.assign_repair(
                    (repairs.RepairFinding("finding-outside", ("outside.py",)),),
                    1,
                    self.original_reviewer,
                    None,
                ),
            )

    def test_closed_actor_set_and_peer_separation_fail_closed(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_ACTOR_MISMATCH"):
            repairs.assign_repair(
                self.findings,
                1,
                repairs.ActorIdentity("sol-high", "sol_high"),
                None,
            )
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_REVIEWER_CONFLICT"):
            repairs.assign_repair(
                self.findings,
                3,
                self.original_reviewer,
                self.original_reviewer,
            )

    def test_workflow_reexports_the_frozen_repair_protocol(self):
        self.assertIs(workflow.assign_repair, repairs.assign_repair)
        self.assertIs(workflow.record_repair_assignment, repairs.record_repair_assignment)
        self.assertIs(workflow.validate_repair_result, repairs.validate_repair_result)

    def test_legacy_remediation_without_repair_events_never_requires_repair_commits(self):
        legacy_task_id = "AWF-20260808-003"
        task = json.loads((self.root / self.task_id / "task.json").read_text(encoding="utf-8"))
        task.update(
            {
                "task_id": legacy_task_id,
                "base_commit": None,
                "candidate_commit": None,
            }
        )
        self.store.create_task(task)

        self.assertFalse(repairs.has_active_repair_assignment(self.store, legacy_task_id))

    def test_repair_protocol_owns_execution_until_an_acceptance_verdict_closes_it(self):
        first = self.assignment(1)
        repairs.record_repair_assignment(self.store, self.task_id, first)
        self.assertTrue(repairs.has_active_repair_assignment(self.store, self.task_id))
        repairs.record_repair_completion(
            self.store,
            self.task_id,
            first,
            first.fixer_identity,
            [path for finding in first.findings for path in finding.allowed_paths],
        )
        self.assertTrue(repairs.has_active_repair_assignment(self.store, self.task_id))
        repairs.record_repair_review(
            self.store,
            self.task_id,
            first,
            first.reviewer_identity,
            "REWORK_RECOMMENDED",
        )
        self.assertTrue(repairs.has_active_repair_assignment(self.store, self.task_id))

    def test_active_repair_fails_closed_without_identity_aware_execution_handoff(self):
        first = self.assignment(1)
        repairs.record_repair_assignment(self.store, self.task_id, first)
        self.store.append_event(
            self.task_id,
            {
                "event_type": "STATE_TRANSITION",
                "new_state": "IMPLEMENTATION_RUNNING",
                "task_sha256": workflow._task_sha256(self.store, self.task_id),
                "retry_budget": {
                    "technical_retries": 0,
                    "implementation_reworks": 0,
                    "cross_model_escalations": 0,
                },
            },
        )

        class UnexpectedRunner:
            is_live_model = False

            def __init__(self):
                self.calls = []

            def run(self, role, task):
                self.calls.append(role)
                return {
                    "schema_version": "ai-result-1",
                    "role": role,
                    "status": "BLOCKED",
                    "summary": "must not run",
                    "claims": [],
                    "evidence": [],
                    "counter_checks": [],
                    "changed_files": [],
                    "blind_spots": [],
                    "unresolved_questions": [],
                    "recommended_next_state": "BLOCKED",
                }

        runner = UnexpectedRunner()
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.root):
            state = workflow.run_until_gate(self.task_id, runner=runner, allow_live_model=False)

        self.assertEqual("BLOCKED", state)
        self.assertEqual([], runner.calls)
        events = (self.root / self.task_id / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event_type":"REPAIR_EXECUTION_INTEGRATION_BLOCKED"', events)


if __name__ == "__main__":
    unittest.main()
