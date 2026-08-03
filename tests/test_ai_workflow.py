import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts import ai_workflow as workflow


ROOT = Path(__file__).resolve().parents[1]


class ContractFilesTest(unittest.TestCase):
    def test_role_models_and_efforts_are_pinned(self):
        with (ROOT / "config/ai_workflow.toml").open("rb") as handle:
            config = tomllib.load(handle)
        self.assertEqual(config["version"], "ai-workflow-1")
        self.assertEqual(
            (config["roles"]["luna"]["model"], config["roles"]["luna"]["reasoning_effort"]),
            ("gpt-5.6-luna", "max"),
        )
        self.assertEqual(
            config["roles"]["luna"]["allowed_statuses"],
            ["SUPPORTED", "PARTIALLY_SUPPORTED", "NOT_SUPPORTED", "BLOCKED"],
        )
        self.assertEqual(
            (config["roles"]["terra"]["model"], config["roles"]["terra"]["reasoning_effort"]),
            ("gpt-5.6-terra", "xhigh"),
        )
        self.assertFalse(config["policy"]["automatic_xhigh"])
        self.assertFalse(config["policy"]["automatic_merge"])
        self.assertFalse(config["policy"]["automatic_push"])

    def test_contract_versions_and_closed_sets_are_pinned(self):
        task_schema = json.loads((ROOT / "config/ai_workflow_task.schema.json").read_text())
        result_schema = json.loads((ROOT / "config/ai_workflow_result.schema.json").read_text())
        self.assertEqual(task_schema["properties"]["schema_version"]["const"], "ai-task-1")
        self.assertEqual(result_schema["properties"]["schema_version"]["const"], "ai-result-1")
        self.assertEqual(
            set(task_schema["properties"]["verification_level"]["enum"]),
            {"L0", "L1", "L2"},
        )


class TaskValidationTest(unittest.TestCase):
    def valid_task(self):
        return {
            "schema_version": "ai-task-1",
            "task_id": "AWF-20260803-001",
            "task_type": "PLAN",
            "objective": "Review the approved workflow specification",
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

    def test_valid_task_passes(self):
        workflow.validate_task(self.valid_task())

    def test_unknown_field_is_rejected(self):
        task = self.valid_task()
        task["surprise"] = True
        with self.assertRaisesRegex(workflow.WorkflowError, "UNKNOWN_FIELD"):
            workflow.validate_task(task)

    def test_acceptance_requires_both_commits(self):
        task = self.valid_task()
        task["task_type"] = "ACCEPTANCE"
        with self.assertRaisesRegex(workflow.WorkflowError, "COMMIT_REQUIRED"):
            workflow.validate_task(task)


class StateMachineTest(unittest.TestCase):
    def test_normal_evidence_transition(self):
        self.assertEqual(
            workflow.next_state("TASK_VALIDATED", "EVIDENCE_RUNNING", owner_authorized=False),
            "EVIDENCE_RUNNING",
        )

    def test_owner_gate_cannot_be_crossed_automatically(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "OWNER_AUTHORIZATION_REQUIRED"):
            workflow.next_state("AWAITING_OWNER_DECISION", "APPROVED_FOR_EXECUTION", owner_authorized=False)

    def test_closed_is_owner_only(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "OWNER_AUTHORIZATION_REQUIRED"):
            workflow.next_state("AWAITING_OWNER_DECISION", "CLOSED", owner_authorized=False)


class WorkflowStoreTest(unittest.TestCase):
    def test_create_task_writes_canonical_json(self):
        with tempfile.TemporaryDirectory() as temp:
            store = workflow.WorkflowStore(Path(temp))
            path = store.create_task(TaskValidationTest().valid_task())
            self.assertEqual(json.loads(path.read_text())["task_id"], "AWF-20260803-001")

    def test_decisions_are_appended_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temp:
            store = workflow.WorkflowStore(Path(temp))
            store.create_task(TaskValidationTest().valid_task())
            store.record_decision("AWF-20260803-001", {"decision": "approve", "by": "owner"})
            store.record_decision("AWF-20260803-001", {"decision": "close", "by": "owner"})
            lines = (Path(temp) / "AWF-20260803-001/human-decisions.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line)["decision"] for line in lines], ["approve", "close"])

    def test_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as temp:
            store = workflow.WorkflowStore(Path(temp))
            store.create_task(TaskValidationTest().valid_task())
            with store.lock("AWF-20260803-001"):
                with self.assertRaisesRegex(workflow.WorkflowError, "TASK_ALREADY_RUNNING"):
                    with store.lock("AWF-20260803-001"):
                        pass


class FakeRunnerTest(unittest.TestCase):
    def test_luna_fake_result_never_claims_acceptance(self):
        result = workflow.FakeRunner().run("luna", TaskValidationTest().valid_task())
        self.assertEqual(result["role"], "luna")
        self.assertEqual(result["status"], "SUPPORTED")
        self.assertNotIn("ACCEPTED", result["status"])


if __name__ == "__main__":
    unittest.main()
