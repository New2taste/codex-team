import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

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

    def test_codex_response_schema_declares_type_for_const(self):
        result_schema = json.loads((ROOT / "config/ai_workflow_result.schema.json").read_text())
        self.assertEqual(
            result_schema["properties"]["schema_version"],
            {"type": "string", "const": "ai-result-1"},
        )

    def test_codex_response_schema_avoids_unsupported_keywords(self):
        result_schema = json.loads((ROOT / "config/ai_workflow_result.schema.json").read_text())

        def collect_keys(value):
            if isinstance(value, dict):
                return set(value).union(*(collect_keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(collect_keys(item) for item in value))
            return set()

        self.assertTrue({"uniqueItems", "minLength"}.isdisjoint(collect_keys(result_schema)))


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


class MetricsReportTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _create_task(self, task_id):
        task = TaskValidationTest().valid_task()
        task["task_id"] = task_id
        self.store.create_task(task)
        return task_id

    def _record(self, task_id, run):
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            workflow.record_metrics(task_id, run)

    def test_record_metrics_never_estimates_missing_or_invalid_token_usage(self):
        task_id = self._create_task("AWF-20260803-001")

        self._record(task_id, {"role": "luna", "duration_seconds": 1.25})
        self._record(task_id, {"role": "luna", "duration_seconds": 0.75, "token_usage": "many"})

        document = json.loads(
            (self.state_root / task_id / "metrics.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(document["token_usage"])
        self.assertEqual([run["token_usage"] for run in document["runs"]], [None, None])
        self.assertRegex(document["runs"][0]["timestamp_utc"], r"Z$")

    def test_aggregate_metrics_separates_luna_value_and_review_cost(self):
        calibration_task = self._create_task("AWF-20260803-001")
        experiment_task = self._create_task("AWF-20260803-002")
        self._record(
            calibration_task,
            {
                "role": "luna",
                "duration_seconds": 1.5,
                "period": "calibration",
                "activity": "self_check",
                "finding_ids": ["luna-1", "luna-2"],
            },
        )
        self._record(
            calibration_task,
            {
                "role": "sol_reviewer",
                "duration_seconds": 2.0,
                "period": "calibration",
                "adopted_luna_finding_ids": ["luna-1"],
                "full_suite_run": True,
            },
        )
        self._record(
            calibration_task,
            {
                "role": "terra",
                "duration_seconds": 3.0,
                "period": "calibration",
                "status": "IMPLEMENTED_CANDIDATE",
            },
        )
        self._record(
            experiment_task,
            {
                "role": "terra",
                "duration_seconds": 4.0,
                "period": "experiment",
                "status": "BLOCKED",
                "semantic_rework": True,
            },
        )

        metrics = workflow.aggregate_metrics(self.state_root)

        self.assertEqual(metrics["calibration_task_count"], 1)
        self.assertEqual(metrics["experiment_task_count"], 1)
        self.assertEqual(metrics["role_calls"], {"luna": 1, "sol_reviewer": 1, "terra": 2})
        self.assertEqual(metrics["sol_participation_count"], 1)
        self.assertEqual(metrics["first_delivery_pass_rate"], 0.5)
        self.assertEqual(metrics["luna_unique_findings"], 2)
        self.assertEqual(metrics["luna_findings_adopted_by_sol"], 1)
        self.assertEqual(metrics["luna_self_check_seconds"], 1.5)
        self.assertEqual(metrics["sol_verification_seconds"], 2.0)
        self.assertEqual(metrics["semantic_reworks"], 1)
        self.assertEqual(metrics["full_suite_runs"], 1)
        self.assertEqual(metrics["end_to_end_seconds"], 10.5)

    def test_report_redacts_secrets_and_high_entropy_event_values(self):
        task_id = self._create_task("AWF-20260803-001")
        high_entropy = "Ab3d" * 32
        self._record(task_id, {"role": "luna", "duration_seconds": 1.0, "period": "calibration"})
        self.store.append_event(
            task_id,
            {
                "event_type": "STOP_LINE",
                "detail": f"TUSHARE_TOKEN=abc123 OPENAI_API_KEY=sk-test-value {high_entropy}",
            },
        )

        report = workflow.render_report(workflow.aggregate_metrics(self.state_root))

        self.assertIn("# AI Workflow Experiment Report", report)
        self.assertIn("Calibration tasks: 1", report)
        self.assertIn("Luna unique findings", report)
        self.assertIn("Stop-line events", report)
        self.assertIn("[REDACTED]", report)
        self.assertNotIn("abc123", report)
        self.assertNotIn("sk-test-value", report)
        self.assertNotIn(high_entropy, report)

    def test_report_redacts_json_style_secret_fields(self):
        task_id = self._create_task("AWF-20260803-001")
        self._record(task_id, {"role": "luna", "duration_seconds": 1.0})
        self.store.append_event(
            task_id,
            {
                "event_type": "STOP_LINE",
                "detail": {"OPENAI_API_KEY": "sk-json-test-value"},
            },
        )

        report = workflow.render_report(workflow.aggregate_metrics(self.state_root))

        self.assertIn("OPENAI_API_KEY=[REDACTED]", report)
        self.assertNotIn("sk-json-test-value", report)

    def test_report_command_writes_one_markdown_file_and_prints_its_path(self):
        task_id = self._create_task("AWF-20260803-001")
        self._record(task_id, {"role": "luna", "duration_seconds": 1.0, "period": "calibration"})
        output_path = Path(self.temporary_directory.name) / "workflow-report.md"
        output = StringIO()

        with redirect_stdout(output):
            exit_code = workflow.main(
                ["report", "--root", str(self.state_root), "--output", str(output_path)]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), f"REPORT_WRITTEN {output_path}\n")
        self.assertEqual(len(list(output_path.parent.glob("*.md"))), 1)
        self.assertIn("# AI Workflow Experiment Report", output_path.read_text(encoding="utf-8"))


class FakeRunnerTest(unittest.TestCase):
    def test_luna_fake_result_never_claims_acceptance(self):
        task = TaskValidationTest().valid_task()
        result = workflow.FakeRunner().run("luna", task)
        self.assertEqual(result["role"], "luna")
        self.assertEqual(result["status"], "SUPPORTED")
        self.assertNotIn("ACCEPTED", result["status"])
        workflow.validate_verification_package("luna", task, result)


class RoutingTest(unittest.TestCase):
    def test_plan_route(self):
        self.assertEqual(workflow.route({"task_type": "PLAN", "risk_flags": []}), ("luna", "sol_planner"))

    def test_acceptance_route(self):
        self.assertEqual(
            workflow.route({"task_type": "ACCEPTANCE", "risk_flags": []}),
            ("luna", "sol_reviewer"),
        )

    def test_plain_remediation_route(self):
        self.assertEqual(
            workflow.route({"task_type": "REMEDIATION", "risk_flags": []}),
            ("terra", "luna", "sol_reviewer"),
        )

    def test_high_risk_remediation_plans_first(self):
        self.assertEqual(
            workflow.route({"task_type": "REMEDIATION", "risk_flags": ["PIT"]}),
            ("sol_planner", "terra", "luna", "sol_reviewer"),
        )


class ScriptedRunner:
    """Test double whose observable calls and responses model one local runner."""

    is_live_model = False

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, role, task):
        self.calls.append(role)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class GatedPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _task(self, task_type="PLAN", risk_flags=None):
        task = TaskValidationTest().valid_task()
        task["task_type"] = task_type
        task["risk_flags"] = [] if risk_flags is None else risk_flags
        if task_type == "REMEDIATION":
            task["allowed_write_paths"] = ["scripts/"]
        if task_type == "ACCEPTANCE":
            task["base_commit"] = "a" * 40
            task["candidate_commit"] = "b" * 40
        return task

    @staticmethod
    def _result(role, status, state):
        result = {
            "schema_version": "ai-result-1",
            "role": role,
            "status": status,
            "summary": "The bounded local stage completed.",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": state,
        }
        if role == "luna":
            result["claims"] = [
                {
                    "id": "claim-1",
                    "kind": "FACT",
                    "text": "The bounded evidence supports the result.",
                    "evidence_ids": ["evidence-1"],
                }
            ]
            result["evidence"] = [
                {
                    "id": "evidence-1",
                    "type": "FILE",
                    "locator": "README.md",
                    "observation": "The authorized evidence was checked.",
                }
            ]
            result["counter_checks"] = [
                {
                    "target_claim_id": "claim-1",
                    "method": "Check the bounded evidence for a contradiction.",
                    "result": "No contradiction found.",
                }
            ]
        return result

    def _create_task(self, task):
        self.store.create_task(task)
        return task["task_id"]

    def test_reviewer_escalation_stops_at_owner_gate_without_calling_sol_xhigh(self):
        task_id = self._create_task(self._task("ACCEPTANCE"))
        runner = ScriptedRunner(
            [
                self._result("luna", "SUPPORTED", "EVIDENCE_READY"),
                self._result("sol_reviewer", "ESCALATION_PROPOSED", "ESCALATION_PROPOSED"),
            ]
        )

        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            state = workflow.run_until_gate(task_id, runner=runner, allow_live_model=False)

        self.assertEqual(state, "AWAITING_OWNER_DECISION")
        self.assertEqual(runner.calls, ["luna", "sol_reviewer"])
        self.assertNotIn("sol_xhigh", runner.calls)

    def test_authorized_escalation_runs_sol_xhigh_once_and_returns_to_a_gate(self):
        task_id = self._create_task(self._task("ACCEPTANCE"))
        first_runner = ScriptedRunner(
            [
                self._result("luna", "SUPPORTED", "EVIDENCE_READY"),
                self._result("sol_reviewer", "ESCALATION_PROPOSED", "ESCALATION_PROPOSED"),
            ]
        )
        escalation_runner = ScriptedRunner(
            [self._result("sol_xhigh", "OPTION_A", "ESCALATION_PROPOSED")]
        )

        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            self.assertEqual(
                workflow.run_until_gate(task_id, runner=first_runner, allow_live_model=False),
                "AWAITING_OWNER_DECISION",
            )
            self.assertEqual(
                workflow.apply_owner_decision(task_id, "authorize_escalation", "owner"),
                "ESCALATION_AUTHORIZED",
            )
            self.assertEqual(
                workflow.run_until_gate(task_id, runner=escalation_runner, allow_live_model=False),
                "AWAITING_OWNER_DECISION",
            )

        self.assertEqual(escalation_runner.calls, ["sol_xhigh"])

    def test_two_malformed_results_use_only_one_technical_retry_then_block(self):
        task_id = self._create_task(self._task())
        runner = ScriptedRunner([{"not": "ai-result-1"}, {"not": "ai-result-1"}])

        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            state = workflow.run_until_gate(task_id, runner=runner, allow_live_model=False)

        self.assertEqual(state, "BLOCKED")
        self.assertEqual(runner.calls, ["luna", "luna"])

    def test_second_terra_failure_after_owner_authorized_rework_does_not_run_terra_third_time(self):
        task_id = self._create_task(self._task("REMEDIATION"))
        runner = ScriptedRunner(
            [
                self._result("terra", "BLOCKED", "BLOCKED"),
                self._result("terra", "BLOCKED", "BLOCKED"),
            ]
        )

        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            self.assertEqual(
                workflow.run_until_gate(task_id, runner=runner, allow_live_model=False),
                "AWAITING_OWNER_DECISION",
            )
            self.assertEqual(
                workflow.apply_owner_decision(task_id, "approve_execution", "owner"),
                "APPROVED_FOR_EXECUTION",
            )
            self.assertEqual(
                workflow.run_until_gate(task_id, runner=runner, allow_live_model=False),
                "AWAITING_OWNER_DECISION",
            )
            self.assertEqual(
                workflow.apply_owner_decision(task_id, "authorize_rework", "owner"),
                "REWORK_AUTHORIZED",
            )
            self.assertEqual(
                workflow.run_until_gate(task_id, runner=runner, allow_live_model=False),
                "BLOCKED",
            )
            self.assertEqual(
                workflow.run_until_gate(task_id, runner=runner, allow_live_model=False),
                "BLOCKED",
            )

        self.assertEqual(runner.calls, ["terra", "terra"])

    def test_authorized_live_remediation_uses_the_existing_safe_worktree_creator(self):
        task = self._task("REMEDIATION")
        task_id = self._create_task(task)
        runner = ScriptedRunner([self._result("terra", "BLOCKED", "BLOCKED")])
        runner.is_live_model = True

        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            workflow.run_until_gate(task_id, runner=ScriptedRunner([]), allow_live_model=False)
            workflow.apply_owner_decision(task_id, "approve_execution", "owner")
            with mock.patch("scripts.ai_workflow.create_worktree") as create_worktree:
                self.assertEqual(
                    workflow.run_until_gate(task_id, runner=runner, allow_live_model=True),
                    "AWAITING_OWNER_DECISION",
                )

        create_worktree.assert_called_once_with(task, owner_authorized=True)

    def test_owner_decision_uses_closed_set_and_records_complete_audit_fields(self):
        task_id = self._create_task(self._task("REMEDIATION"))
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            workflow.run_until_gate(task_id, runner=ScriptedRunner([]), allow_live_model=False)
            with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_OWNER_DECISION"):
                workflow.apply_owner_decision(task_id, "merge", "owner")
            with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_ACTOR"):
                workflow.apply_owner_decision(task_id, "approve_execution", "")
            self.assertEqual(
                workflow.apply_owner_decision(task_id, "defer", "owner"),
                "DEFERRED",
            )

        record = json.loads(
            (self.state_root / task_id / "human-decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(record["decision"], "defer")
        self.assertEqual(record["previous_state"], "AWAITING_OWNER_DECISION")
        self.assertEqual(record["new_state"], "DEFERRED")
        self.assertRegex(record["timestamp_utc"], r"Z$")
        self.assertEqual(len(record["task_sha256"]), 64)

    def test_decide_command_records_only_a_complete_closed_set_owner_decision(self):
        task_id = self._create_task(self._task("REMEDIATION"))
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            workflow.run_until_gate(task_id, runner=ScriptedRunner([]), allow_live_model=False)
        output = StringIO()
        with redirect_stdout(output):
            exit_code = workflow.main(
                ["decide", task_id, "defer", "--by", "owner", "--root", str(self.state_root)]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), "DECISION_RECORDED\n")

        record = json.loads(
            (self.state_root / task_id / "human-decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(record["new_state"], "DEFERRED")
        self.assertIn("task_sha256", record)

    def test_owner_authorization_edges_cannot_be_crossed_by_state_table_alone(self):
        for current, target in (
            ("DEFERRED", "TASK_VALIDATED"),
            ("REWORK_AUTHORIZED", "IMPLEMENTATION_RUNNING"),
            ("ESCALATION_AUTHORIZED", "PLAN_OR_REVIEW_RUNNING"),
        ):
            with self.subTest(current=current):
                with self.assertRaisesRegex(workflow.WorkflowError, "OWNER_AUTHORIZATION_REQUIRED"):
                    workflow.next_state(current, target, owner_authorized=False)


class RetryBudgetTest(unittest.TestCase):
    def test_each_budget_is_limited_to_one_consumption(self):
        budget = workflow.RetryBudget()
        budget.consume_technical()
        budget.consume_rework()
        budget.consume_escalation()

        with self.assertRaisesRegex(workflow.WorkflowError, "RETRY_BUDGET_EXHAUSTED"):
            budget.consume_technical()
        with self.assertRaisesRegex(workflow.WorkflowError, "RETRY_BUDGET_EXHAUSTED"):
            budget.consume_rework()
        with self.assertRaisesRegex(workflow.WorkflowError, "RETRY_BUDGET_EXHAUSTED"):
            budget.consume_escalation()


class CodexCommandTest(unittest.TestCase):
    def test_luna_command_is_pinned_and_read_only(self):
        command = workflow.build_codex_command(
            "luna", ROOT, Path("result.json"), ROOT / "config/ai_workflow_result.schema.json"
        )
        self.assertIn("gpt-5.6-luna", command)
        self.assertIn('model_reasoning_effort="max"', command)
        self.assertIn("read-only", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--agent", command)


class CodexRunnerTest(unittest.TestCase):
    def valid_task(self):
        return TaskValidationTest().valid_task()

    def valid_result(self, role="luna", status="SUPPORTED"):
        result = {
            "schema_version": "ai-result-1",
            "role": role,
            "status": status,
            "summary": "Evidence supports the claim.",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "EVIDENCE_READY",
        }
        if role == "luna":
            result.update(self.l1_result(role=role, status=status))
        return result

    def l1_result(self, claim_count=1, counter_check_count=1, role="luna", status="SUPPORTED"):
        result = {
            "schema_version": "ai-result-1",
            "role": role,
            "status": status,
            "summary": "Evidence supports the claim.",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "EVIDENCE_READY",
        }
        result["claims"] = [
            {
                "id": f"claim-{index}",
                "kind": "FACT",
                "text": f"Supported fact {index}",
                "evidence_ids": ["evidence-1"],
            }
            for index in range(claim_count)
        ]
        result["evidence"] = [
            {
                "id": "evidence-1",
                "type": "FILE",
                "locator": "README.md",
                "observation": "The authorized document supports the claim.",
            }
        ]
        result["counter_checks"] = [
            {
                "target_claim_id": "claim-0",
                "method": f"Cross-check {index}",
                "result": "No contradiction found.",
            }
            for index in range(counter_check_count)
        ]
        return result

    def test_business_secrets_are_not_forwarded(self):
        env = workflow.sanitized_environment(
            {
                "HOME": "/tmp/home",
                "PATH": "/usr/bin",
                "CODEX_HOME": "/tmp/codex",
                "TUSHARE_TOKEN": "secret",
                "OPENAI_API_KEY": "secret",
                "DB_PASSWORD": "secret",
            }
        )
        self.assertEqual(env["HOME"], "/tmp/home")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["CODEX_HOME"], "/tmp/codex")
        self.assertNotIn("TUSHARE_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("DB_PASSWORD", env)

    def test_role_status_cross_checks_reject_invalid_statuses(self):
        invalid = (
            ("luna", "ACCEPTANCE_RECOMMENDED"),
            ("terra", "SUPPORTED"),
            ("sol_reviewer", "IMPLEMENTED_CANDIDATE"),
        )
        for role, status in invalid:
            with self.subTest(role=role, status=status):
                with self.assertRaisesRegex(workflow.WorkflowError, "ROLE_STATUS_MISMATCH"):
                    workflow.validate_role_result(role, self.valid_result(role, status), set())

    def test_read_only_role_rejects_real_diff_even_when_result_declares_none(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_ROLE_MODIFIED_REPO"):
            workflow.validate_role_result(
                "luna", self.valid_result("luna"), {"forbidden/change.py"}
            )

    def test_result_changed_files_must_match_real_diff(self):
        result = self.valid_result("terra", "IMPLEMENTED_CANDIDATE")
        result["changed_files"] = ["declared.py"]
        with self.assertRaisesRegex(workflow.WorkflowError, "CHANGED_FILES_MISMATCH"):
            workflow.validate_role_result("terra", result, {"actual.py"})

    def test_result_rejects_incomplete_nested_schema_record(self):
        result = self.valid_result()
        result["claims"] = [{"id": "claim-1"}]
        with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_ROLE_RESULT"):
            workflow.validate_role_result("luna", result, set())

    def test_prompt_is_limited_to_task_contract_and_named_evidence(self):
        task = self.valid_task()
        contract = {"acceptance": "run unit tests"}
        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(temp) / "evidence.txt"
            evidence.write_text("verified fact", encoding="utf-8")
            prompt = workflow.build_role_prompt("luna", task, contract, [evidence])
        self.assertIn("Handle only bounded tasks.", prompt)
        prompt_lines = prompt.splitlines()
        self.assertEqual(json.loads(prompt_lines[1].removeprefix("Task envelope: ")), task)
        self.assertEqual(json.loads(prompt_lines[2].removeprefix("Task contract: ")), contract)
        evidence_manifest = json.loads(prompt_lines[3].removeprefix("Named evidence: "))
        self.assertEqual(evidence_manifest[0]["path"], str(evidence))
        self.assertEqual(
            evidence_manifest[0]["sha256"],
            "6f9a5b7a0a9ebb03cde5ab869b864795326fb356563618a3ad0b2b0eb1a835bc",
        )
        self.assertIn(str(evidence), prompt)
        self.assertIn('Output "role" exactly as "luna".', prompt)
        self.assertIn(
            'Output "status" as exactly one of: SUPPORTED, PARTIALLY_SUPPORTED, NOT_SUPPORTED, BLOCKED.',
            prompt,
        )
        self.assertIn(
            "For Luna L1, output 1 to 5 claims and exactly 1 counter_check unless status is BLOCKED",
            prompt,
        )
        self.assertIn("Read the named evidence files at the listed paths", prompt)
        self.assertIn("only output ai-result-1 JSON", prompt)
        self.assertNotIn("registry/", prompt)
        self.assertNotIn("chat history", prompt)

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.subprocess.run")
    def test_run_codex_enforces_luna_l1_evidence_package(self, run, _capture_repo):
        task = self.valid_task()
        cases = [
            ("five claims and one check", self.l1_result(5, 1), None),
            ("six claims", self.l1_result(6, 1), "INVALID_VERIFICATION_PACKAGE"),
            ("two checks", self.l1_result(5, 2), "INVALID_VERIFICATION_PACKAGE"),
        ]
        dangling_evidence = self.l1_result()
        dangling_evidence["claims"][0]["evidence_ids"] = ["missing"]
        cases.append(("dangling evidence", dangling_evidence, "INVALID_VERIFICATION_PACKAGE"))
        dangling_claim = self.l1_result()
        dangling_claim["counter_checks"][0]["target_claim_id"] = "missing"
        cases.append(("dangling claim", dangling_claim, "INVALID_VERIFICATION_PACKAGE"))
        blocked = self.l1_result(0, 0, status="BLOCKED")
        cases.append(("blocked without invented evidence", blocked, None))
        terra = self.valid_result("terra", "IMPLEMENTED_CANDIDATE")
        cases.append(("non-luna role", terra, None))

        for name, result, error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                output_path = root / "result.json"
                output_path.write_text(json.dumps(result), encoding="utf-8")
                paths = workflow.RunPaths(
                    repo=ROOT,
                    output_path=output_path,
                    schema_path=ROOT / "config/ai_workflow_result.schema.json",
                    logs_dir=root / "logs",
                )
                run.return_value = mock.Mock(returncode=0, stdout='{"event":"done"}\n', stderr="")
                if error:
                    with self.assertRaisesRegex(workflow.WorkflowError, error):
                        workflow.run_codex(result["role"], task, "task contract", paths)
                else:
                    self.assertEqual(
                        workflow.run_codex(result["role"], task, "task contract", paths),
                        result,
                    )

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.subprocess.run")
    def test_run_codex_passes_sanitized_stdin_and_accepts_valid_output(self, run, _capture_repo):
        result = self.valid_result()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output_path = root / "luna-result.json"
            output_path.write_text(json.dumps(result), encoding="utf-8")
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=output_path,
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
            )
            run.return_value = mock.Mock(returncode=0, stdout='{"event":"done"}\n', stderr="")
            with mock.patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "secret", "TUSHARE_TOKEN": "secret", "PATH": "/usr/bin"},
                clear=True,
            ):
                actual = workflow.run_codex("luna", self.valid_task(), "task contract", paths)

            self.assertEqual(actual, result)
            self.assertEqual((root / "logs/luna-events.jsonl").read_text(), '{"event":"done"}\n')

        _, kwargs = run.call_args
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["input"], "task contract")
        self.assertTrue(kwargs["text"])
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("TUSHARE_TOKEN", kwargs["env"])

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.subprocess.run")
    def test_run_codex_rejects_timeout_exit_and_invalid_json(self, run, _capture_repo):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
            )
            run.side_effect = __import__("subprocess").TimeoutExpired("codex", 30)
            with self.assertRaisesRegex(workflow.WorkflowError, "CODEX_TIMEOUT"):
                workflow.run_codex("luna", self.valid_task(), "task contract", paths)

            run.side_effect = None
            run.return_value = mock.Mock(returncode=23, stdout="", stderr="failed")
            with self.assertRaisesRegex(workflow.WorkflowError, "CODEX_EXIT_NONZERO"):
                workflow.run_codex("luna", self.valid_task(), "task contract", paths)

            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            paths.output_path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_ROLE_RESULT"):
                workflow.run_codex("luna", self.valid_task(), "task contract", paths)

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.subprocess.run")
    def test_run_codex_redacts_secret_assignments_and_long_tokens_from_events(self, run, _capture_repo):
        result = self.valid_result()
        long_token = "Ab3d" * 32
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output_path = root / "luna-result.json"
            output_path.write_text(json.dumps(result), encoding="utf-8")
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=output_path,
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
            )
            run.return_value = mock.Mock(
                returncode=0,
                stdout=f"TUSHARE_TOKEN=abc123 OPENAI_API_KEY=sk-test-value {long_token}",
                stderr="",
            )
            workflow.run_codex("luna", self.valid_task(), "task contract", paths)
            events = (root / "logs/luna-events.jsonl").read_text(encoding="utf-8")

        self.assertIn("[REDACTED]", events)
        self.assertIn("TUSHARE_TOKEN=[REDACTED]", events)
        self.assertIn("OPENAI_API_KEY=[REDACTED]", events)
        self.assertNotIn("abc123", events)
        self.assertNotIn("sk-test-value", events)
        self.assertNotIn(long_token, events)


class LiveLunaCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)
        self.task = TaskValidationTest().valid_task()
        self.task_path = self.store.create_task(self.task)

    def tearDown(self):
        self.temporary_directory.cleanup()

    @staticmethod
    def _result():
        return {
            "schema_version": "ai-result-1",
            "role": "luna",
            "status": "SUPPORTED",
            "summary": "The state machine and gates are coherent.",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "EVIDENCE_READY",
        }

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_live_runner_requires_explicit_authorization(self, run_codex):
        output = StringIO()

        with redirect_stdout(output):
            exit_code = workflow.main(
                ["run", str(self.task_path), "--runner", "live", "--root", str(self.state_root)]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("LIVE_MODEL_NOT_AUTHORIZED", output.getvalue())
        run_codex.assert_not_called()

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_live_runner_rejects_non_luna_roles(self, run_codex):
        output = StringIO()

        with redirect_stdout(output):
            exit_code = workflow.main(
                [
                    "run",
                    str(self.task_path),
                    "--runner",
                    "live",
                    "--allow-live-model",
                    "--role",
                    "terra",
                    "--root",
                    str(self.state_root),
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("LIVE_ROLE_NOT_ALLOWED", output.getvalue())
        run_codex.assert_not_called()

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_authorized_live_luna_uses_only_task_evidence_and_task_directory(self, run_codex):
        result = self._result()
        run_codex.return_value = result
        output = StringIO()

        with redirect_stdout(output):
            exit_code = workflow.main(
                [
                    "run",
                    str(self.task_path),
                    "--runner",
                    "live",
                    "--allow-live-model",
                    "--role",
                    "luna",
                    "--root",
                    str(self.state_root),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), workflow._canonical_json(result) + "\n")
        run_codex.assert_called_once()
        role, task, prompt, paths = run_codex.call_args.args
        self.assertEqual(role, "luna")
        self.assertEqual(task, self.task)
        self.assertEqual(paths.repo, ROOT)
        self.assertEqual(paths.output_path, self.state_root / self.task["task_id"] / "luna-result.json")
        self.assertEqual(paths.schema_path, ROOT / "config/ai_workflow_result.schema.json")
        self.assertEqual(paths.logs_dir, self.state_root / self.task["task_id"] / "logs")
        prompt_lines = prompt.splitlines()
        self.assertEqual(json.loads(prompt_lines[1].removeprefix("Task envelope: ")), self.task)
        self.assertEqual(
            json.loads(prompt_lines[2].removeprefix("Task contract: ")),
            {"acceptance_commands": [], "verification_level": "L1"},
        )
        self.assertEqual(
            json.loads(prompt_lines[3].removeprefix("Named evidence: "))[0]["path"],
            str(ROOT / "README.md"),
        )
        self.assertNotIn("registry/", prompt)
        self.assertNotIn("chat history", prompt)


class GitSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_directory.name) / "repository"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Workflow Test")
        (self.repo / "tracked.txt").write_text("first\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "initial")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _git(self, *args):
        completed = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def _task(self):
        return {
            "schema_version": "ai-task-1",
            "task_id": "AWF-20260803-001",
            "task_type": "REMEDIATION",
            "objective": "Apply a bounded workflow change",
            "repository_root": str(self.repo),
            "source_worktree": None,
            "base_commit": self._git("rev-parse", "HEAD"),
            "candidate_commit": None,
            "authoritative_files": ["tracked.txt"],
            "allowed_write_paths": ["allowed/"],
            "forbidden_actions": ["merge", "push"],
            "risk_flags": [],
            "acceptance_commands": [],
            "verification_level": "L1",
            "human_gates": ["EXECUTION_APPROVAL"],
        }

    @staticmethod
    def _valid_role_result(role, status):
        return {
            "schema_version": "ai-result-1",
            "role": role,
            "status": status,
            "summary": "The bounded run completed.",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "EVIDENCE_READY",
        }

    def test_assert_pinned_rejects_a_repository_head_that_moved(self):
        snapshot = workflow.capture_repo(self.repo)

        (self.repo / "tracked.txt").write_text("second\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "second")

        with self.assertRaisesRegex(workflow.WorkflowError, "HEAD_DRIFT"):
            workflow.assert_pinned(snapshot, self.repo)

    def test_changed_paths_reports_the_files_between_two_commits(self):
        base = self._git("rev-parse", "HEAD")
        (self.repo / "allowed").mkdir()
        (self.repo / "allowed/a.py").write_text("allowed\n", encoding="utf-8")
        (self.repo / "forbidden").mkdir()
        (self.repo / "forbidden/b.py").write_text("forbidden\n", encoding="utf-8")
        self._git("add", "allowed/a.py", "forbidden/b.py")
        self._git("commit", "-m", "changed paths")
        candidate = self._git("rev-parse", "HEAD")

        self.assertEqual(
            workflow.changed_paths(self.repo, base, candidate),
            {"allowed/a.py", "forbidden/b.py"},
        )

    def test_assert_allowed_changes_rejects_a_path_outside_the_allowed_prefix(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "OUT_OF_SCOPE_CHANGE"):
            workflow.assert_allowed_changes(
                {"allowed/a.py", "forbidden/b.py"},
                ["allowed/"],
            )

    @mock.patch("scripts.ai_workflow.subprocess.run")
    def test_git_uses_a_list_command_without_a_shell(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="pinned-head\n", stderr="")

        self.assertEqual(workflow.git(self.repo, "rev-parse", "HEAD"), "pinned-head")

        command, kwargs = run.call_args
        self.assertEqual(command[0], ["git", "-C", str(self.repo), "rev-parse", "HEAD"])
        self.assertFalse(kwargs["shell"])

    def test_read_only_luna_and_sol_runs_reject_real_repository_mutations(self):
        for role, status in (("luna", "SUPPORTED"), ("sol_reviewer", "ACCEPTANCE_RECOMMENDED")):
            with self.subTest(role=role):
                output_path = self.repo / f"{role}-result.json"
                output_path.write_text(
                    json.dumps(self._valid_role_result(role, status)), encoding="utf-8"
                )
                paths = workflow.RunPaths(
                    repo=self.repo,
                    output_path=output_path,
                    schema_path=ROOT / "config/ai_workflow_result.schema.json",
                    logs_dir=Path(self.temporary_directory.name) / "logs",
                )
                real_subprocess_run = subprocess.run

                def run_with_real_git(command, *args, **kwargs):
                    if command[0] == "git":
                        return real_subprocess_run(command, *args, **kwargs)
                    (self.repo / f"{role}-mutation.txt").write_text("changed\n", encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0, stdout="{\"event\": \"done\"}\n", stderr="")

                with mock.patch("scripts.ai_workflow.subprocess.run", side_effect=run_with_real_git):
                    with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_ROLE_MODIFIED_REPO"):
                        workflow.run_codex(role, self._task(), "bounded task", paths)

    def test_create_worktree_rejects_an_unauthorized_owner_before_running_git(self):
        task = self._task()
        with mock.patch("scripts.ai_workflow.subprocess.run") as run:
            with self.assertRaisesRegex(workflow.WorkflowError, "OWNER_AUTHORIZATION_REQUIRED"):
                workflow.create_worktree(task, owner_authorized=False)

        run.assert_not_called()

    def test_create_worktree_requires_an_execution_approval_record(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "APPROVED_FOR_EXECUTION_REQUIRED"):
            workflow.create_worktree(self._task(), owner_authorized=True)

    def test_create_worktree_uses_the_approved_branch_and_directory(self):
        task = self._task()
        decision_path = (
            self.repo
            / "data/state/ai-workflow"
            / task["task_id"]
            / "human-decisions.jsonl"
        )
        decision_path.parent.mkdir(parents=True)
        decision_path.write_text(
            json.dumps({"decision": "APPROVED_FOR_EXECUTION", "by": "owner"}) + "\n",
            encoding="utf-8",
        )

        worktree = workflow.create_worktree(task, owner_authorized=True)

        self.assertEqual(
            worktree,
            self.repo / ".codex-worktrees" / "awf-20260803-001",
        )
        self.assertEqual(self._git("-C", str(worktree), "branch", "--show-current"), "aiwf/awf-20260803-001")


if __name__ == "__main__":
    unittest.main()
