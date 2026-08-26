import json
import hashlib
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow


ROOT = Path(__file__).resolve().parents[1]


def remediation_task(*, objective="implement one isolated parser behavior", paths=None):
    return {
        "schema_version": "ai-task-1",
        "task_id": "AWF-20260808-601",
        "task_type": "REMEDIATION",
        "objective": objective,
        "repository_root": str(ROOT),
        "source_worktree": None,
        "base_commit": "b" * 40,
        "candidate_commit": "c" * 40,
        "authoritative_files": ["README.md"],
        "allowed_write_paths": ["tests/fixtures"] if paths is None else paths,
        "forbidden_actions": ["merge", "push"],
        "risk_flags": [],
        "acceptance_commands": ["python -m unittest"],
        "verification_level": "L2",
        "human_gates": ["EXECUTION_APPROVAL"],
    }


def construction_plan(*, task=None, scope="tests/fixtures/paired-cases.json", envelope=None, owner_role="luna_construction"):
    selected_task = remediation_task() if task is None else task
    return {
        "schema_version": "ai-plan-1",
        "plan_id": "plan-20260808-construction-601",
        "task_id": selected_task["task_id"],
        "goal": "implement one isolated parser behavior",
        "done_when": ["the approved construction behavior has been verified"],
        "tasks": [
            {
                "id": "construction-601",
                "owner_role": owner_role,
                "read_scope": [scope],
                "write_scope": [scope],
                "do_not_touch": [],
                "depends_on": [],
                "expected_result": "the parser behavior has one deterministic result",
                "verification_commands": ["python -m unittest tests.test_parser"],
                "first_artifact": scope,
                "evidence_level": "L2",
                **(
                    {
                        "construction_envelope": valid_envelope(scope)
                        if envelope is None
                        else envelope,
                    }
                    if owner_role == "luna_construction"
                    else {}
                ),
            }
        ],
        "stages": [["construction-601"]],
    }


def valid_envelope(scope):
    artifact_path = ROOT / scope
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest() if artifact_path.is_file() else "a" * 64
    return {
        "allowed_paths": [scope],
        "done_when": {
            "kind": "TEST",
            "command": "/usr/bin/grep -F case-01 tests/fixtures/paired-cases.json",
            "expected_exit": 0,
            "assertion": "case-01",
            "artifact": scope,
        },
        "evidence": {
            "L0": {"kind": "HASH", "artifact": scope, "sha256": digest},
            "L1": {
                "kind": "COMMAND",
                "command": "/usr/bin/grep -F case-01 tests/fixtures/paired-cases.json",
                "expected_exit": 0,
                "assertion": "case-01",
                "artifact": scope,
            },
            "L2": {
                "kind": "TEST",
                "command": "/usr/bin/grep -F case-01 tests/fixtures/paired-cases.json",
                "expected_exit": 0,
                "assertion": "case-01",
                "artifact": scope,
            },
        },
        "negative_checks": [
            {
                "kind": "COMMAND",
                "command": "/usr/bin/grep -F definitely-absent tests/fixtures/paired-cases.json",
                "expected_exit": 1,
                "assertion": "exit=1",
                "artifact": scope,
            }
        ],
        "risk_classification": {
            "kind": "LOCAL_DETERMINISTIC_IMPLEMENTATION",
            "security": False,
            "authorization": False,
            "protocol": False,
            "control_plane": False,
        },
    }


def route_request(task):
    return {
        "schema_version": "ai-route-request-1",
        "task_id": task["task_id"],
        "work_class": "BOUNDED",
        "execution_need": "WRITE",
        "decomposable": True,
        "risk_flags": [],
        "reason_codes": ["PLAN_IS_DELIVERABLE"],
    }


class BoundConstructionRunner:
    is_live_model = False

    def __init__(self):
        self.calls = []

    def run_construction(self, role, task, context, *, attempt_context=None):
        self.calls.append((role, context.dispatch_id, context.step.id))
        return workflow.FakeRunner().run_construction(
            role, task, context, attempt_context=attempt_context
        )


class ClarifyingConstructionRunner(BoundConstructionRunner):
    def run_construction(self, role, task, context, *, attempt_context=None):
        result = super().run_construction(
            role, task, context, attempt_context=attempt_context
        )
        result["status"] = "NEEDS_CLARIFICATION"
        result["recommended_next_state"] = "NEEDS_REPLAN"
        return result


class EnforcedConstructionExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.root)
        self.task = remediation_task()
        self.plan = construction_plan(task=self.task)
        self.request = route_request(self.task)
        self.task_path = self.store.create_task(self.task)
        decision = workflow.decide_route(
            self.task,
            self.request,
            "enforced",
            construction_plan=self.plan,
            construction_step_id="construction-601",
        )
        workflow.record_route_decision(self.store, self.task["task_id"], decision)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_enforced_state_machine_launches_the_hash_bound_luna_owner(self):
        runner = BoundConstructionRunner()

        waiting = workflow.run_until_gate(
            self.task["task_id"],
            construction_plan=self.plan,
            construction_request=self.request,
            construction_step_id="construction-601",
            construction_attempt=1,
            runner=runner,
            allow_live_model=False,
            state_root=self.root,
        )
        self.assertEqual("AWAITING_OWNER_DECISION", waiting)
        workflow._apply_owner_decision(self.store, self.task["task_id"], "approve_execution", "owner")

        state = workflow.run_until_gate(
            self.task["task_id"],
            construction_plan=self.plan,
            construction_request=self.request,
            construction_step_id="construction-601",
            construction_attempt=1,
            runner=runner,
            allow_live_model=False,
            state_root=self.root,
        )

        self.assertEqual("IMPLEMENTED_CANDIDATE", state)
        self.assertEqual(1, len(runner.calls))
        self.assertEqual("luna_construction", runner.calls[0][0])
        dispatch = json.loads(
            (self.root / self.task["task_id"] / "dispatches.jsonl").read_text()
        )
        self.assertEqual("construction-601", dispatch["subtask_id"])
        self.assertEqual(workflow.validate_plan(self.plan, self.task).plan_sha256, dispatch["plan_sha256"])
        events = [json.loads(line) for line in (
            self.root / self.task["task_id"] / "events.jsonl"
        ).read_text().splitlines()]
        evidence_event = next(
            event for event in events
            if event["event_type"] == "CONSTRUCTION_EVIDENCE_RECORDED"
        )
        self.assertEqual("controller", json.loads(evidence_event["evidence"][0]["observation"])["source"])
        self.assertEqual(0, json.loads(evidence_event["evidence"][1]["observation"])["exit_code"])

    def test_non_luna_frozen_step_runs_terra_xhigh_without_any_sol_role(self):
        task = remediation_task()
        task["task_id"] = "AWF-20260808-602"
        plan = construction_plan(task=task, owner_role="terra_xhigh")
        request = route_request(task)
        store = workflow.WorkflowStore(self.root)
        store.create_task(task)
        decision = workflow.decide_route(
            task,
            request,
            "enforced",
            construction_plan=plan,
            construction_step_id="construction-601",
        )
        workflow.record_route_decision(store, task["task_id"], decision)
        runner = BoundConstructionRunner()

        self.assertEqual(
            "AWAITING_OWNER_DECISION",
            workflow.run_until_gate(
                task["task_id"],
                runner=runner,
                allow_live_model=False,
                construction_plan=plan,
                construction_request=request,
                construction_step_id="construction-601",
                construction_attempt=1,
                state_root=self.root,
            ),
        )
        workflow._apply_owner_decision(store, task["task_id"], "approve_execution", "owner")
        self.assertEqual(
            "IMPLEMENTED_CANDIDATE",
            workflow.run_until_gate(
                task["task_id"],
                runner=runner,
                allow_live_model=False,
                construction_plan=plan,
                construction_request=request,
                construction_step_id="construction-601",
                construction_attempt=1,
                state_root=self.root,
            ),
        )
        self.assertEqual(["terra_xhigh"], [call[0] for call in runner.calls])

    def test_owner_approval_cannot_switch_to_a_different_plan_after_freeze(self):
        runner = BoundConstructionRunner()
        self.assertEqual(
            "AWAITING_OWNER_DECISION",
            workflow.run_until_gate(
                self.task["task_id"],
                runner=runner,
                allow_live_model=False,
                construction_plan=self.plan,
                construction_request=self.request,
                construction_step_id="construction-601",
                construction_attempt=1,
                state_root=self.root,
            ),
        )
        changed_plan = construction_plan(task=self.task)
        changed_plan["tasks"][0]["expected_result"] = "a different parser result"

        with self.assertRaisesRegex(workflow.WorkflowError, "CONSTRUCTION_PLAN_MISMATCH"):
            workflow.run_until_gate(
                self.task["task_id"],
                runner=runner,
                allow_live_model=False,
                construction_plan=changed_plan,
                construction_request=self.request,
                construction_step_id="construction-601",
                construction_attempt=1,
                state_root=self.root,
            )

    def test_cli_construction_path_uses_the_same_enforced_executor(self):
        output = StringIO()
        plan_path = Path(self.temporary_directory.name) / "plan.json"
        request_path = Path(self.temporary_directory.name) / "request.json"
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        request_path.write_text(json.dumps(self.request), encoding="utf-8")

        with redirect_stdout(output):
            exit_code = workflow.main(
                [
                    "run",
                    "--task",
                    str(self.task_path),
                    "--runner",
                    "fake",
                    "--construction-plan",
                    str(plan_path),
                    "--construction-request",
                    str(request_path),
                    "--construction-step",
                    "construction-601",
                    "--attempt",
                    "1",
                    "--root",
                    str(self.root),
                ]
            )

        self.assertEqual(0, exit_code)
        self.assertIn("AWAITING_OWNER_DECISION", output.getvalue())

    def test_resume_reuses_the_frozen_construction_context_without_redispatch(self):
        plan_path = Path(self.temporary_directory.name) / "plan.json"
        request_path = Path(self.temporary_directory.name) / "request.json"
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        request_path.write_text(json.dumps(self.request), encoding="utf-8")
        initial_output = StringIO()
        with redirect_stdout(initial_output):
            self.assertEqual(
                0,
                workflow.main(
                    [
                        "run",
                        "--task",
                        str(self.task_path),
                        "--runner",
                        "fake",
                        "--construction-plan",
                        str(plan_path),
                        "--construction-request",
                        str(request_path),
                        "--construction-step",
                        "construction-601",
                        "--attempt",
                        "1",
                        "--root",
                        str(self.root),
                    ]
                ),
            )
        self.assertEqual("AWAITING_OWNER_DECISION\n", initial_output.getvalue())
        store = workflow.WorkflowStore(self.root)
        workflow._apply_owner_decision(
            store, self.task["task_id"], "approve_execution", "owner"
        )

        for _ in range(2):
            output = StringIO()
            with redirect_stdout(output):
                exit_code = workflow.main(
                    [
                        "resume",
                        self.task["task_id"],
                        "--runner",
                        "fake",
                        "--root",
                        str(self.root),
                    ]
                )
            self.assertEqual(0, exit_code)
            self.assertEqual("IMPLEMENTED_CANDIDATE\n", output.getvalue())

        self.assertTrue(
            (
                self.root
                / self.task["task_id"]
                / "construction-resume.json"
            ).is_file()
        )
        events = [
            json.loads(line)
            for line in (
                self.root / self.task["task_id"] / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            1,
            sum(
                event.get("event_type") == "ROLE_RESULT"
                and event.get("role") == "luna_construction"
                for event in events
            ),
        )

    def test_decide_resume_advances_attempt_before_rework_dispatch(self):
        runner = ClarifyingConstructionRunner()
        self.assertEqual(
            "AWAITING_OWNER_DECISION",
            workflow.run_until_gate(
                self.task["task_id"],
                runner=runner,
                allow_live_model=False,
                construction_plan=self.plan,
                construction_request=self.request,
                construction_step_id="construction-601",
                construction_attempt=1,
                state_root=self.root,
            ),
        )
        workflow._apply_owner_decision(
            self.store, self.task["task_id"], "approve_execution", "owner"
        )
        self.assertEqual(
            "AWAITING_OWNER_DECISION",
            workflow.run_until_gate(
                self.task["task_id"],
                runner=runner,
                allow_live_model=False,
                construction_plan=self.plan,
                construction_request=self.request,
                construction_step_id="construction-601",
                construction_attempt=1,
                state_root=self.root,
            ),
        )

        output = StringIO()
        with redirect_stdout(output):
            exit_code = workflow.main(
                [
                    "decide",
                    self.task["task_id"],
                    "authorize_rework",
                    "--resume",
                    "--runner",
                    "fake",
                    "--root",
                    str(self.root),
                ]
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(
            "DECISION_RECORDED\nIMPLEMENTED_CANDIDATE\n",
            output.getvalue(),
        )
        context = json.loads(
            (
                self.root
                / self.task["task_id"]
                / "construction-resume.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(2, context["attempt"])
        dispatches = [
            json.loads(line)
            for line in (
                self.root / self.task["task_id"] / "dispatches.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([1, 2], [record["attempt"] for record in dispatches])

    def test_duplicate_rework_dispatch_does_not_leave_running_state(self):
        runner = ClarifyingConstructionRunner()
        self.assertEqual(
            "AWAITING_OWNER_DECISION",
            workflow.run_until_gate(
                self.task["task_id"],
                runner=runner,
                allow_live_model=False,
                construction_plan=self.plan,
                construction_request=self.request,
                construction_step_id="construction-601",
                construction_attempt=1,
                state_root=self.root,
            ),
        )
        workflow._apply_owner_decision(
            self.store, self.task["task_id"], "approve_execution", "owner"
        )
        self.assertEqual(
            "AWAITING_OWNER_DECISION",
            workflow.run_until_gate(
                self.task["task_id"],
                runner=runner,
                allow_live_model=False,
                construction_plan=self.plan,
                construction_request=self.request,
                construction_step_id="construction-601",
                construction_attempt=1,
                state_root=self.root,
            ),
        )
        workflow._apply_owner_decision(
            self.store, self.task["task_id"], "authorize_rework", "owner"
        )

        with self.assertRaisesRegex(
            workflow.WorkflowError, "CONSTRUCTION_CONTEXT_MISMATCH"
        ):
            workflow.run_until_gate(
                self.task["task_id"],
                runner=workflow.FakeRunner(),
                allow_live_model=False,
                construction_plan=self.plan,
                construction_request=self.request,
                construction_step_id="construction-601",
                construction_attempt=1,
                state_root=self.root,
            )

        self.assertEqual(
            "REWORK_AUTHORIZED",
            workflow._current_state(self.store, self.task["task_id"]),
        )

    def test_orphan_dispatch_after_append_is_resumed_on_the_same_identity(self):
        runner = BoundConstructionRunner()
        kwargs = {
            "task_id": self.task["task_id"],
            "construction_plan": self.plan,
            "request": self.request,
            "step_id": "construction-601",
            "attempt": 1,
            "runner": runner,
            "allow_live_model": False,
            "state_root": self.root,
        }
        self.assertEqual("AWAITING_OWNER_DECISION", workflow.run_enforced_construction(**kwargs))
        workflow._apply_owner_decision(
            self.store, self.task["task_id"], "approve_execution", "owner"
        )
        real_transition = workflow._transition

        def crash_after_dispatch(store, task_id, current, target, budget, **extra):
            if current == "WORKTREE_READY" and target == "IMPLEMENTATION_RUNNING":
                raise workflow.WorkflowError("INJECTED_CRASH", "after dispatch append")
            return real_transition(store, task_id, current, target, budget, **extra)

        with mock.patch.object(workflow, "_transition", side_effect=crash_after_dispatch):
            with self.assertRaisesRegex(workflow.WorkflowError, "INJECTED_CRASH"):
                workflow.run_enforced_construction(**kwargs)
        dispatch_path = self.root / self.task["task_id"] / "dispatches.jsonl"
        original = dispatch_path.read_bytes()

        self.assertEqual("IMPLEMENTED_CANDIDATE", workflow.run_enforced_construction(**kwargs))
        self.assertEqual(original, dispatch_path.read_bytes())
        self.assertEqual(1, len(runner.calls))

    def test_orphan_dispatch_identity_drift_is_rejected_before_running(self):
        runner = BoundConstructionRunner()
        kwargs = {
            "task_id": self.task["task_id"],
            "construction_plan": self.plan,
            "request": self.request,
            "step_id": "construction-601",
            "attempt": 1,
            "runner": runner,
            "allow_live_model": False,
            "state_root": self.root,
        }
        workflow.run_enforced_construction(**kwargs)
        workflow._apply_owner_decision(
            self.store, self.task["task_id"], "approve_execution", "owner"
        )
        real_transition = workflow._transition
        with mock.patch.object(
            workflow,
            "_transition",
            side_effect=lambda store, task_id, current, target, budget, **extra: (
                (_ for _ in ()).throw(workflow.WorkflowError("INJECTED_CRASH", "after append"))
                if current == "WORKTREE_READY" and target == "IMPLEMENTATION_RUNNING"
                else real_transition(store, task_id, current, target, budget, **extra)
            ),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "INJECTED_CRASH"):
                workflow.run_enforced_construction(**kwargs)
        path = self.root / self.task["task_id"] / "dispatches.jsonl"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["request_sha256"] = "0" * 64
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(workflow.WorkflowError, "ORPHAN_DISPATCH_MISMATCH"):
            workflow.run_enforced_construction(**kwargs)
        self.assertEqual([], runner.calls)

    def test_rework_attempt_two_orphan_resumes_without_incrementing_to_three(self):
        runner = ClarifyingConstructionRunner()
        kwargs = {
            "task_id": self.task["task_id"],
            "construction_plan": self.plan,
            "request": self.request,
            "step_id": "construction-601",
            "attempt": 1,
            "runner": runner,
            "allow_live_model": False,
            "state_root": self.root,
        }
        workflow.run_enforced_construction(**kwargs)
        workflow._apply_owner_decision(
            self.store, self.task["task_id"], "approve_execution", "owner"
        )
        self.assertEqual("AWAITING_OWNER_DECISION", workflow.run_enforced_construction(**kwargs))
        workflow._apply_owner_decision(
            self.store, self.task["task_id"], "authorize_rework", "owner"
        )
        kwargs["attempt"] = 2
        real_transition = workflow._transition
        with mock.patch.object(
            workflow,
            "_transition",
            side_effect=lambda store, task_id, current, target, budget, **extra: (
                (_ for _ in ()).throw(workflow.WorkflowError("INJECTED_CRASH", "after append"))
                if current == "REWORK_AUTHORIZED" and target == "IMPLEMENTATION_RUNNING"
                else real_transition(store, task_id, current, target, budget, **extra)
            ),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "INJECTED_CRASH"):
                workflow.run_enforced_construction(**kwargs)
        class Args:
            runner = "fake"
            allow_live_model = False
            runtime_sessions_dir = None

        _, context = workflow._prepare_resume(
            self.store, self.task["task_id"], Args()
        )
        self.assertIsNotNone(context)
        self.assertEqual(2, context[3])

    def test_separate_second_rework_decision_advances_attempt_two_to_three(self):
        config = json.loads(json.dumps(workflow._load_workflow_config()))
        config["policy"]["max_implementation_reworks"] = 2
        runner = ClarifyingConstructionRunner()
        kwargs = {
            "task_id": self.task["task_id"],
            "construction_plan": self.plan,
            "request": self.request,
            "step_id": "construction-601",
            "attempt": 1,
            "runner": runner,
            "allow_live_model": False,
            "state_root": self.root,
        }

        with mock.patch.object(workflow, "_load_workflow_config", return_value=config):
            workflow.run_enforced_construction(**kwargs)
            workflow._apply_owner_decision(
                self.store, self.task["task_id"], "approve_execution", "owner"
            )
            workflow.run_enforced_construction(**kwargs)
            workflow._apply_owner_decision(
                self.store, self.task["task_id"], "authorize_rework", "owner"
            )
            kwargs["attempt"] = 2
            workflow.run_enforced_construction(**kwargs)
            workflow._apply_owner_decision(
                self.store, self.task["task_id"], "authorize_rework", "owner"
            )

            class Args:
                runner = "fake"
                allow_live_model = False
                runtime_sessions_dir = None

            _, context = workflow._prepare_resume(
                self.store, self.task["task_id"], Args()
            )

        self.assertIsNotNone(context)
        self.assertEqual(3, context[3])

    def test_generic_terra_os_state_machine_and_cli_fail_closed_without_context(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "CONSTRUCTION_CONTEXT_REQUIRED"):
            with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.root):
                workflow.run_until_gate(
                    self.task["task_id"], runner=workflow.FakeRunner(), allow_live_model=False
                )

        output = StringIO()
        with redirect_stdout(output):
            exit_code = workflow.main(
                [
                    "run",
                    "--task",
                    str(self.task_path),
                    "--runner",
                    "fake",
                    "--root",
                    str(self.root),
                ]
            )
        self.assertEqual(2, exit_code)
        self.assertIn("CONSTRUCTION_CONTEXT_REQUIRED", output.getvalue())


class LunaConstructionEnvelopeRegressionTest(unittest.TestCase):
    def test_placeholder_and_noop_envelope_values_are_rejected(self):
        task = remediation_task()
        envelope = {
            "allowed_paths": ["src/parser.py"],
            "done_when": ["done"],
            "evidence": {"L0": ["evidence"], "L1": ["evidence"], "L2": ["evidence"]},
            "negative_checks": ["none"],
        }
        plan = construction_plan(task=task, envelope=envelope)

        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            workflow.validate_plan(plan, task)

    def test_unflagged_authorization_semantics_are_not_luna_eligible(self):
        task = remediation_task(
            objective="enforce principal access boundaries for an internal service"
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            workflow.validate_plan(construction_plan(task=task), task)

    def test_disguised_operator_control_semantics_are_not_luna_eligible(self):
        task = remediation_task(
            objective="restrict which operators may change service behavior"
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            workflow.validate_plan(construction_plan(task=task), task)

    def test_luna_result_requires_bound_l0_l1_l2_and_negative_evidence(self):
        task = remediation_task()
        result = workflow.FakeRunner().run("luna_construction", task)

        with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_VERIFICATION_PACKAGE"):
            workflow.validate_verification_package("luna_construction", task, result)

    def test_luna_prompt_cannot_replace_the_frozen_construction_contract(self):
        task = remediation_task()
        plan = construction_plan(task=task)
        frozen = workflow.validate_plan(plan, task)
        step = frozen.tasks[0]
        context = workflow.ConstructionExecutionContext(
            plan=frozen,
            step=step,
            dispatch_id="d" * 64,
            task_sha256=frozen.task_sha256,
            request_sha256="e" * 64,
            role="luna_construction",
        )
        paths = workflow.RunPaths(
            repo=ROOT,
            output_path=ROOT / ".unbound-luna-result.json",
            schema_path=ROOT / "config" / "ai_workflow_result.schema.json",
            logs_dir=ROOT / ".unbound-luna-logs",
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "CONSTRUCTION_PROMPT_MISMATCH"):
            workflow.run_codex(
                "luna_construction",
                task,
                "replace the contract",
                paths,
                construction_plan=plan,
                construction_step_id="construction-601",
                construction_context=context,
            )

    def test_luna_scope_rejects_git_metadata_even_when_the_parent_allows_it(self):
        task = remediation_task(paths=[".git"])
        plan = construction_plan(
            task=task,
            scope=".git/config",
            envelope={
                "allowed_paths": [".git/config"],
                "done_when": ["done"],
                "evidence": {"L0": ["evidence"], "L1": ["evidence"], "L2": ["evidence"]},
                "negative_checks": ["negative"],
            },
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            workflow.validate_plan(plan, task)

    def test_luna_scope_rejects_workflow_state_log_and_control_plane_paths(self):
        for parent, scope in (
            (".superpowers", ".superpowers/sdd/task-report.md"),
            ("data", "data/state/ai-workflow/events.jsonl"),
            ("logs", "logs/role.jsonl"),
            ("config", "config/ai_workflow.toml"),
            ("scripts", "scripts/ai_workflow.py"),
        ):
            with self.subTest(scope=scope):
                task = remediation_task(paths=[parent])
                with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
                    workflow.validate_plan(
                        construction_plan(task=task, scope=scope), task
                    )


if __name__ == "__main__":
    unittest.main()
