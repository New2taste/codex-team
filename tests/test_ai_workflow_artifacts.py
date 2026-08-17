import json
import tempfile
import unittest
from pathlib import Path

from scripts import ai_workflow as workflow


ROOT = Path(__file__).resolve().parents[1]
LEGACY_MODEL_ROLES = ("luna", "terra", "sol_planner", "sol_reviewer", "sol_xhigh")
TERRA_OS_MODEL_ROLES = (
    "luna_construction",
    "sol_medium_supervisor",
    "terra_xhigh",
    "terra_xhigh_planner",
    "terra_xhigh_reviewer",
    "sol_medium_reviewer",
    "sol_xhigh_planner",
)
ALL_MODEL_ROLES = LEGACY_MODEL_ROLES + TERRA_OS_MODEL_ROLES


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
        "reason_codes": ["PLAN_IS_DELIVERABLE"],
    }


def valid_cost_evidence(**overrides):
    value = {
        "schema_version": "cost-evidence-1",
        "route": "delegated",
        "role": "luna",
        "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
        "duration_seconds": 1.0,
        "prompt_bytes": 4,
        "input_tokens": 10,
        "cached_input_tokens": 0,
        "output_tokens": 5,
        "retry_kind": "none",
        "verification_seconds": 0.1,
        "quality_outcome": "SUPPORTED",
        "paired_case_id": "case-1",
        "evidence_class": "measured",
        "rate_snapshot_id": None,
    }
    value.update(overrides)
    return value


def valid_runtime_evidence(**overrides):
    value = {
        "schema_version": "runtime-evidence-1",
        "attempt_id": "runtime-attempt-1",
        "requested_role": "luna",
        "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
        "observed_agent_type": None,
        "native_agent_id": None,
        "native_thread_id": None,
        "observed_model": "gpt-5.6-luna",
        "observed_reasoning_effort": "max",
        "observed_sandbox_policy": "read-only",
        "observed_permission_profile": "read-only",
        "observed_cwd": str(ROOT),
        "evidence_source": "LOCAL_ROLLOUT",
        "observed_at_utc": "2026-08-08T00:00:00Z",
        "verification_status": "VERIFIED",
        "failure_reasons": [],
    }
    value.update(overrides)
    return value


def valid_plan_shape(owner_role):
    return {
        "schema_version": "ai-plan-1",
        "plan_id": "plan-1",
        "task_id": "AWF-20260803-001",
        "goal": "validate the role schema",
        "done_when": ["role accepted"],
        "tasks": [
            {
                "id": "task-1",
                "owner_role": owner_role,
                "read_scope": ["README.md"],
                "write_scope": [],
                "do_not_touch": [],
                "depends_on": [],
                "expected_result": "validated role",
                "verification_commands": ["python -m unittest"],
                "first_artifact": "result.json",
                "evidence_level": "L1",
            }
        ],
        "stages": [["task-1"]],
    }


def valid_role_result(role, status):
    return {
        "schema_version": "ai-result-1",
        "role": role,
        "status": status,
        "summary": "validate a pinned role",
        "claims": [],
        "evidence": [],
        "counter_checks": [],
        "changed_files": [],
        "blind_spots": [],
        "unresolved_questions": [],
        "recommended_next_state": "AWAITING_OWNER_DECISION",
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

    def test_reason_codes_reject_unknown_values(self):
        request = valid_route_request()
        request["reason_codes"] = ["BOUNDED_TASK"]
        with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_ENUM"):
            workflow.validate_route_request(request, valid_task())

    def test_cost_evidence_requires_execution_surface(self):
        workflow.validate_cost_evidence(valid_cost_evidence())

    def test_cost_execution_surface_is_a_closed_set(self):
        for surface in ("NATIVE_SUBAGENT", "CODEX_EXEC_ROLE_CONTRACT"):
            workflow.validate_cost_evidence(valid_cost_evidence(execution_surface=surface))
        with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_ENUM"):
            workflow.validate_cost_evidence(valid_cost_evidence(execution_surface="OTHER"))

    def test_measured_cost_requires_token_evidence(self):
        value = valid_cost_evidence(
            input_tokens=None,
            cached_input_tokens=None,
            output_tokens=None,
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "COST_EVIDENCE_INVALID"):
            workflow.validate_cost_evidence(value)

    def test_runtime_schema_freezes_observed_agent_type_conditions(self):
        schema = json.loads(
            (ROOT / "config" / "ai_workflow_runtime_evidence.schema.json").read_text()
        )
        conditions = schema["allOf"]
        native = next(item for item in conditions if item["if"]["properties"]["execution_surface"]["const"] == "NATIVE_SUBAGENT")
        codex = next(item for item in conditions if item["if"]["properties"]["execution_surface"]["const"] == "CODEX_EXEC_ROLE_CONTRACT")
        self.assertEqual(native["then"]["properties"]["observed_agent_type"], {"type": "null"})
        self.assertEqual(native["then"]["properties"]["native_agent_id"]["format"], "uuid")
        self.assertEqual(native["then"]["properties"]["native_thread_id"]["format"], "uuid")
        self.assertEqual(codex["then"]["properties"]["observed_agent_type"], {"type": "null"})
        self.assertEqual(codex["then"]["properties"]["native_agent_id"], {"type": "null"})
        self.assertEqual(codex["then"]["properties"]["native_thread_id"], {"type": "null"})

    def test_native_runtime_schema_requires_null_agent_type(self):
        schema = json.loads(
            (ROOT / "config" / "ai_workflow_runtime_evidence.schema.json").read_text()
        )
        native = next(
            item for item in schema["allOf"]
            if item["if"]["properties"]["execution_surface"]["const"] == "NATIVE_SUBAGENT"
        )["then"]["properties"]["observed_agent_type"]
        self.assertEqual("null", native["type"])

    def test_route_decision_has_no_unvalidated_runtime_extensions(self):
        fields = set(workflow.RouteDecision.__dataclass_fields__)
        self.assertTrue(fields.isdisjoint({"roles", "shadow_route", "effective_roles"}))

    def test_new_and_legacy_model_roles_validate_in_every_frozen_artifact(self):
        schemas = {
            "result": json.loads((ROOT / "config" / "ai_workflow_result.schema.json").read_text()),
            "runtime": json.loads(
                (ROOT / "config" / "ai_workflow_runtime_evidence.schema.json").read_text()
            ),
            "cost": json.loads(
                (ROOT / "config" / "ai_workflow_cost_evidence.schema.json").read_text()
            ),
            "plan": json.loads((ROOT / "config" / "ai_workflow_plan.schema.json").read_text()),
        }
        result_statuses = {
            "luna": "SUPPORTED",
            "luna_construction": "IMPLEMENTED_CANDIDATE",
            "terra": "IMPLEMENTED_CANDIDATE",
            "sol_planner": "PLAN_READY",
            "sol_reviewer": "ACCEPTANCE_RECOMMENDED",
            "sol_xhigh": "OPTION_A",
            "sol_medium_supervisor": "PLAN_READY",
            "terra_xhigh": "IMPLEMENTED_CANDIDATE",
            "terra_xhigh_planner": "PLAN_READY",
            "terra_xhigh_reviewer": "ACCEPTANCE_RECOMMENDED",
            "sol_medium_reviewer": "ACCEPTANCE_RECOMMENDED",
            "sol_xhigh_planner": "OPTION_A",
        }
        schema_role_enums = (
            schemas["result"]["properties"]["role"]["enum"],
            schemas["runtime"]["properties"]["requested_role"]["enum"],
            schemas["cost"]["properties"]["role"]["enum"],
            schemas["plan"]["properties"]["tasks"]["items"]["properties"]["owner_role"]["enum"],
        )

        for role in ALL_MODEL_ROLES:
            with self.subTest(role=role):
                for enum in schema_role_enums:
                    self.assertIn(role, enum)
                workflow.validate_role_result(
                    role, valid_role_result(role, result_statuses[role]), set()
                )
                workflow.validate_runtime_evidence(valid_runtime_evidence(requested_role=role))
                workflow.validate_cost_evidence(valid_cost_evidence(role=role))
                workflow.validate_plan_shape(valid_plan_shape(role))


if __name__ == "__main__":
    unittest.main()
