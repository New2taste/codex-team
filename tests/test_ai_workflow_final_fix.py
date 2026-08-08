import json
import tempfile
import unittest
from pathlib import Path

from scripts import ai_workflow as workflow
from tests.test_ai_workflow_construction_execution import (
    construction_plan,
    remediation_task,
    valid_envelope,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = "tests/fixtures/paired-cases.json"


class FinalFixEvidenceContractTest(unittest.TestCase):
    def _validate_l1_command(self, command: str, *, artifact: str = FIXTURE):
        task = remediation_task(paths=["tests/fixtures"])
        envelope = valid_envelope(artifact)
        envelope["evidence"]["L1"]["command"] = command
        return workflow.validate_plan(
            construction_plan(task=task, scope=artifact, envelope=envelope), task
        )

    def test_plan_rejects_a_cross_scope_evidence_operand(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            self._validate_l1_command("/usr/bin/grep -F root /etc/passwd")

    def test_plan_rejects_an_external_same_name_executable(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            self._validate_l1_command(
                "/tmp/grep -F case-01 tests/fixtures/paired-cases.json"
            )

    def test_plan_rejects_an_artifact_argv_mismatch(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            self._validate_l1_command(
                "/usr/bin/grep -F case-01 tests/fixtures/paired-cases.json",
                artifact="tests/fixtures/not-the-command-operand.json",
            )

    def test_executor_revalidates_a_forged_cross_scope_check_before_launch(self):
        check = workflow.ConstructionCheck(
            kind="COMMAND",
            artifact=FIXTURE,
            command="/usr/bin/grep -F root /etc/passwd",
            expected_exit=0,
            assertion="root",
        )

        with self.assertRaisesRegex(
            workflow.WorkflowError, "CONSTRUCTION_EVIDENCE_FAILED"
        ):
            workflow._execute_construction_command(ROOT, check)

    def test_authorized_artifact_bound_test_executes_normally(self):
        task = remediation_task(paths=["tests/fixtures"])
        frozen = workflow.validate_plan(
            construction_plan(task=task, scope=FIXTURE), task
        )
        l2 = dict(frozen.tasks[0].construction_envelope.evidence)["L2"]

        observation = workflow._execute_construction_command(ROOT, l2)

        self.assertEqual(0, observation["exit_code"])
        self.assertEqual(
            ["/usr/bin/grep", "-F", "case-01", FIXTURE], observation["argv"]
        )


def read_only_task(task_type: str, index: int) -> dict[str, object]:
    return {
        "schema_version": "ai-task-1",
        "task_id": f"AWF-20260808-9{index:02d}",
        "task_type": task_type,
        "objective": "produce the bounded read-only workflow result",
        "repository_root": str(ROOT),
        "source_worktree": None,
        "base_commit": "1" * 40 if task_type == "ACCEPTANCE" else None,
        "candidate_commit": "2" * 40 if task_type == "ACCEPTANCE" else None,
        "authoritative_files": ["README.md"],
        "allowed_write_paths": [],
        "forbidden_actions": ["merge", "push"],
        "risk_flags": [],
        "acceptance_commands": ["python3 -m unittest"],
        "verification_level": "L1",
        "human_gates": [
            "FINAL_ACCEPTANCE" if task_type == "ACCEPTANCE" else "PLAN_APPROVAL"
        ],
    }


def read_only_request(task: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "ai-route-request-1",
        "task_id": task["task_id"],
        "work_class": "PLANNING_ONLY",
        "execution_need": "READ_ONLY",
        "decomposable": True,
        "risk_flags": [],
        "reason_codes": ["PLAN_IS_DELIVERABLE"],
    }


class ReadOnlyRouteRunner:
    is_live_model = False

    def __init__(self):
        self.calls: list[str] = []

    def run(self, role: str, task: dict[str, object]) -> dict[str, object]:
        self.calls.append(role)
        statuses = {
            "terra_xhigh_planner": ("PLAN_READY", "PLAN_READY"),
            "terra_xhigh_reviewer": (
                "ACCEPTANCE_RECOMMENDED",
                "REVIEW_READY",
            ),
        }
        status, next_state = statuses[role]
        return {
            "schema_version": "ai-result-1",
            "role": role,
            "status": status,
            "summary": f"Read-only {role} result for {task['task_id']}",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": next_state,
        }


class FinalFixReadOnlyRouteTest(unittest.TestCase):
    def test_plan_and_acceptance_routes_select_task_typed_read_only_terra_roles(self):
        for index, (task_type, expected_role) in enumerate(
            (
                ("PLAN", "terra_xhigh_planner"),
                ("ACCEPTANCE", "terra_xhigh_reviewer"),
            ),
            start=1,
        ):
            with self.subTest(task_type=task_type):
                task = read_only_task(task_type, index)
                decision = workflow.decide_route(
                    task, read_only_request(task), "enforced"
                )

                self.assertEqual("sol_only", decision.route)
                self.assertEqual((expected_role,), decision.roles)
                self.assertTrue(set(decision.roles).issubset(workflow.READ_ONLY_ROLES))

    def test_persisted_read_only_route_reaches_owner_gate_through_its_runner(self):
        for index, (task_type, expected_role) in enumerate(
            (
                ("PLAN", "terra_xhigh_planner"),
                ("ACCEPTANCE", "terra_xhigh_reviewer"),
            ),
            start=11,
        ):
            with self.subTest(task_type=task_type), tempfile.TemporaryDirectory() as temporary:
                task = read_only_task(task_type, index)
                request = read_only_request(task)
                store = workflow.WorkflowStore(Path(temporary) / "state")
                store.create_task(task)
                decision = workflow.decide_route(task, request, "enforced")
                workflow.record_route_decision(store, task["task_id"], decision)
                runner = ReadOnlyRouteRunner()

                state = workflow.run_until_gate(
                    task["task_id"],
                    runner=runner,
                    allow_live_model=False,
                    state_root=store.root,
                )

                self.assertEqual("AWAITING_OWNER_DECISION", state)
                self.assertEqual([expected_role], runner.calls)
                events = [
                    json.loads(line)
                    for line in (
                        store.root / task["task_id"] / "events.jsonl"
                    ).read_text(encoding="utf-8").splitlines()
                ]
                self.assertTrue(
                    any(
                        event.get("event_type") == "ROLE_RESULT"
                        and event.get("role") == expected_role
                        for event in events
                    )
                )
                self.assertEqual("OWNER_GATE_REACHED", events[-1]["event_type"])


if __name__ == "__main__":
    unittest.main()
