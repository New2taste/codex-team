import json
import hashlib
import inspect
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_ownership as ownership
from scripts import ai_workflow_repairs as repairs
from tests.test_ai_workflow import _RecordingPopen, _compat_popen, _install_declaration


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
        repo = Path(self.temporary_directory.name)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
        (repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
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

    @unittest.skip("repair-ledger-1 assignment creation is terminally disabled")
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

    def test_v1_repair_mutation_api_is_disabled(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_PROTOCOL_V1_DISABLED"):
            self.assignment(1)

    @unittest.skip("repair-ledger-1 assignment creation is terminally disabled")
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

    @unittest.skip("repair-ledger-1 mutation is terminally disabled")
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

    @unittest.skip("repair-ledger-1 mutation is terminally disabled")
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

    @unittest.skip("repair-ledger-1 mutation is terminally disabled")
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

    @unittest.skip("repair-ledger-1 mutation is terminally disabled")
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
        self.assertIs(workflow.open_task_acceptance, repairs.open_task_acceptance)
        self.assertIs(workflow.run_assignment, repairs.run_assignment)

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

    def test_v2_absence_preserves_plan_acceptance_and_legacy_generic_routing(self):
        tasks: list[tuple[str, str]] = []
        for task_id, task_type in (
            ("AWF-20260809-910", "PLAN"),
            ("AWF-20260809-911", "ACCEPTANCE"),
            ("AWF-20260809-912", "REMEDIATION"),
        ):
            task = json.loads((self.root / self.task_id / "task.json").read_text(encoding="utf-8"))
            task.update({"task_id": task_id, "task_type": task_type, "allowed_write_paths": []})
            if task_type in {"PLAN", "REMEDIATION"}:
                task.update({"base_commit": None, "candidate_commit": None})
            if task_type == "REMEDIATION":
                task["allowed_write_paths"] = ["scripts/"]
                task["source_worktree"] = task["repository_root"]
            else:
                task["human_gates"] = ["FINAL_ACCEPTANCE"]
            self.store.create_task(task)
            tasks.append((task_id, "AWAITING_OWNER_DECISION"))

        legacy_config = workflow._load_workflow_config()
        legacy_config["routing"] = {"mode": "legacy", "role_policy": "legacy"}
        with mock.patch.object(workflow, "_load_workflow_config", return_value=legacy_config):
            for task_id, expected_state in tasks:
                with self.subTest(task_id=task_id):
                    self.assertFalse(repairs.repair_ledger_claims_task(self.store, task_id))
                    self.assertEqual(
                        expected_state,
                        workflow.run_until_gate(
                            task_id,
                            runner=workflow.FakeRunner(),
                            allow_live_model=False,
                            state_root=self.root,
                        ),
                    )
        self.store.append_event(
            "AWF-20260809-910",
            {"ledger_version": "adversarial-acceptance-1", "event_type": "forged"},
        )
        with self.assertRaises(workflow.WorkflowError):
            repairs.repair_ledger_claims_task(self.store, "AWF-20260809-910")

    @unittest.skip("repair-ledger-1 mutation is terminally disabled")
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

    @unittest.skip("repair-ledger-1 mutation is terminally disabled")
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

    def test_v2_adapter_requires_controller_boundary_and_records_one_failure(self):
        task = json.loads((self.root / self.task_id / "task.json").read_text(encoding="utf-8"))
        repository = Path(self.temporary_directory.name) / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.email", "repair@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repository), "config", "user.name", "Repair Tests"], check=True)
        (repository / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", "base"], check=True)
        candidate = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        task.update(
            {
                "repository_root": str(repository),
                "base_commit": candidate,
                "candidate_commit": candidate,
            }
        )
        workflow.atomic_write_json(self.root / self.task_id / "task.json", task)
        owner_thread = str(uuid.uuid4())
        owner_evidence = {
            "schema_version": "runtime-evidence-1",
            "attempt_id": "owner-attempt-1",
            "requested_role": "luna",
            "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
            "observed_agent_type": None,
            "native_agent_id": None,
            "native_thread_id": None,
            "observed_model": "gpt-5.6-luna",
            "observed_reasoning_effort": "max",
            "observed_sandbox_policy": "workspace-write",
            "observed_permission_profile": "workspace-write",
            "observed_cwd": str(repository),
            "evidence_source": "LOCAL_ROLLOUT",
            "observed_at_utc": "2026-08-09T00:00:00+00:00",
            "verification_status": "VERIFIED",
            "failure_reasons": [],
        }
        owner = repairs.VerifiedActorReceipt(
            assignment_id=hashlib.sha256(f"open:{self.task_id}".encode("utf-8")).hexdigest(),
            execution_surface="CODEX_EXEC_ROLE_CONTRACT",
            runtime_instance_id=owner_thread,
            attempt_id="owner-attempt-1",
            requested_role="luna",
            observed_model="gpt-5.6-luna",
            observed_reasoning_effort="max",
            observed_sandbox_policy="workspace-write",
            observed_permission_profile="workspace-write",
            observed_cwd=str(repository),
            runtime_evidence_sha256=hashlib.sha256(
                json.dumps(owner_evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            native_agent_uuid=None,
            codex_thread_id=owner_thread,
        )
        workflow.write_runtime_evidence(self.store, self.task_id, owner_evidence)
        self.store.append_event(
            self.task_id,
            {
                "event_type": "RUNTIME_EVIDENCE_RECORDED",
                "attempt_id": owner.attempt_id,
                "requested_role": owner.requested_role,
                "execution_surface": owner.execution_surface,
                "thread_id": owner_thread,
            },
        )
        repairs.open_task_acceptance(self.store, task, owner)
        reviewer_thread = str(uuid.uuid4())
        reviewer = repairs.ActorIdentity(
            f"CODEX_EXEC_ROLE_CONTRACT:{reviewer_thread}", "terra_xhigh_reviewer"
        )
        assignment = repairs.issue_acceptance_assignment(
            self.store, self.task_id, "REVIEW_1", reviewer
        )
        receipt = repairs.VerifiedActorReceipt(
            assignment_id=assignment.assignment_id,
            execution_surface="CODEX_EXEC_ROLE_CONTRACT",
            runtime_instance_id=reviewer_thread,
            attempt_id=assignment.attempt_id,
            requested_role="terra_xhigh_reviewer",
            observed_model="gpt-5.6-terra",
            observed_reasoning_effort="xhigh",
            observed_sandbox_policy="read-only",
            observed_permission_profile="read-only",
            observed_cwd=str(repository),
            runtime_evidence_sha256="d" * 64,
            native_agent_uuid=None,
            codex_thread_id=reviewer_thread,
        )

        case = self

        class LegacyAdapter:
            def __init__(self):
                self.assignment_calls = 0
                self.generic_calls = 0

            def run_assignment(self, received_assignment, received_receipt):
                self.assignment_calls += 1
                case.assertIs(assignment, received_assignment)
                case.assertIs(receipt, received_receipt)
                return {"verdict": "ACCEPT"}

            def run(self, *_args):
                self.generic_calls += 1
                raise AssertionError("generic runner must never receive a v2 assignment")

        legacy_adapter = LegacyAdapter()
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_ADAPTER_REQUIRED"):
            repairs.run_assignment(
                self.store, self.task_id, assignment, receipt, legacy_adapter
            )
        self.assertEqual(0, legacy_adapter.assignment_calls)
        self.assertEqual(0, legacy_adapter.generic_calls)

        class ControllerBoundary(repairs.ControllerAssignmentBoundary):
            def __init__(self):
                self.attestation_calls = 0
                self.execution_calls = 0

            def attest_execution(self, capability):
                self.attestation_calls += 1
                case.assertEqual(assignment.capability, capability)
                return repairs.ControllerExecutionAttestation(
                    task_id=self_task_id,
                    task_sha256=capability.task_sha256,
                    assignment_id=assignment.assignment_id,
                    capability_id=capability.capability_id,
                    candidate_commit=assignment.input_candidate_commit,
                    actor_receipt=receipt,
                )

            def execute_capability(self, capability):
                self.execution_calls += 1
                case.assertEqual(assignment.capability, capability)
                return {"verdict": "ACCEPT"}

        self_task_id = self.task_id
        boundary = ControllerBoundary()
        with self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_ADAPTER_REQUIRED"):
            repairs.run_assignment(self.store, self.task_id, assignment, boundary)
        self.assertEqual(0, boundary.attestation_calls)
        self.assertEqual(0, boundary.execution_calls)
        events = [
            json.loads(line)
            for line in (self.root / self.task_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        failed = [event for event in events if event.get("event_type") == "ASSIGNMENT_ATTEMPT_FAILED"]
        self.assertEqual(0, len(failed))


class AssignmentSideEffectObservationTest(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_ai_workflow_adversarial_acceptance import (
            AcceptanceLedgerV2ContractTest,
        )

        self.fx = AcceptanceLedgerV2ContractTest()
        self.fx.setUp()
        self.fx.task["source_worktree"] = str(self.fx.repository_root)
        (self.fx.repository_root / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
        original_open = self.fx._open_with_owner

        def gated_open(*args, **kwargs):
            result = original_open(*args, **kwargs)
            _install_declaration(
                self.fx.store,
                self.fx.task,
                allowed_roles=(
                    "luna",
                    "terra",
                    "terra_xhigh_reviewer",
                    "sol_medium_reviewer",
                ),
                active_roles=("luna", "terra_xhigh_reviewer"),
                max_dispatches=32,
            )
            return result

        self.fx._open_with_owner = gated_open

    def tearDown(self) -> None:
        self.fx.tearDown()

    def _write_rollout(self, sessions: Path, thread_id: str, *, sandbox: str, model: str, effort: str, permission: str) -> None:
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"rollout-{thread_id}").write_text(
            json.dumps(
                {
                    "thread_id": thread_id,
                    "agent_type": None,
                    "model": model,
                    "reasoning_effort": effort,
                    "sandbox_policy": sandbox,
                    "permission_profile": permission,
                    "cwd": str(self.fx.repository_root),
                }
            ),
            encoding="utf-8",
        )

    def _patch_codex(self, handler):
        return mock.patch.object(repairs.subprocess, "Popen", _compat_popen(handler))

    def test_v2_controller_captures_before_and_after_snapshots(self) -> None:
        from scripts import ai_workflow_side_effects as side_effects

        self.fx._open_with_owner("luna-owner", "luna")
        reviewer_thread = str(uuid.uuid4())
        review = self.fx._issue(
            "REVIEW_1",
            self.fx._expected_actor(
                "terra-controller-review",
                "terra_xhigh_reviewer",
                runtime_instance_id=reviewer_thread,
            ),
        )
        sessions = self.fx.repository_root.parent / "observe-sessions"
        self._write_rollout(
            sessions,
            reviewer_thread,
            sandbox="read-only",
            model="gpt-5.6-terra",
            effort="xhigh",
            permission="read-only",
        )
        snapshots: list[object] = []
        real_capture = side_effects.capture_fs_snapshot

        def capturing(repo, *, exclusions):
            snapshot = real_capture(repo, exclusions=exclusions)
            snapshots.append(snapshot)
            return snapshot

        result = {
            "schema_version": "ai-result-1",
            "dispatch_id": None,
            "task_id": None,
            "step_id": None,
            "attempt": None,
            "role": "terra_xhigh_reviewer",
            "status": "ACCEPTANCE_RECOMMENDED",
            "summary": "ok",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "AWAITING_OWNER_DECISION",
        }

        def controller_process(command, *args, **kwargs):
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text(json.dumps(result), encoding="utf-8")
            events = "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": reviewer_thread}),
                    json.dumps({"type": "turn.completed"}),
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=events + "\n", stderr="")

        with (
            mock.patch.object(side_effects, "capture_fs_snapshot", side_effect=capturing),
            mock.patch.object(
                repairs, "capture_fs_snapshot", side_effect=capturing
            ),
            self._patch_codex(controller_process),
            self.fx._controller_codex_lookup(),
        ):
            repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)
        self.assertGreaterEqual(len(snapshots), 2)

    def test_assignment_timeout_records_unobserved(self) -> None:
        from scripts import ai_workflow_ownership as ownership

        self.fx._open_with_owner("luna-owner", "luna")
        reviewer_thread = str(uuid.uuid4())
        review = self.fx._issue(
            "REVIEW_1",
            self.fx._expected_actor(
                "terra-timeout-review",
                "terra_xhigh_reviewer",
                runtime_instance_id=reviewer_thread,
            ),
        )
        sessions = self.fx.repository_root.parent / "timeout-sessions"
        self._write_rollout(
            sessions,
            reviewer_thread,
            sandbox="read-only",
            model="gpt-5.6-terra",
            effort="xhigh",
            permission="read-only",
        )

        def boom(command, *args, **kwargs):
            raise subprocess.TimeoutExpired("codex", 30)

        with (
            self._patch_codex(boom),
            self.fx._controller_codex_lookup(),
            self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_ADAPTER_REQUIRED"),
        ):
            repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)
        self.assertTrue(
            ownership.has_ownership_locking_side_effect(self.fx.store, self.fx.TASK_ID)
        )
        kinds = {
            row["effect_kind"]
            for row in ownership.load_side_effects(self.fx.store, self.fx.TASK_ID)
        }
        self.assertIn("UNOBSERVED_ASSUMED_PRESENT", kinds)

    def test_nonzero_exit_after_spawn_still_observes_worktree_mutation(self) -> None:
        from scripts import ai_workflow_ownership as ownership

        self.fx._open_with_owner("luna-owner", "luna")
        reviewer_thread = str(uuid.uuid4())
        review = self.fx._issue(
            "REVIEW_1",
            self.fx._expected_actor(
                "terra-nonzero-review",
                "terra_xhigh_reviewer",
                runtime_instance_id=reviewer_thread,
            ),
        )
        sessions = self.fx.repository_root.parent / "nonzero-sessions"
        self._write_rollout(
            sessions,
            reviewer_thread,
            sandbox="read-only",
            model="gpt-5.6-terra",
            effort="xhigh",
            permission="read-only",
        )

        def fail_and_mutate(command, *args, **kwargs):
            (self.fx.repository_root / "src" / "spawn-failed.txt").write_text(
                "mutated after spawn\n", encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 23, stdout="", stderr="failed")

        with (
            self._patch_codex(fail_and_mutate),
            self.fx._controller_codex_lookup(),
            self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_ADAPTER_REQUIRED"),
        ):
            repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)
        rows = ownership.load_side_effects(self.fx.store, self.fx.TASK_ID)
        kinds = {row["effect_kind"] for row in rows}
        self.assertTrue(ownership.has_ownership_locking_side_effect(self.fx.store, self.fx.TASK_ID))
        self.assertTrue(kinds & {"OWNED_WRITE", "UNTRACKED_WRITE", "COMMAND_GENERATED"})
        self.assertIn("src/spawn-failed.txt", {row["path"] for row in rows})
        self.assertNotIn("UNOBSERVED_ASSUMED_PRESENT", kinds)

    def test_post_spawn_snapshot_failure_records_unobserved(self) -> None:
        from scripts import ai_workflow_ownership as ownership
        from scripts import ai_workflow_side_effects as side_effects

        self.fx._open_with_owner("luna-owner", "luna")
        reviewer_thread = str(uuid.uuid4())
        review = self.fx._issue(
            "REVIEW_1",
            self.fx._expected_actor(
                "terra-snapshot-fail-review",
                "terra_xhigh_reviewer",
                runtime_instance_id=reviewer_thread,
            ),
        )
        sessions = self.fx.repository_root.parent / "snapshot-fail-sessions"
        self._write_rollout(
            sessions,
            reviewer_thread,
            sandbox="read-only",
            model="gpt-5.6-terra",
            effort="xhigh",
            permission="read-only",
        )
        result = {
            "schema_version": "ai-result-1",
            "dispatch_id": None,
            "task_id": None,
            "step_id": None,
            "attempt": None,
            "role": "terra_xhigh_reviewer",
            "status": "ACCEPTANCE_RECOMMENDED",
            "summary": "ok",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "AWAITING_OWNER_DECISION",
        }
        snapshot_calls = {"n": 0}
        repo_calls = {"n": 0}
        real_snapshot = side_effects.capture_fs_snapshot
        real_capture_repo = workflow.capture_repo

        def fail_after_spawn(repo, *, exclusions):
            snapshot_calls["n"] += 1
            if snapshot_calls["n"] == 1:
                return real_snapshot(repo, exclusions=exclusions)
            raise RuntimeError("cannot snapshot the post-launch repository")

        def fail_post_launch_repo(*args, **kwargs):
            repo_calls["n"] += 1
            if repo_calls["n"] == 1:
                return real_capture_repo(*args, **kwargs)
            raise RuntimeError("cannot snapshot the post-launch repository")

        def controller_process(command, *args, **kwargs):
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text(json.dumps(result), encoding="utf-8")
            events = "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": reviewer_thread}),
                    json.dumps({"type": "turn.completed"}),
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=events + "\n", stderr="")

        with (
            mock.patch.object(side_effects, "capture_fs_snapshot", side_effect=fail_after_spawn),
            mock.patch.object(repairs, "capture_fs_snapshot", side_effect=fail_after_spawn),
            mock.patch.object(workflow, "capture_repo", side_effect=fail_post_launch_repo),
            self._patch_codex(controller_process),
            self.fx._controller_codex_lookup(),
            self.assertRaisesRegex(workflow.WorkflowError, "REPAIR_ADAPTER_REQUIRED"),
        ):
            repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)
        self.assertTrue(
            ownership.has_ownership_locking_side_effect(self.fx.store, self.fx.TASK_ID)
        )
        kinds = {
            row["effect_kind"]
            for row in ownership.load_side_effects(self.fx.store, self.fx.TASK_ID)
        }
        self.assertIn("UNOBSERVED_ASSUMED_PRESENT", kinds)

    def test_observed_changes_match_actual_changed_paths(self) -> None:
        from scripts import ai_workflow_side_effects as side_effects

        owner_actor, _ = self.fx._open_with_owner("luna-owner", "luna")
        _, review, reviewer_receipt = self.fx._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        self.fx._review(review, reviewer_receipt, "REWORK", (self.fx.findings[0],))
        owner_repair = self.fx._issue("OWNER_REPAIR", owner_actor)
        owner_thread = owner_actor.identity.split(":", 1)[1]
        sessions = self.fx.repository_root.parent / "owner-observe-sessions"
        self._write_rollout(
            sessions,
            owner_thread,
            sandbox="workspace-write",
            model="gpt-5.6-luna",
            effort="max",
            permission="workspace-write",
        )
        observed: list[tuple[object, ...]] = []
        real_observe = side_effects.observe_execution_side_effects

        def wrapping(*args, **kwargs):
            result = real_observe(*args, **kwargs)
            observed.append(result)
            return result

        real_run = subprocess.run

        def controller_process(command, *args, **kwargs):
            (self.fx.repository_root / "src" / "alpha.py").write_text(
                "CONTROLLER_REPAIR = True\n", encoding="utf-8"
            )
            real_run(["git", "add", "src/alpha.py"], cwd=self.fx.repository_root, check=True)
            real_run(
                ["git", "commit", "-q", "-m", "controller repair"],
                cwd=self.fx.repository_root,
                env={
                    **__import__("os").environ,
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
                        "dispatch_id": None,
                        "task_id": None,
                        "step_id": None,
                        "attempt": None,
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
                    json.dumps({"type": "thread.started", "thread_id": owner_thread}),
                    json.dumps({"type": "turn.completed"}),
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=events + "\n", stderr="")

        with (
            mock.patch.object(side_effects, "observe_execution_side_effects", side_effect=wrapping),
            mock.patch.object(repairs, "observe_execution_side_effects", side_effect=wrapping),
            self._patch_codex(controller_process),
            self.fx._controller_codex_lookup(),
        ):
            repairs.run_assignment(self.fx.store, self.fx.TASK_ID, owner_repair, sessions)
        self.assertTrue(observed)
        observed_paths = {change.path for change in observed[0]}
        events = [
            json.loads(line)
            for line in (
                self.fx.state_root / self.fx.TASK_ID / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        completed = next(
            event for event in events if event.get("event_type") == "REPAIR_COMPLETED"
        )
        actual = set(completed["actual_changed_paths"])
        self.assertEqual(actual, observed_paths)


class AssignmentDispatchGateTest(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_ai_workflow_adversarial_acceptance import (
            AcceptanceLedgerV2ContractTest,
        )

        self.fx = AcceptanceLedgerV2ContractTest()
        self.fx.setUp()
        self.fx.task["source_worktree"] = str(self.fx.repository_root)
        (self.fx.repository_root / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
        _RecordingPopen.reset()

    def tearDown(self) -> None:
        self.fx.tearDown()

    def _write_rollout(self, sessions: Path, thread_id: str) -> None:
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"rollout-{thread_id}").write_text(
            json.dumps(
                {
                    "thread_id": thread_id,
                    "agent_type": None,
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "xhigh",
                    "sandbox_policy": "read-only",
                    "permission_profile": "read-only",
                    "cwd": str(self.fx.repository_root),
                }
            ),
            encoding="utf-8",
        )

    def _review_result(self, role: str = "terra_xhigh_reviewer") -> dict[str, object]:
        return {
            "schema_version": "ai-result-1",
            "dispatch_id": None,
            "task_id": None,
            "step_id": None,
            "attempt": None,
            "role": role,
            "status": "ACCEPTANCE_RECOMMENDED",
            "summary": "ok",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "AWAITING_OWNER_DECISION",
        }

    def _controller_handler(self, thread_id: str, result: dict[str, object], *writes: str):
        def controller_process(command, *args, **kwargs):
            for relative in writes:
                path = self.fx.repository_root / relative
                path.write_text("OVER_BOUND = True\n", encoding="utf-8")
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text(json.dumps(result), encoding="utf-8")
            events = "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": thread_id}),
                    json.dumps({"type": "turn.completed"}),
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=events + "\n", stderr="")

        return controller_process

    def _issue_review(self):
        self.fx._open_with_owner("luna-owner", "luna")
        reviewer_thread = str(uuid.uuid4())
        review = self.fx._issue(
            "REVIEW_1",
            self.fx._expected_actor(
                "terra-gate-review",
                "terra_xhigh_reviewer",
                runtime_instance_id=reviewer_thread,
            ),
        )
        sessions = self.fx.repository_root.parent / "gate-sessions"
        self._write_rollout(sessions, reviewer_thread)
        return review, sessions, reviewer_thread

    def test_missing_declaration_rejects_assignment_before_spawn(self) -> None:
        review, sessions, thread_id = self._issue_review()
        (
            self.fx.store._require_task(self.fx.TASK_ID) / "route-declaration.json"
        ).unlink()
        _RecordingPopen.reset()
        with (
            mock.patch.object(
                repairs.subprocess,
                "Popen",
                _compat_popen(self._controller_handler(thread_id, self._review_result())),
            ),
            self.fx._controller_codex_lookup(),
        ):
            with self.assertRaisesRegex(
                workflow.WorkflowError, "ROUTE_DECLARATION_MISSING|ROUTE_DECLARATION_CORRUPT"
            ):
                repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)
        self.assertEqual([], _RecordingPopen.calls)
        intents = [
            json.loads(line)
            for line in (
                self.fx.store._require_task(self.fx.TASK_ID) / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            if line and json.loads(line).get("event_type") == "LAUNCH_INTENT_RECORDED"
        ]
        self.assertEqual([], intents)

    def test_role_not_allowed_rejects_assignment_before_spawn(self) -> None:
        self.fx._declaration_kwargs = {
            "allowed_roles": ("luna",),
            "active_roles": ("luna",),
        }
        review, sessions, thread_id = self._issue_review()
        _RecordingPopen.reset()
        with (
            mock.patch.object(
                repairs.subprocess,
                "Popen",
                _compat_popen(self._controller_handler(thread_id, self._review_result())),
            ),
            self.fx._controller_codex_lookup(),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROLE_NOT_ALLOWED"):
                repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)
        self.assertEqual([], _RecordingPopen.calls)

    def test_role_not_preflighted_rejects_assignment_before_spawn(self) -> None:
        self.fx._declaration_kwargs = {"run_preflight": False}
        review, sessions, thread_id = self._issue_review()
        _RecordingPopen.reset()
        with (
            mock.patch.object(
                repairs.subprocess,
                "Popen",
                _compat_popen(self._controller_handler(thread_id, self._review_result())),
            ),
            self.fx._controller_codex_lookup(),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROLE_NOT_PREFLIGHTED"):
                repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)
        self.assertEqual([], _RecordingPopen.calls)

    def test_budget_exceeded_rejects_assignment_before_spawn(self) -> None:
        self.fx._declaration_kwargs = {"max_dispatches": 0}
        review, sessions, thread_id = self._issue_review()
        _RecordingPopen.reset()
        with (
            mock.patch.object(
                repairs.subprocess,
                "Popen",
                _compat_popen(self._controller_handler(thread_id, self._review_result())),
            ),
            self.fx._controller_codex_lookup(),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_BUDGET_EXCEEDED"):
                repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)
        self.assertEqual([], _RecordingPopen.calls)

    def test_unknown_observation_does_not_complete_assignment(self) -> None:
        review, sessions, thread_id = self._issue_review()
        with (
            mock.patch.object(
                repairs, "_observe_assignment_execution_side_effects", return_value=None
            ),
            mock.patch.object(
                repairs.subprocess,
                "Popen",
                _compat_popen(self._controller_handler(thread_id, self._review_result())),
            ),
            self.fx._controller_codex_lookup(),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ACTUAL_WRITE_PATHS_UNKNOWN"):
                repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)

    def test_assignment_over_bound_write_records_violation(self) -> None:
        review, sessions, thread_id = self._issue_review()
        with (
            mock.patch.object(
                repairs.subprocess,
                "Popen",
                _compat_popen(
                    self._controller_handler(
                        thread_id, self._review_result(), "src/alpha.py"
                    )
                ),
            ),
            self.fx._controller_codex_lookup(),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "OWNERSHIP_VIOLATION"):
                repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)
        violations = [
            json.loads(line)
            for line in (
                self.fx.store._require_task(self.fx.TASK_ID) / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            if line and json.loads(line).get("event_type") == "OWNERSHIP_VIOLATION_RECORDED"
        ]
        self.assertEqual(1, len(violations))
        self.assertEqual(["src/alpha.py"], violations[0]["paths"])
        side_effects = ownership.load_side_effects(self.fx.store, self.fx.TASK_ID)
        self.assertFalse(
            any(row.get("effect_kind") == "OWNERSHIP_VIOLATION_RECORDED" for row in side_effects)
        )

    def test_assignment_hub_keeps_plan_owner_not_actor(self) -> None:
        review, sessions, thread_id = self._issue_review()
        with (
            mock.patch.object(
                repairs.subprocess,
                "Popen",
                _compat_popen(self._controller_handler(thread_id, self._review_result())),
            ),
            self.fx._controller_codex_lookup(),
        ):
            repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)
        registry = ownership.load_ownership_registry(self.fx.store, self.fx.TASK_ID)
        self.assertIsNotNone(registry)
        assert registry is not None
        self.assertEqual("luna", registry.path_owners["src"])
        self.assertNotEqual("terra_xhigh_reviewer", registry.path_owners["src"])
        sidecar = self.fx.store._require_task(self.fx.TASK_ID) / "runtime-evidence-v2.jsonl"
        self.assertTrue(sidecar.is_file())

    def test_legal_assignment_records_launch_intent_before_popen(self) -> None:
        review, sessions, thread_id = self._issue_review()
        order: list[str] = []
        real_append = workflow.WorkflowStore.append_event
        real_ledger = workflow.WorkflowStore.append_task_ledger

        def tracking_append(store, task_id, event):
            order.append(str(event.get("event_type")))
            return real_append(store, task_id, event)

        def tracking_ledger(store, task_id, name, record):
            if name == "dispatch-permits.jsonl":
                order.append(f"PERMIT:{record.get('state')}")
            return real_ledger(store, task_id, name, record)

        inner = _compat_popen(self._controller_handler(thread_id, self._review_result()))

        class Popen(inner):
            def __init__(self, command, *args, **kwargs):
                super().__init__(command, *args, **kwargs)
                if getattr(self, "_delegate", None) is None:
                    order.append("POPEN")

        with (
            mock.patch.object(workflow.WorkflowStore, "append_event", tracking_append),
            mock.patch.object(workflow.WorkflowStore, "append_task_ledger", tracking_ledger),
            mock.patch.object(repairs.subprocess, "Popen", Popen),
            self.fx._controller_codex_lookup(),
        ):
            repairs.run_assignment(self.fx.store, self.fx.TASK_ID, review, sessions)
        self.assertLess(order.index("PERMIT:RESERVED"), order.index("LAUNCH_INTENT_RECORDED"))
        self.assertLess(order.index("LAUNCH_INTENT_RECORDED"), order.index("POPEN"))
        events = [
            json.loads(line)
            for line in (
                self.fx.store._require_task(self.fx.TASK_ID) / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            if line
        ]
        intents = [event for event in events if event.get("event_type") == "LAUNCH_INTENT_RECORDED"]
        self.assertEqual(1, len(intents))
        self.assertEqual(artifacts.artifact_sha256(self.fx.task), intents[0]["envelope_hash"])
        recorded = [event for event in events if event.get("event_type") == "RUNTIME_EVIDENCE_RECORDED"]
        self.assertTrue(recorded)
        self.assertEqual(
            {
                "event_type",
                "attempt_id",
                "requested_role",
                "thread_id",
                "execution_surface",
                "runtime_evidence_sha256",
            },
            set(recorded[-1]),
        )


class VerdictReleaseGateRepairTest(unittest.TestCase):
    def setUp(self) -> None:
        from tests.test_ai_workflow_adversarial_acceptance import (
            AcceptanceLedgerV2ContractTest,
        )

        self.fx = AcceptanceLedgerV2ContractTest()
        self.fx.setUp()

    def tearDown(self) -> None:
        self.fx.tearDown()

    def test_review_one_completion_does_not_require_final_verdict(self) -> None:
        self.fx._open_with_owner("luna-owner", "luna")
        _, first, receipt = self.fx._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        self.fx._review(first, receipt, "ACCEPT")
        completed = [
            event
            for event in self.fx._events()
            if event.get("event_type") == "REVIEW_COMPLETED"
        ]
        self.assertEqual(1, len(completed))

    def test_terminal_repair_completed_without_verdict_is_missing(self) -> None:
        self.fx.task["source_worktree"] = str(self.fx.repository_root)
        owner_actor, _ = self.fx._open_with_owner("luna-owner", "luna")
        _, first, reviewer_one_receipt = self.fx._issue_with_receipt(
            "REVIEW_1", "terra-review-one", "terra_xhigh_reviewer"
        )
        self.fx._review(first, reviewer_one_receipt, "REWORK", self.fx.findings)
        _, owner_repair, owner_receipt = self.fx._issue_with_receipt(
            "OWNER_REPAIR", "luna-owner-repair", "luna", expected_actor=owner_actor
        )
        owner_candidate = self.fx._commit_file("src/alpha.py", "OWNER_ALPHA = 1\n")
        self.fx._complete(
            owner_repair, owner_receipt, owner_candidate, ("src/alpha.py",)
        )
        _, second, reviewer_two_receipt = self.fx._issue_with_receipt(
            "REVIEW_2", "terra-review-two", "terra_xhigh_reviewer"
        )
        self.fx._review(second, reviewer_two_receipt, "REWORK", self.fx.findings)
        _, sol_repair, sol_receipt = self.fx._issue_with_receipt(
            "SOL_MEDIUM_REPAIR", "sol-fixer", "sol_medium_reviewer"
        )
        sol_candidate = self.fx._commit_file("src/alpha.py", "SOL_MEDIUM_ALPHA = 1\n")
        self.fx._complete(sol_repair, sol_receipt, sol_candidate, ("src/alpha.py",))
        _, peer_review, peer_receipt = self.fx._issue_with_receipt(
            "SOL_MEDIUM_PEER_REVIEW", "sol-peer", "sol_medium_reviewer"
        )
        self.fx._review(peer_review, peer_receipt, "REWORK", self.fx.findings)
        _, terminal, receipt = self.fx._issue_with_receipt(
            "SOL_XHIGH_TERMINAL_REPAIR", "sol-xhigh", "sol_xhigh"
        )
        candidate = self.fx._commit_file("src/alpha.py", "TERMINAL_ALPHA = 1\n")
        with self.assertRaisesRegex(workflow.WorkflowError, "VERDICT_MISSING"):
            self.fx._complete(terminal, receipt, candidate, ("src/alpha.py",))
        self.assertFalse(
            any(
                event.get("event_type") == "REPAIR_COMPLETED"
                and event.get("terminal_reason") == "SOL_XHIGH_TERMINAL_REPAIR_COMPLETED"
                for event in self.fx._events()
            )
        )

    def test_v2_append_and_authorize_call_locked_gate(self) -> None:
        append_source = inspect.getsource(repairs._v2_append)
        authorize_source = inspect.getsource(repairs.authorize_final_xhigh)
        self.assertIn("require_verdict_fresh_locked", append_source)
        self.assertIn("require_verdict_fresh_locked", authorize_source)
        self.assertIn("Generic pipeline", append_source)
        self.assertNotIn(
            "require_verdict_fresh(",
            append_source.replace("require_verdict_fresh_locked", ""),
        )
        self.assertNotIn(
            "require_verdict_fresh(",
            authorize_source.replace("require_verdict_fresh_locked", ""),
        )

    def test_run_assignment_requires_locked_dispatch_permit(self) -> None:
        source = inspect.getsource(self.fx._original_run_assignment)
        self.assertIn("require_dispatch_permit_locked(", source)
        self.assertIn("claim_permit_start_locked(", source)
        self.assertIn("release_permit_if_never_spawned(", source)


if __name__ == "__main__":
    unittest.main()
