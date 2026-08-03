import json
import tempfile
import unittest
from pathlib import Path

from scripts import ai_workflow as workflow


ROOT = Path(__file__).resolve().parents[1]


class NewArtifactSchemaTest(unittest.TestCase):
    EXPECTED = {
        "ai_workflow_route_request.schema.json": "ai-route-request-1",
        "ai_workflow_route_decision.schema.json": "ai-route-decision-1",
        "ai_workflow_plan.schema.json": "ai-plan-1",
        "ai_workflow_runtime_evidence.schema.json": "runtime-evidence-1",
        "ai_workflow_cost_evidence.schema.json": "cost-evidence-1",
    }

    def test_every_new_schema_is_strict_and_versioned(self):
        for filename, version in self.EXPECTED.items():
            schema = json.loads((ROOT / "config" / filename).read_text())
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(version, schema["properties"]["schema_version"]["const"])
            self.assertEqual(set(schema["properties"]), set(schema["required"]))


def valid_task(*, risk_flags=None):
    return {
        "schema_version": "ai-task-1",
        "task_id": "AWF-20260803-001",
        "task_type": "PLAN",
        "objective": "validate workflow",
        "repository_root": str(ROOT),
        "source_worktree": None,
        "base_commit": None,
        "candidate_commit": None,
        "authoritative_files": ["README.md"],
        "allowed_write_paths": [],
        "forbidden_actions": ["merge"],
        "risk_flags": [] if risk_flags is None else risk_flags,
        "acceptance_commands": ["python -m unittest"],
        "verification_level": "L1",
        "human_gates": ["PLAN_APPROVAL"],
    }


def valid_route_request(*, risk_flags=None):
    task = valid_task(risk_flags=risk_flags)
    return {
        "schema_version": "ai-route-request-1",
        "task_id": task["task_id"],
        "work_class": "BOUNDED",
        "execution_need": "READ_ONLY",
        "decomposable": True,
        "risk_flags": list(task["risk_flags"]),
        "reason_codes": ["BOUNDED_TASK"],
    }


class ArtifactValidatorTest(unittest.TestCase):
    def test_route_request_must_match_task_risk_flags(self):
        request = valid_route_request(risk_flags=[])
        task = valid_task(risk_flags=["SECURITY"])
        with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_CONFLICT"):
            workflow.validate_route_request(request, task)

    def test_unknown_artifact_field_is_rejected(self):
        request = valid_route_request()
        request["surprise"] = True
        with self.assertRaisesRegex(workflow.WorkflowError, "UNKNOWN_FIELD"):
            workflow.validate_route_request(request, valid_task())

    def test_artifact_hash_is_canonical(self):
        left = {"b": 2, "a": "中文"}
        right = {"a": "中文", "b": 2}
        self.assertEqual(workflow.artifact_sha256(left), workflow.artifact_sha256(right))

    def test_load_artifact_requires_json_object(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "artifact.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(workflow.ArtifactError):
                workflow.load_artifact(path)


if __name__ == "__main__":
    unittest.main()
