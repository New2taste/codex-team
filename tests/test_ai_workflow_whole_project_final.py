"""Whole-project concentrated Sol final-acceptance ladder.

These tests reuse the adversarial-acceptance-1 public API and fixtures.  They
do not change ordinary REMEDIATION ladder semantics.
"""

from __future__ import annotations

import json
import hashlib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_repairs as repairs
from scripts import ai_workflow_scheduler as scheduler

_FROZEN_FINAL_ACCEPTANCE_REWORK = {
    "fixer_role": "sol_medium_reviewer",
    "fixer_permission_profile": "assignment-scoped-write",
    "fixer_distinct_from_acceptor": True,
    "recheck_role": "sol_medium_reviewer",
    "recheck_distinct_from_fixer": True,
    "terminal_escalation_role": "sol_xhigh",
    "terminal_review_required": False,
}


def _acceptance_harness():
    from tests.test_ai_workflow_adversarial_acceptance import AcceptanceLedgerV2ContractTest

    return AcceptanceLedgerV2ContractTest()


class WholeProjectFinalAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _acceptance_harness()
        self.fx.setUp()
        self.fx.task["task_type"] = "ACCEPTANCE"
        self.fx.task["human_gates"] = ["FINAL_ACCEPTANCE", "XHIGH_APPROVAL"]
        self.fx.task["objective"] = "Concentrated whole-project Sol final acceptance"
        self.fx.task["allowed_write_paths"] = ["src/alpha.py", "src/beta.py"]
        parent = {
            **self.fx.task,
            "task_id": "AWF-20260809-900",
            "task_type": "REMEDIATION",
            "objective": "Complete every frozen engineering section",
            "human_gates": ["EXECUTION_APPROVAL"],
            "allowed_write_paths": ["src/alpha.py", "src/beta.py"],
        }
        self.fx.store.create_task(parent)
        plan = {
            "schema_version": "ai-plan-1",
            "plan_id": "plan-20260809-final",
            "task_id": parent["task_id"],
            "goal": self.fx.task["objective"],
            "done_when": ["the engineering section is complete"],
            "tasks": [
                {
                    "id": "section-final",
                    "owner_role": "terra",
                    "read_scope": [],
                    "write_scope": ["src/alpha.py", "src/beta.py"],
                    "do_not_touch": [],
                    "depends_on": [],
                    "expected_result": "the final section is implemented",
                    "verification_commands": ["git diff --check"],
                    "first_artifact": "src/alpha.py",
                    "evidence_level": "L2",
                }
            ],
            "stages": [["section-final"]],
        }
        frozen = workflow.validate_plan(plan, parent)
        proposal = scheduler.dispatch_ready_batch(self.fx.store, frozen)[0]
        result = workflow.FakeRunner().run("terra", parent)
        result.update(
            {
                "dispatch_id": proposal["dispatch_id"],
                "task_id": parent["task_id"],
                "step_id": proposal["subtask_id"],
                "attempt": proposal["attempt"],
            }
        )
        result_bytes = (workflow._canonical_json(result) + "\n").encode("utf-8")
        result_path = (
            self.fx.state_root
            / parent["task_id"]
            / "scheduler-results"
            / f"{proposal['dispatch_id']}.json"
        )
        result_path.parent.mkdir()
        result_path.write_bytes(result_bytes)
        scheduler.record_step_receipt(
            self.fx.store,
            frozen,
            {
                "schema_version": "construction-receipt-1",
                "task_id": parent["task_id"],
                "subtask_id": proposal["subtask_id"],
                "dispatch_id": proposal["dispatch_id"],
                "plan_sha256": frozen.plan_sha256,
                "task_sha256": frozen.task_sha256,
                "candidate_commit": frozen.candidate_commit,
                "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                "status": "IMPLEMENTED_CANDIDATE",
            },
        )
        projected = scheduler.create_final_acceptance_case(
            self.fx.store,
            frozen,
            self.fx.TASK_ID,
            self.fx.input_candidate,
        )
        self.assertEqual(self.fx.task, projected)
        original_create = self.fx._create_task

        def create_bound_task(task=None):
            document = dict(task or self.fx.task)
            path = self.fx.state_root / document["task_id"] / "task.json"
            if path.exists():
                self.assertEqual(document, workflow.load_task(path))
                return
            original_create(document)

        self.fx._create_task = create_bound_task

    def tearDown(self) -> None:
        self.fx.tearDown()

    def _events_path(self) -> Path:
        return self.fx.state_root / self.fx.TASK_ID / "events.jsonl"

    def _ledger_bytes(self) -> bytes:
        path = self._events_path()
        return path.read_bytes() if path.exists() else b""

    def _open_construction_owner(self):
        return self.fx._open_with_owner("luna-construction-owner", "luna_construction")

    def _open_sol_review_one(self):
        owner_actor, owner_receipt = self._open_construction_owner()
        acceptor_actor, first, acceptor_receipt = self.fx._issue_with_receipt(
            "REVIEW_1", "sol-acceptor", "sol_medium_reviewer"
        )
        return owner_actor, owner_receipt, acceptor_actor, first, acceptor_receipt

    def _advance_to_fixer(self):
        owner_actor, _, acceptor_actor, first, acceptor_receipt = self._open_sol_review_one()
        self.fx._review(first, acceptor_receipt, "REWORK", self.fx.findings)
        fixer_actor, repair, fixer_receipt = self.fx._issue_with_receipt(
            "SOL_MEDIUM_REPAIR", "sol-fixer", "sol_medium_reviewer"
        )
        return (
            owner_actor,
            acceptor_actor,
            acceptor_receipt,
            fixer_actor,
            repair,
            fixer_receipt,
        )

    def _advance_to_peer_rework(self):
        (
            owner_actor,
            acceptor_actor,
            _,
            fixer_actor,
            repair,
            fixer_receipt,
        ) = self._advance_to_fixer()
        owner_candidate = self.fx._commit_file("src/alpha.py", "SOL_FIXER_ALPHA = 1\n")
        self.fx._complete(repair, fixer_receipt, owner_candidate, ("src/alpha.py",))
        recheck_actor, peer, recheck_receipt = self.fx._issue_with_receipt(
            "SOL_MEDIUM_PEER_REVIEW", "sol-recheck", "sol_medium_reviewer"
        )
        self.fx._review(peer, recheck_receipt, "REWORK", self.fx.findings)
        return owner_actor, acceptor_actor, fixer_actor, recheck_actor

    def test_review_one_uses_sol_medium_acceptor(self):
        _, _, acceptor_actor, first, _ = self._open_sol_review_one()
        self.assertEqual("sol_medium_reviewer", first.expected_actor.role)
        self.assertEqual(acceptor_actor, first.expected_actor)
        self.assertEqual("REVIEW_1", first.phase)

    def test_review_one_rejects_terra_acceptor(self):
        self._open_construction_owner()
        with self.assertRaises(workflow.WorkflowError) as raised:
            self.fx._issue(
                "REVIEW_1",
                self.fx._expected_actor("terra-review-one", "terra_xhigh_reviewer"),
            )
        self.assertEqual("ACCEPTANCE_SEQUENCE_INVALID", raised.exception.code)

    def test_review_one_rework_skips_owner_repair_and_review_two(self):
        owner_actor, _, _, first, acceptor_receipt = self._open_sol_review_one()
        self.fx._review(first, acceptor_receipt, "REWORK", self.fx.findings)
        with self.assertRaises(workflow.WorkflowError):
            self.fx._issue("OWNER_REPAIR", owner_actor)
        with self.assertRaises(workflow.WorkflowError):
            self.fx._issue(
                "REVIEW_2",
                self.fx._expected_actor("terra-review-two", "terra_xhigh_reviewer"),
            )
        _, repair, _ = self.fx._issue_with_receipt(
            "SOL_MEDIUM_REPAIR", "sol-fixer", "sol_medium_reviewer"
        )
        self.assertEqual("SOL_MEDIUM_REPAIR", repair.phase)
        phases = [
            event["phase"]
            for event in self.fx._events()
            if event.get("event_type") == "ASSIGNMENT_ISSUED"
        ]
        self.assertEqual(["REVIEW_1", "SOL_MEDIUM_REPAIR"], phases)
        self.assertNotIn("OWNER_REPAIR", phases)
        self.assertNotIn("REVIEW_2", phases)

    def test_fixer_cannot_reuse_acceptor_identity(self):
        _, _, acceptor_actor, first, acceptor_receipt = self._open_sol_review_one()
        self.fx._review(first, acceptor_receipt, "REWORK", self.fx.findings)
        with self.assertRaises(workflow.WorkflowError) as raised:
            self.fx._issue("SOL_MEDIUM_REPAIR", acceptor_actor)
        self.assertEqual("ACCEPTANCE_SEQUENCE_INVALID", raised.exception.code)

    def test_recheck_cannot_match_acceptor_or_fixer(self):
        (
            _,
            acceptor_actor,
            _,
            fixer_actor,
            repair,
            fixer_receipt,
        ) = self._advance_to_fixer()
        candidate = self.fx._commit_file("src/alpha.py", "SOL_FIXER_ALPHA = 1\n")
        self.fx._complete(repair, fixer_receipt, candidate, ("src/alpha.py",))
        with self.assertRaises(workflow.WorkflowError):
            self.fx._issue("SOL_MEDIUM_PEER_REVIEW", acceptor_actor)
        with self.assertRaises(workflow.WorkflowError):
            self.fx._issue("SOL_MEDIUM_PEER_REVIEW", fixer_actor)
        _, peer, _ = self.fx._issue_with_receipt(
            "SOL_MEDIUM_PEER_REVIEW", "sol-recheck", "sol_medium_reviewer"
        )
        self.assertEqual("SOL_MEDIUM_PEER_REVIEW", peer.phase)

    def test_peer_rework_without_owner_xhigh_authorization_leaves_ledger_bytes(self):
        self._advance_to_peer_rework()
        before = self._ledger_bytes()
        with self.assertRaises(workflow.WorkflowError) as raised:
            self.fx._issue(
                "SOL_XHIGH_TERMINAL_REPAIR",
                self.fx._expected_actor("sol-xhigh", "sol_xhigh"),
            )
        self.assertNotEqual("ACCEPT", getattr(raised.exception, "code", ""))
        self.assertEqual(before, self._ledger_bytes())
        self.assertFalse(
            any(
                event.get("event_type") == "ASSIGNMENT_ISSUED"
                and event.get("phase") == "SOL_XHIGH_TERMINAL_REPAIR"
                for event in self.fx._events()
            )
        )

    def test_owner_authorized_xhigh_may_issue_once(self):
        self._advance_to_peer_rework()
        authorize = getattr(repairs, "authorize_final_xhigh", None)
        self.assertIsNotNone(authorize, "explicit owner xhigh authorization API is required")
        authorize(self.fx.store, self.fx.TASK_ID, "owner")
        with self.assertRaises(workflow.WorkflowError):
            authorize(self.fx.store, self.fx.TASK_ID, "owner")
        _, terminal, receipt = self.fx._issue_with_receipt(
            "SOL_XHIGH_TERMINAL_REPAIR", "sol-xhigh", "sol_xhigh"
        )
        self.assertEqual("sol_xhigh", terminal.expected_actor.role)
        candidate = self.fx._commit_file("src/alpha.py", "SOL_XHIGH_ALPHA = 1\n")
        self.fx._complete(terminal, receipt, candidate, ("src/alpha.py",))
        with self.assertRaises(workflow.WorkflowError):
            self.fx._issue(
                "SOL_XHIGH_TERMINAL_REPAIR",
                self.fx._expected_actor("sol-xhigh-again", "sol_xhigh"),
            )

    def test_authorize_final_xhigh_rejects_empty_actor(self):
        self._advance_to_peer_rework()
        authorize = getattr(repairs, "authorize_final_xhigh", None)
        self.assertIsNotNone(authorize)
        with self.assertRaises(workflow.WorkflowError):
            authorize(self.fx.store, self.fx.TASK_ID, "  ")

    def _decisions_path(self) -> Path:
        return self.fx.state_root / self.fx.TASK_ID / "human-decisions.jsonl"

    def _append_owner_decision(self, record: dict[str, object]) -> None:
        self.fx.store.record_decision(self.fx.TASK_ID, record)

    def _complete_xhigh_ticket(self, **overrides: object) -> dict[str, object]:
        replay = repairs.replay_acceptance_ledger(self.fx.store, self.fx.TASK_ID)
        self.assertIsNotNone(replay)
        ticket = {
            "event_type": "OWNER_DECISION",
            "decision": "authorize_final_xhigh",
            "actor": "owner",
            "timestamp_utc": "2026-08-26T00:00:00Z",
            "previous_state": workflow._current_state(self.fx.store, self.fx.TASK_ID),
            "new_state": "FINAL_XHIGH_AUTHORIZED",
            "task_sha256": workflow._task_sha256(self.fx.store, self.fx.TASK_ID),
            "candidate_commit": replay.current_candidate_commit,
            "acceptance_event_id": replay.last_event_id,
        }
        ticket.update(overrides)
        return ticket

    def _issue_xhigh(self):
        return self.fx._issue(
            "SOL_XHIGH_TERMINAL_REPAIR",
            self.fx._expected_actor("sol-xhigh", "sol_xhigh"),
        )

    def _tampered_config(self, **policy_overrides: object) -> dict[str, object]:
        config = workflow._load_workflow_config()
        policy = dict(config["final_acceptance_rework"])
        for key, value in policy_overrides.items():
            if value is Ellipsis:
                policy.pop(key, None)
            else:
                policy[key] = value
        mutated = dict(config)
        mutated["final_acceptance_rework"] = policy
        return mutated

    def test_authorize_binds_candidate_and_acceptance_event(self):
        self._advance_to_peer_rework()
        replay = repairs.replay_acceptance_ledger(self.fx.store, self.fx.TASK_ID)
        repairs.authorize_final_xhigh(self.fx.store, self.fx.TASK_ID, "owner")
        record = json.loads(self._decisions_path().read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(replay.current_candidate_commit, record["candidate_commit"])
        self.assertEqual(replay.last_event_id, record["acceptance_event_id"])
        self.assertEqual(workflow._task_sha256(self.fx.store, self.fx.TASK_ID), record["task_sha256"])

    def test_later_unrelated_owner_decision_does_not_hide_valid_xhigh_ticket(self):
        self._advance_to_peer_rework()
        repairs.authorize_final_xhigh(self.fx.store, self.fx.TASK_ID, "owner")
        self._append_owner_decision(
            {
                "event_type": "OWNER_DECISION",
                "decision": "defer",
                "actor": "owner",
                "timestamp_utc": "2026-08-26T00:00:01Z",
                "previous_state": workflow._current_state(self.fx.store, self.fx.TASK_ID),
                "new_state": "DEFERRED",
                "task_sha256": workflow._task_sha256(self.fx.store, self.fx.TASK_ID),
            }
        )
        assignment = self._issue_xhigh()
        self.assertEqual("SOL_XHIGH_TERMINAL_REPAIR", assignment.phase)

    def test_forged_xhigh_ticket_missing_fields_or_wrong_hash_leaves_ledger(self):
        self._advance_to_peer_rework()
        replay = repairs.replay_acceptance_ledger(self.fx.store, self.fx.TASK_ID)
        forgeries = (
            {
                "event_type": "OWNER_DECISION",
                "decision": "authorize_final_xhigh",
                "actor": "owner",
                "timestamp_utc": "2026-08-26T00:00:00Z",
                "previous_state": "DRAFT",
                "new_state": "FINAL_XHIGH_AUTHORIZED",
                "task_sha256": workflow._task_sha256(self.fx.store, self.fx.TASK_ID),
            },
            self._complete_xhigh_ticket(task_sha256="0" * 64),
            self._complete_xhigh_ticket(candidate_commit="a" * 40),
            self._complete_xhigh_ticket(acceptance_event_id="b" * 64),
        )
        for forged in forgeries:
            with self.subTest(forged={key: forged.get(key) for key in ("task_sha256", "candidate_commit", "acceptance_event_id") if key in forged or "candidate_commit" not in forged}):
                self._decisions_path().write_text("", encoding="utf-8")
                self._append_owner_decision(forged)
                before = self._ledger_bytes()
                with self.assertRaises(workflow.WorkflowError) as raised:
                    self._issue_xhigh()
                self.assertEqual("ACCEPTANCE_SEQUENCE_INVALID", raised.exception.code)
                self.assertEqual(before, self._ledger_bytes())
        self.assertEqual(replay.last_event_id, repairs.replay_acceptance_ledger(self.fx.store, self.fx.TASK_ID).last_event_id)

    def test_authorize_before_peer_rework_is_rejected(self):
        self._open_sol_review_one()
        with self.assertRaises(workflow.WorkflowError) as raised:
            repairs.authorize_final_xhigh(self.fx.store, self.fx.TASK_ID, "owner")
        self.assertEqual("ACCEPTANCE_SEQUENCE_INVALID", raised.exception.code)
        self.assertFalse(self._decisions_path().exists())

    def test_stale_ticket_bindings_reject_when_replay_position_differs(self):
        self._advance_to_peer_rework()
        replay = repairs.replay_acceptance_ledger(self.fx.store, self.fx.TASK_ID)
        stale = self._complete_xhigh_ticket(acceptance_event_id="c" * 64)
        self.assertNotEqual(replay.last_event_id, stale["acceptance_event_id"])
        self._append_owner_decision(stale)
        before = self._ledger_bytes()
        with self.assertRaises(workflow.WorkflowError) as raised:
            self._issue_xhigh()
        self.assertEqual("ACCEPTANCE_SEQUENCE_INVALID", raised.exception.code)
        self.assertEqual(before, self._ledger_bytes())

    def test_tampered_final_acceptance_rework_policy_fails_before_assignment(self):
        tampers = (
            {"fixer_role": "sol_xhigh"},
            {"fixer_permission_profile": "workspace-write"},
            {"fixer_distinct_from_acceptor": False},
            {"recheck_role": "sol_xhigh"},
            {"recheck_distinct_from_fixer": False},
            {"terminal_escalation_role": "sol_medium_reviewer"},
            {"terminal_review_required": True},
            {"unexpected": True},
            {"fixer_role": Ellipsis},
        )
        self.assertEqual(_FROZEN_FINAL_ACCEPTANCE_REWORK, workflow._load_workflow_config()["final_acceptance_rework"])
        for overrides in tampers:
            with self.subTest(overrides=overrides):
                self.tearDown()
                self.setUp()
                self._open_construction_owner()
                before = self._ledger_bytes()
                with mock.patch.object(
                    workflow,
                    "_load_workflow_config",
                    return_value=self._tampered_config(**overrides),
                ):
                    with self.assertRaises(workflow.WorkflowError) as raised:
                        self.fx._issue(
                            "REVIEW_1",
                            self.fx._expected_actor("sol-acceptor", "sol_medium_reviewer"),
                        )
                self.assertEqual("ACCEPTANCE_SEQUENCE_INVALID", raised.exception.code)
                self.assertEqual(before, self._ledger_bytes())

    def test_blocked_and_reject_are_not_accept(self):
        _, _, _, first, acceptor_receipt = self._open_sol_review_one()
        for verdict in ("BLOCKED", "REJECT"):
            with self.subTest(verdict=verdict):
                with self.assertRaises(workflow.WorkflowError):
                    self.fx._review(first, acceptor_receipt, verdict)

    def test_skipping_review_one_is_rejected(self):
        self._open_construction_owner()
        with self.assertRaises(workflow.WorkflowError):
            self.fx._issue(
                "SOL_MEDIUM_REPAIR",
                self.fx._expected_actor("sol-fixer", "sol_medium_reviewer"),
            )
        with self.assertRaises(workflow.WorkflowError):
            self.fx._issue(
                "SOL_XHIGH_TERMINAL_REPAIR",
                self.fx._expected_actor("sol-xhigh", "sol_xhigh"),
            )

    def test_v2_open_still_blocks_generic_runner(self):
        self._open_construction_owner()
        self.assertTrue(repairs.repair_ledger_claims_task(self.fx.store, self.fx.TASK_ID))

        class UnexpectedGenericRunner:
            is_live_model = False

            def __init__(self) -> None:
                self.calls: list[str] = []

            def run(self, role: str, task: object) -> dict[str, object]:
                self.calls.append(role)
                return {}

        runner = UnexpectedGenericRunner()
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.fx.state_root):
            with self.assertRaises(workflow.WorkflowError) as raised:
                workflow.run_until_gate(
                    self.fx.TASK_ID,
                    runner=runner,
                    allow_live_model=False,
                    state_root=self.fx.state_root,
                )
        self.assertEqual("REPAIR_ADAPTER_REQUIRED", raised.exception.code)
        self.assertEqual([], runner.calls)

    def test_ordinary_remediation_still_rejects_luna_construction_owner(self):
        fx = _acceptance_harness()
        fx.setUp()
        try:
            receipt = fx._open_owner_receipt("luna-construction-owner", "luna_construction")
            fx._create_task()
            fx._record_runtime_evidence(receipt)
            with self.assertRaises(workflow.WorkflowError):
                repairs.open_task_acceptance(fx.store, fx.task, receipt)
        finally:
            fx.tearDown()

    def test_ordinary_remediation_ladder_still_uses_owner_repair(self):
        fx = _acceptance_harness()
        fx.setUp()
        try:
            owner_actor, _, reviewer_actor, first, reviewer_receipt = fx._open_review_one()
            self.assertEqual("terra_xhigh_reviewer", first.expected_actor.role)
            self.assertEqual("terra_xhigh_reviewer", reviewer_actor.role)
            fx._review(first, reviewer_receipt, "REWORK", fx.findings)
            _, owner_repair, _ = fx._issue_with_receipt(
                "OWNER_REPAIR",
                "luna-owner-repair",
                "luna",
                expected_actor=owner_actor,
            )
            self.assertEqual("OWNER_REPAIR", owner_repair.phase)
        finally:
            fx.tearDown()

    def test_standalone_acceptance_terra_route_is_unchanged(self):
        task = {
            "schema_version": "ai-task-1",
            "task_id": "AWF-20260808-901",
            "task_type": "ACCEPTANCE",
            "objective": "produce the bounded read-only workflow result",
            "repository_root": str(self.fx.repository_root),
            "source_worktree": None,
            "base_commit": self.fx.base_commit,
            "candidate_commit": self.fx.input_candidate,
            "authoritative_files": ["README.md"],
            "allowed_write_paths": [],
            "forbidden_actions": ["merge", "push"],
            "risk_flags": [],
            "acceptance_commands": ["python3 -m unittest"],
            "verification_level": "L1",
            "human_gates": ["FINAL_ACCEPTANCE"],
        }
        request = {
            "schema_version": "ai-route-request-1",
            "task_id": task["task_id"],
            "work_class": "PLANNING_ONLY",
            "execution_need": "READ_ONLY",
            "decomposable": True,
            "risk_flags": [],
            "reason_codes": ["PLAN_IS_DELIVERABLE"],
        }
        decision = workflow.decide_route(task, request, "enforced")
        self.assertEqual(("terra_xhigh_reviewer",), decision.roles)
        self.assertNotIn("sol_medium_reviewer", decision.roles)

    def test_unrelated_corrupt_scheduler_artifacts_do_not_affect_bound_child(self):
        unrelated = {
            **self.fx.task,
            "task_id": "AWF-20260809-899",
            "task_type": "REMEDIATION",
            "human_gates": ["EXECUTION_APPROVAL"],
        }
        self.fx.store.create_task(unrelated)
        directory = self.fx.state_root / unrelated["task_id"]
        (directory / "scheduler-plan.json").write_text("{broken", encoding="utf-8")
        (directory / "scheduler.jsonl").write_text("{broken\n", encoding="utf-8")

        self.assertTrue(
            repairs._is_whole_project_final(self.fx.task, store=self.fx.store)
        )

    def test_decide_authorize_final_xhigh_records_without_resume_or_generic_state(self):
        self._advance_to_peer_rework()
        previous_state = workflow._current_state(self.fx.store, self.fx.TASK_ID)
        output = StringIO()
        with redirect_stdout(output):
            exit_code = workflow.main(
                [
                    "decide",
                    self.fx.TASK_ID,
                    "authorize_final_xhigh",
                    "--by",
                    "owner",
                    "--root",
                    str(self.fx.state_root),
                ]
            )
        self.assertEqual(0, exit_code)
        self.assertEqual("DECISION_RECORDED\n", output.getvalue())
        self.assertEqual(previous_state, workflow._current_state(self.fx.store, self.fx.TASK_ID))
        record = json.loads(self._decisions_path().read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual("authorize_final_xhigh", record["decision"])
        self.assertEqual("FINAL_XHIGH_AUTHORIZED", record["new_state"])

        resume = StringIO()
        with redirect_stderr(resume):
            resume_exit = workflow.main(
                [
                    "decide",
                    self.fx.TASK_ID,
                    "authorize_final_xhigh",
                    "--resume",
                    "--by",
                    "owner",
                    "--root",
                    str(self.fx.state_root),
                ]
            )
        self.assertEqual(2, resume_exit)
        self.assertIn("RESUME", resume.getvalue())
        self.assertEqual(1, len(self._final_xhigh_tickets()))

    def test_ordinary_decide_still_uses_closed_owner_decisions(self):
        self.assertNotIn("authorize_final_xhigh", workflow.OWNER_DECISIONS)
        fx = _acceptance_harness()
        fx.setUp()
        try:
            fx._create_task()
            errors = StringIO()
            with redirect_stderr(errors):
                exit_code = workflow.main(
                    ["decide", fx.TASK_ID, "defer", "--by", "owner", "--root", str(fx.state_root)]
                )
            self.assertEqual(2, exit_code)
            self.assertIn("OWNER_DECISION_NOT_APPLICABLE", errors.getvalue())
            self.assertFalse((fx.state_root / fx.TASK_ID / "human-decisions.jsonl").exists())
        finally:
            fx.tearDown()

    def _final_xhigh_tickets(self):
        path = self._decisions_path()
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("decision") == "authorize_final_xhigh"
        ]


class WholeProjectFinalClassificationTest(unittest.TestCase):
    def test_standalone_final_acceptance_is_not_whole_project_final(self):
        fx = _acceptance_harness()
        fx.setUp()
        try:
            fx.task["task_type"] = "ACCEPTANCE"
            fx.task["human_gates"] = ["FINAL_ACCEPTANCE"]
            fx._create_task()
            self.assertFalse(repairs._is_whole_project_final(fx.task, store=fx.store))
        finally:
            fx.tearDown()

    def test_forged_scheduler_child_hash_does_not_classify_as_whole_project(self):
        fx = _acceptance_harness()
        fx.setUp()
        try:
            fx.task["task_type"] = "ACCEPTANCE"
            fx.task["human_gates"] = ["FINAL_ACCEPTANCE"]
            fx._create_task()
            parent = {
                **fx.task,
                "task_id": "AWF-20260809-900",
                "task_type": "REMEDIATION",
                "human_gates": ["EXECUTION_APPROVAL"],
            }
            fx.store.create_task(parent)
            common = {
                "schema_version": "plan-scheduler-1",
                "timestamp": "2026-08-26T00:00:00Z",
                "task_id": parent["task_id"],
                "plan_sha256": "a" * 64,
                "task_sha256": workflow.artifact_sha256(parent),
            }
            opened = {
                **common,
                "event_type": "SCHEDULER_OPENED",
                "event_index": 0,
                "previous_event_id": None,
                "candidate_commit": parent["candidate_commit"],
            }
            opened["event_id"] = scheduler._event_id(opened)
            final = {
                **common,
                "event_type": "FINAL_ACCEPTANCE_OPENED",
                "event_index": 1,
                "previous_event_id": opened["event_id"],
                "acceptance_task_id": fx.TASK_ID,
                "candidate_commit": fx.task["candidate_commit"],
                "acceptance_task_sha256": "0" * 64,
            }
            final["event_id"] = scheduler._event_id(final)
            ledger = fx.state_root / parent["task_id"] / "scheduler.jsonl"
            ledger.write_text(
                workflow._canonical_json(opened)
                + "\n"
                + workflow._canonical_json(final)
                + "\n",
                encoding="utf-8",
            )
            self.assertFalse(repairs._is_whole_project_final(fx.task, store=fx.store))
        finally:
            fx.tearDown()


if __name__ == "__main__":
    unittest.main()
