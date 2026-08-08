import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts import ai_workflow as workflow
from scripts import ai_workflow_routing as routing
from tests.test_ai_workflow_construction_execution import (
    BoundConstructionRunner,
    construction_plan,
    remediation_task,
    route_request,
    valid_envelope,
)


class FrozenConstructionAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "state"
        self.store = workflow.WorkflowStore(self.root)
        self.task = remediation_task()
        self.plan = construction_plan(task=self.task)
        self.request = route_request(self.task)
        self.store.create_task(self.task)
        decision = workflow.decide_route(
            self.task,
            self.request,
            "enforced",
            construction_plan=self.plan,
            construction_step_id="construction-601",
        )
        workflow.record_route_decision(self.store, self.task["task_id"], decision)

    def tearDown(self):
        self.temporary.cleanup()

    def _freeze(self):
        self.assertEqual(
            "AWAITING_OWNER_DECISION",
            workflow.run_until_gate(
                self.task["task_id"],
                runner=BoundConstructionRunner(),
                allow_live_model=False,
                construction_plan=self.plan,
                construction_request=self.request,
                construction_step_id="construction-601",
                construction_attempt=1,
                state_root=self.root,
            ),
        )

    def test_rewriting_both_supplied_and_persisted_plan_after_gate_is_rejected(self):
        self._freeze()
        changed = copy.deepcopy(self.plan)
        changed["tasks"][0]["expected_result"] = "silently expanded result"
        plan_path = self.root / self.task["task_id"] / "construction-plan.json"
        plan_path.write_text(json.dumps(changed), encoding="utf-8")
        workflow._apply_owner_decision(
            self.store, self.task["task_id"], "approve_execution", "owner"
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "CONSTRUCTION_AUTHORITY_DRIFT"):
            workflow.run_until_gate(
                self.task["task_id"], runner=BoundConstructionRunner(), allow_live_model=False,
                construction_plan=changed, construction_request=self.request,
                construction_step_id="construction-601", construction_attempt=1,
                state_root=self.root,
            )

    def test_route_decision_is_write_once(self):
        replacement = workflow.decide_route(
            self.task, self.request, "enforced",
            construction_plan=self.plan, construction_step_id="construction-601",
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_ALREADY_FROZEN"):
            workflow.record_route_decision(self.store, self.task["task_id"], replacement)

    def test_first_freeze_event_binds_complete_owner_decision(self):
        self._freeze()
        events = [json.loads(line) for line in (
            self.root / self.task["task_id"] / "events.jsonl"
        ).read_text().splitlines()]
        frozen = next(event for event in events if event["event_type"] == "CONSTRUCTION_PLAN_FROZEN")
        self.assertEqual(
            {
                "task_sha256", "plan_sha256", "request_sha256", "route", "rule_id",
                "routing_mode", "step_id", "role", "candidate_commit", "scope_sha256",
            },
            set(frozen) - {"event_type", "timestamp_utc"},
        )


class ControllerEvidenceExecutionTest(unittest.TestCase):
    def test_shell_noop_and_unbound_commands_are_rejected_by_plan_validation(self):
        task = remediation_task()
        for command in ("sh -c true", "command true", "false"):
            with self.subTest(command=command):
                envelope = valid_envelope("src/parser.py")
                envelope["evidence"]["L1"]["command"] = command
                with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
                    workflow.validate_plan(construction_plan(task=task, envelope=envelope), task)

    def test_nonexistent_l0_artifact_cannot_be_attested_by_fake_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            task = remediation_task()
            task["repository_root"] = temporary
            plan = construction_plan(task=task)
            request = route_request(task)
            store = workflow.WorkflowStore(root)
            store.create_task(task)
            decision = workflow.decide_route(
                task, request, "enforced", construction_plan=plan,
                construction_step_id="construction-601",
            )
            workflow.record_route_decision(store, task["task_id"], decision)
            runner = BoundConstructionRunner()
            workflow.run_until_gate(
                task["task_id"], runner=runner, allow_live_model=False,
                construction_plan=plan, construction_request=request,
                construction_step_id="construction-601", construction_attempt=1,
                state_root=root,
            )
            workflow._apply_owner_decision(store, task["task_id"], "approve_execution", "owner")
            self.assertEqual(
                "BLOCKED",
                workflow.run_until_gate(
                    task["task_id"], runner=runner, allow_live_model=False,
                    construction_plan=plan, construction_request=request,
                    construction_step_id="construction-601", construction_attempt=1,
                    state_root=root,
                ),
            )


class TerraOSDefaultExecutionTest(unittest.TestCase):
    def test_plan_and_acceptance_generic_execution_fail_closed(self):
        for index, task_type in enumerate(("PLAN", "ACCEPTANCE"), start=1):
            with self.subTest(task_type=task_type), tempfile.TemporaryDirectory() as temporary:
                task = remediation_task()
                task["task_id"] = f"AWF-20260808-70{index}"
                task["task_type"] = task_type
                task["allowed_write_paths"] = []
                if task_type == "ACCEPTANCE":
                    task["human_gates"] = ["FINAL_ACCEPTANCE"]
                store = workflow.WorkflowStore(Path(temporary) / "state")
                store.create_task(task)
                with self.assertRaisesRegex(workflow.WorkflowError, "TERRA_OS_DECISION_REQUIRED"):
                    workflow.run_until_gate(
                        task["task_id"], runner=workflow.FakeRunner(),
                        allow_live_model=False, state_root=store.root,
                    )


class LunaScopeAndOwnerTest(unittest.TestCase):
    def test_casefolded_control_scope_and_wide_control_parent_are_rejected(self):
        for parent, scope in (
            (".GIT", ".GIT/config"), ("scripts", "scripts"),
            ("config", "config"), ("data", "data"),
        ):
            with self.subTest(scope=scope):
                task = remediation_task(paths=[parent])
                with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
                    workflow.validate_plan(construction_plan(task=task, scope=scope), task)

    def test_symlink_scope_is_rejected_before_freeze(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "docs").mkdir()
            os.symlink("../outside", repo / "docs" / "link")
            task = remediation_task(paths=["docs"])
            task["repository_root"] = str(repo)
            with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
                workflow.validate_plan(
                    construction_plan(task=task, scope="docs/link/file.md"), task
                )

    def test_uncertain_authorization_language_is_not_luna_eligible(self):
        task = remediation_task(
            objective="Let team leads decide which employees may use each internal tool."
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            workflow.validate_plan(construction_plan(task=task), task)

    def test_enforced_construction_rejects_every_non_construction_owner(self):
        for index, owner in enumerate((
            "luna", "sol_planner", "sol_reviewer", "sol_medium_reviewer",
            "sol_xhigh", "terra",
        ), start=1):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temporary:
                task = remediation_task()
                task["task_id"] = f"AWF-20260808-8{index:02d}"
                plan = construction_plan(task=task, owner_role=owner)
                request = route_request(task)
                store = workflow.WorkflowStore(Path(temporary) / "state")
                store.create_task(task)
                decision = workflow.decide_route(
                    task, request, "enforced", construction_plan=plan,
                    construction_step_id="construction-601",
                )
                workflow.record_route_decision(store, task["task_id"], decision)
                with self.assertRaisesRegex(workflow.WorkflowError, "CONSTRUCTION_OWNER_INVALID"):
                    workflow._load_enforced_construction_artifacts(
                        store, task["task_id"], plan, request, "construction-601"
                    )


if __name__ == "__main__":
    unittest.main()
