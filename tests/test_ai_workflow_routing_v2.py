import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow


ROOT = Path(__file__).resolve().parents[1]


def valid_task(*, risk_flags=None, task_type="PLAN"):
    task = {
        "schema_version": "ai-task-1",
        "task_id": "AWF-20260803-001",
        "task_type": task_type,
        "objective": "route a bounded workflow task",
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
    if task_type == "ACCEPTANCE":
        task["base_commit"] = "1" * 40
        task["candidate_commit"] = "2" * 40
    return task


def route_request(work_class, execution_need, risk_flags=None, *, decomposable=True):
    return {
        "schema_version": "ai-route-request-1",
        "task_id": "AWF-20260803-001",
        "work_class": work_class,
        "execution_need": execution_need,
        "decomposable": decomposable,
        "risk_flags": [] if risk_flags is None else risk_flags,
        "reason_codes": ["PLAN_IS_DELIVERABLE"],
    }


class ClosedSetRoutingTest(unittest.TestCase):
    def test_simple_low_risk_work_routes_direct(self):
        """A direct-route regression must fail if SIMPLE work delegates workers."""

        decision = workflow.decide_route(
            valid_task(), route_request("SIMPLE", "WRITE"), "enforced"
        )

        self.assertEqual("direct", decision.route)
        self.assertEqual((), decision.roles)
        self.assertEqual((), decision.effective_roles)

    def test_planning_only_routes_sol_only_with_zero_workers(self):
        """Enforced Terra OS planning uses the medium Sol supervisor only."""

        decision = workflow.decide_route(
            valid_task(), route_request("PLANNING_ONLY", "READ_ONLY"), "enforced"
        )

        self.assertEqual("sol_only", decision.route)
        self.assertEqual(("sol_medium_supervisor",), decision.roles)

    def test_security_write_can_never_route_direct(self):
        """A risk-handling regression must fail if security writes become direct."""

        task = valid_task(risk_flags=["SECURITY"])
        decision = workflow.decide_route(
            task,
            route_request("SIMPLE", "WRITE", ["SECURITY"]),
            "enforced",
        )

        self.assertEqual("delegated", decision.route)

    def test_undecidable_request_fails_closed(self):
        """A high-consequence write without decomposition must never be dispatched."""

        with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_UNDECIDABLE"):
            workflow.decide_route(
                valid_task(),
                route_request("HIGH_CONSEQUENCE", "WRITE", decomposable=False),
                "enforced",
            )

    def test_non_decomposable_bounded_work_is_blocked(self):
        """A fallback regression must fail if non-decomposable bounded work delegates."""

        decision = workflow.decide_route(
            valid_task(), route_request("BOUNDED", "WRITE", decomposable=False), "enforced"
        )

        self.assertEqual("blocked", decision.route)
        self.assertEqual((), decision.roles)

    def test_unknown_mode_is_rejected(self):
        """Closed-set mode validation must reject a new unreviewed policy mode."""

        with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_INPUT_INVALID"):
            workflow.decide_route(valid_task(), route_request("SIMPLE", "NONE"), "trial")


class ShadowRoutingCompatibilityTest(unittest.TestCase):
    def test_shadow_records_decision_without_changing_legacy_roles(self):
        """Shadow routing must keep the legacy PLAN execution chain intact."""

        decision = workflow.decide_route(
            valid_task(), route_request("SIMPLE", "READ_ONLY"), "shadow"
        )

        self.assertEqual("direct", decision.route)
        self.assertEqual("direct", decision.shadow_route)
        self.assertEqual(("luna", "sol_planner"), decision.effective_roles)

    def test_legacy_mode_keeps_existing_role_chain(self):
        """Legacy mode must preserve the existing route(task) behavior exactly."""

        decision = workflow.decide_route(
            valid_task(task_type="ACCEPTANCE"),
            route_request("SIMPLE", "READ_ONLY"),
            "legacy",
        )

        self.assertEqual("delegated", decision.route)
        self.assertEqual(("luna", "sol_reviewer"), decision.roles)
        self.assertEqual(("luna", "sol_reviewer"), decision.effective_roles)
        self.assertIsNone(decision.shadow_route)

    def test_route_decision_binds_both_input_hashes(self):
        """A persisted routing decision must bind the exact task and request inputs."""

        task = valid_task()
        request = route_request("BOUNDED", "WRITE")
        decision = workflow.decide_route(task, request, "shadow")

        self.assertEqual(workflow.artifact_sha256(task), decision.task_sha256)
        self.assertEqual(workflow.artifact_sha256(request), decision.request_sha256)

    def test_wire_decision_excludes_runtime_compatibility_fields(self):
        """A serialization regression must fail if shadow metadata leaks onto ai-route-decision-1."""

        decision = workflow.decide_route(
            valid_task(), route_request("SIMPLE", "READ_ONLY"), "shadow"
        )
        wire = decision.to_dict()

        workflow.validate_route_decision(wire)
        self.assertEqual(
            {
                "schema_version",
                "task_id",
                "route",
                "rule_id",
                "task_sha256",
                "request_sha256",
                "decided_at_utc",
                "routing_mode",
                "evidence_class",
            },
            set(wire),
        )
        self.assertNotIn("shadow_route", wire)
        self.assertNotIn("effective_roles", wire)


class RouteDecisionPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)
        self.task = valid_task()
        self.task_path = self.store.create_task(self.task)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_record_route_decision_writes_strict_document_and_hash_event(self):
        """Persistence must atomically record only wire fields and both input hashes."""

        request = route_request("SIMPLE", "READ_ONLY")
        decision = workflow.decide_route(self.task, request, "shadow")
        path = workflow.record_route_decision(self.store, self.task["task_id"], decision)

        self.assertEqual(self.state_root / self.task["task_id"] / "route-decision.json", path)
        persisted = json.loads(path.read_text(encoding="utf-8"))
        workflow.validate_route_decision(persisted)
        self.assertEqual("direct", persisted["route"])
        self.assertNotIn("shadow_route", persisted)
        event = json.loads(
            (self.state_root / self.task["task_id"] / "events.jsonl").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("ROUTE_DECIDED", event["event_type"])
        self.assertEqual(workflow.artifact_sha256(self.task), event["task_sha256"])
        self.assertEqual(workflow.artifact_sha256(request), event["request_sha256"])

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_route_cli_never_starts_a_model_and_prints_canonical_wire_json(self, run_codex):
        """The route CLI must make a local decision without invoking model execution."""

        request_path = Path(self.temporary_directory.name) / "request.json"
        request_path.write_text(
            json.dumps(route_request("SIMPLE", "READ_ONLY")), encoding="utf-8"
        )
        output = StringIO()

        with redirect_stdout(output):
            exit_code = workflow.main(
                [
                    "route",
                    "--task",
                    str(self.task_path),
                    "--request",
                    str(request_path),
                    "--mode",
                    "shadow",
                    "--root",
                    str(self.state_root),
                ]
            )

        self.assertEqual(0, exit_code)
        payload = json.loads(output.getvalue())
        self.assertEqual("direct", payload["route"])
        self.assertEqual(
            workflow._canonical_json(payload) + "\n",
            output.getvalue(),
        )
        run_codex.assert_not_called()


if __name__ == "__main__":
    unittest.main()
