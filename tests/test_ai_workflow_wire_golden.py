"""Frozen wire golden and production-surface negative regressions."""

from __future__ import annotations

import argparse
import inspect
import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_authorizations as authorizations
from scripts import ai_workflow_costs as costs
from scripts import ai_workflow_dispatch_policy as policy
from scripts import ai_workflow_evidence as evidence
from scripts import ai_workflow_ownership as ownership
from scripts import ai_workflow_repairs as repairs
from scripts import ai_workflow_router_probe as probe
from scripts import ai_workflow_verdicts as verdicts
from scripts import sync_plugin
from tests import test_ai_workflow as _workflow_tests
from tests import test_ai_workflow_router_probe as _probe_tests
from tests.test_ai_workflow import (
    _RecordingPopen,
    _compat_popen,
    _install_declaration,
)
from tests.test_ai_workflow_construction_execution import (
    construction_plan,
    remediation_task,
)
from tests.test_ai_workflow_dispatch_policy import TASK_ID, _DispatchStoreMixin


ROOT = Path(__file__).resolve().parents[1]
README_DISCLAIMER = (
    "这不是实测成本赢家，也不改生产 `effective_route`，实际仍以使用者选择为准"
)
FROZEN_TASK_FIELDS = frozenset(
    {
        "schema_version",
        "task_id",
        "paired_case_id",
        "task_type",
        "objective",
        "repository_root",
        "source_worktree",
        "base_commit",
        "candidate_commit",
        "authoritative_files",
        "allowed_write_paths",
        "forbidden_actions",
        "risk_flags",
        "acceptance_commands",
        "verification_level",
        "human_gates",
    }
)
FROZEN_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "role",
        "status",
        "summary",
        "claims",
        "evidence",
        "counter_checks",
        "changed_files",
        "blind_spots",
        "unresolved_questions",
        "recommended_next_state",
        "dispatch_id",
        "task_id",
        "step_id",
        "attempt",
    }
)
FROZEN_ROUTE_DECISION_FIELDS = frozenset(
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
    }
)
FROZEN_OWNER_DECISIONS = frozenset(
    {
        "approve_execution",
        "authorize_rework",
        "authorize_escalation",
        "defer",
        "close",
        "abort",
    }
)
FROZEN_EFFECT_KINDS = frozenset(
    {
        "CONTROL_PLANE_ARTIFACT",
        "OWNED_WRITE",
        "UNTRACKED_WRITE",
        "COMMAND_GENERATED",
        "EXTERNAL",
        "UNOBSERVED_ASSUMED_PRESENT",
    }
)
FROZEN_OWNERSHIP_VIOLATION_EVENT_FIELDS = frozenset(
    {
        "event_type",
        "task_id",
        "envelope_hash",
        "permit_id",
        "role",
        "paths",
        "timestamp_utc",
    }
)
FROZEN_ACCEPTANCE_EVENT_TYPES = frozenset(
    {
        "ACCEPTANCE_OPENED",
        "ASSIGNMENT_ISSUED",
        "ASSIGNMENT_ATTEMPT_STARTED",
        "ASSIGNMENT_ATTEMPT_FAILED",
        "REPAIR_COMPLETED",
        "REVIEW_COMPLETED",
    }
)
FROZEN_ACCEPTANCE_COMMON_FIELDS = frozenset(
    {
        "ledger_version",
        "event_type",
        "event_index",
        "event_id",
        "previous_event_id",
        "timestamp_utc",
        "task_id",
        "task_sha256",
        "base_commit",
        "candidate_commit",
    }
)
FROZEN_ACCEPTANCE_REQUIRED_EXTRAS = {
    "ACCEPTANCE_OPENED": frozenset(
        {
            "owner_actor",
            "owner_receipt",
            "owner_receipt_sha256",
            "initial_candidate_commit",
        }
    ),
    "ASSIGNMENT_ISSUED": frozenset(
        {
            "assignment_id",
            "attempt_id",
            "phase",
            "expected_actor",
            "input_candidate_commit",
            "findings",
            "allowed_paths",
            "capability",
        }
    ),
    "ASSIGNMENT_ATTEMPT_STARTED": frozenset(
        {"assignment_id", "attempt_id", "actor_receipt", "receipt_sha256"}
    ),
    "ASSIGNMENT_ATTEMPT_FAILED": frozenset(
        {"assignment_id", "attempt_id", "failure_code", "failure_message"}
    ),
    "REPAIR_COMPLETED": frozenset(
        {
            "assignment_id",
            "attempt_id",
            "actor_receipt",
            "changed_paths",
            "actual_changed_paths",
            "output_candidate_commit",
        }
    ),
    "REVIEW_COMPLETED": frozenset(
        {
            "assignment_id",
            "attempt_id",
            "reviewer_receipt",
            "verdict",
            "findings",
            "evidence",
            "evidence_sha256",
        }
    ),
}
FROZEN_ACCEPTANCE_OPTIONAL_EXTRAS = {
    "ACCEPTANCE_OPENED": frozenset(),
    "ASSIGNMENT_ISSUED": frozenset(),
    "ASSIGNMENT_ATTEMPT_STARTED": frozenset(
        {"controller_attestation", "controller_attestation_sha256"}
    ),
    "ASSIGNMENT_ATTEMPT_FAILED": frozenset(),
    "REPAIR_COMPLETED": frozenset(
        {
            "terminal_state",
            "terminal_reason",
            "whole_project_acceptance_required",
        }
    ),
    "REVIEW_COMPLETED": frozenset(
        {
            "terminal_state",
            "terminal_reason",
            "whole_project_acceptance_required",
        }
    ),
}
LANDED_SIDECAR_SCHEMAS = (
    "ai_workflow_route_declaration.schema.json",
    "ai_workflow_candidate_state.schema.json",
    "ai_workflow_final_verdict.schema.json",
    "ai_workflow_ownership_registry.schema.json",
    "ai_workflow_side_effect.schema.json",
    "ai_workflow_owner_authorization.schema.json",
    "ai_workflow_rate_snapshot.schema.json",
    "ai_workflow_preflight_record.schema.json",
    "ai_workflow_runtime_evidence_v2.schema.json",
)
LANDED_PRODUCTION_MODULES = (
    "ai_workflow_declarations.py",
    "ai_workflow_candidate_state.py",
    "ai_workflow_verdicts.py",
    "ai_workflow_ownership.py",
    "ai_workflow_side_effects.py",
    "ai_workflow_authorizations.py",
    "ai_workflow_preflight.py",
    "ai_workflow_dispatch_policy.py",
    "ai_workflow_evidence.py",
)
EXCLUDED_RUNTIME_SCRIPTS = (
    "ai_workflow_identity_probe.py",
    "ai_workflow_evidence_chain.py",
    "ai_workflow_router_probe.py",
    "collect_test_baseline.py",
)


def _schema_properties(name: str) -> set[str]:
    payload = json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))
    return set(payload["properties"])


def _git_show(spec: str) -> bytes:
    completed = subprocess.run(
        ["git", "show", spec],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return completed.stdout


class FrozenWireGoldenTest(unittest.TestCase):
    def test_ai_task_1_field_set_matches_schema_and_constant(self):
        self.assertEqual(FROZEN_TASK_FIELDS, workflow.TASK_FIELDS)
        self.assertEqual(FROZEN_TASK_FIELDS, _schema_properties("ai_workflow_task.schema.json"))
        self.assertEqual(
            FROZEN_TASK_FIELDS - {"paired_case_id"},
            workflow.REQUIRED_TASK_FIELDS,
        )

    def test_ai_result_1_field_set_matches_schema_and_constants(self):
        combined = workflow.RESULT_REQUIRED_FIELDS | workflow.RESULT_IDENTITY_FIELDS
        self.assertEqual(FROZEN_RESULT_FIELDS, combined)
        self.assertEqual(
            FROZEN_RESULT_FIELDS,
            _schema_properties("ai_workflow_result.schema.json"),
        )
        self.assertEqual(
            frozenset({"dispatch_id", "task_id", "step_id", "attempt"}),
            workflow.RESULT_IDENTITY_FIELDS,
        )

    def test_route_decision_nine_fields_are_frozen(self):
        self.assertEqual(9, len(FROZEN_ROUTE_DECISION_FIELDS))
        self.assertEqual(FROZEN_ROUTE_DECISION_FIELDS, artifacts.ROUTE_DECISION_FIELDS)
        self.assertEqual(
            FROZEN_ROUTE_DECISION_FIELDS,
            _schema_properties("ai_workflow_route_decision.schema.json"),
        )

    def test_acceptance_ledger_version_and_six_event_types(self):
        self.assertEqual("adversarial-acceptance-1", repairs._ACCEPTANCE_LEDGER_VERSION)
        self.assertEqual(FROZEN_ACCEPTANCE_EVENT_TYPES, repairs._ACCEPTANCE_EVENT_TYPES)
        self.assertEqual(6, len(repairs._ACCEPTANCE_EVENT_TYPES))
        self.assertEqual(FROZEN_ACCEPTANCE_COMMON_FIELDS, repairs._V2_COMMON_FIELDS)
        self.assertEqual(
            FROZEN_ACCEPTANCE_EVENT_TYPES,
            set(FROZEN_ACCEPTANCE_REQUIRED_EXTRAS),
        )
        for event_type, required in FROZEN_ACCEPTANCE_REQUIRED_EXTRAS.items():
            allowed = (
                FROZEN_ACCEPTANCE_COMMON_FIELDS
                | required
                | FROZEN_ACCEPTANCE_OPTIONAL_EXTRAS[event_type]
            )
            self.assertTrue(required.isdisjoint(FROZEN_ACCEPTANCE_COMMON_FIELDS), event_type)
            self.assertTrue(
                FROZEN_ACCEPTANCE_OPTIONAL_EXTRAS[event_type].isdisjoint(required),
                event_type,
            )
            self.assertEqual(
                allowed,
                FROZEN_ACCEPTANCE_COMMON_FIELDS
                | required
                | FROZEN_ACCEPTANCE_OPTIONAL_EXTRAS[event_type],
            )

    def test_owner_decisions_closed_set(self):
        self.assertEqual(FROZEN_OWNER_DECISIONS, workflow.OWNER_DECISIONS)
        self.assertNotIn("authorize_final_xhigh", workflow.OWNER_DECISIONS)

    def test_effect_kinds_closed_set_excludes_ownership_violation(self):
        self.assertEqual(FROZEN_EFFECT_KINDS, ownership.EFFECT_KINDS)
        self.assertNotIn("OWNERSHIP_VIOLATION_RECORDED", ownership.EFFECT_KINDS)
        self.assertEqual(
            "OWNERSHIP_VIOLATION_RECORDED",
            ownership.OWNERSHIP_VIOLATION_EVENT_TYPE,
        )
        self.assertEqual(
            FROZEN_OWNERSHIP_VIOLATION_EVENT_FIELDS,
            ownership.OWNERSHIP_VIOLATION_EVENT_FIELDS,
        )

    def test_exclude_constants_are_per_record_class_and_not_merged(self):
        self.assertEqual(frozenset({"authorization_id"}), authorizations.AUTHORIZATION_ID_EXCLUDE)
        self.assertEqual(frozenset({"record_id"}), authorizations.RECORD_ID_EXCLUDE)
        self.assertEqual(frozenset({"verdict_id"}), verdicts.VERDICT_ID_EXCLUDE)
        self.assertEqual(frozenset({"event_id"}), evidence.LAUNCH_INTENT_ID_EXCLUDE)
        self.assertEqual(frozenset({"evidence_id"}), evidence.RUNTIME_EVIDENCE_ID_EXCLUDE)
        self.assertNotEqual(
            authorizations.AUTHORIZATION_ID_EXCLUDE,
            authorizations.RECORD_ID_EXCLUDE,
        )
        self.assertEqual(
            authorizations.AUTHORIZATION_ID_EXCLUDE | authorizations.RECORD_ID_EXCLUDE,
            frozenset({"authorization_id", "record_id"}),
        )
        merged = (
            authorizations.AUTHORIZATION_ID_EXCLUDE
            | authorizations.RECORD_ID_EXCLUDE
            | verdicts.VERDICT_ID_EXCLUDE
            | evidence.LAUNCH_INTENT_ID_EXCLUDE
            | evidence.RUNTIME_EVIDENCE_ID_EXCLUDE
        )
        self.assertEqual(
            merged,
            frozenset(
                {
                    "authorization_id",
                    "record_id",
                    "verdict_id",
                    "event_id",
                    "evidence_id",
                }
            ),
        )

    def test_baseline_manifest_blob_matches_task_00_freeze(self):
        current = (ROOT / "tests" / "baseline_manifest.json").read_bytes()
        frozen = _git_show("e35c010:tests/baseline_manifest.json")
        self.assertEqual(frozen, current)
        manifest = json.loads(current)
        self.assertEqual("ce24e14ec39107a97a4c675ea763e784caff8c60", manifest["base_commit"])
        self.assertEqual(588, len(manifest["tests"]))


class EffectiveRouteAndProductionSurfaceTest(unittest.TestCase):
    def test_landed_artifacts_exist_and_probe_effective_route_stays_unchanged(self):
        for name in LANDED_SIDECAR_SCHEMAS:
            self.assertTrue((ROOT / "config" / name).is_file(), name)
        for name in LANDED_PRODUCTION_MODULES:
            self.assertTrue((ROOT / "scripts" / name).is_file(), name)
        rows, cost_rows, source = _probe_tests.RouterProbeAnalysisTest.measured_matrix()
        summary = probe.aggregate_probe_results(
            rows, cost_rows, source_manifest=source
        )
        self.assertEqual("UNCHANGED", summary["effective_route"])
        self.assertEqual("router-probe-summary-2", summary["schema_version"])

    def test_route_declaration_does_not_change_stored_route_decision_wire(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repository"
            repo.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "golden@example.test"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Golden Test"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "commit.gpgsign", "false"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            (repo / "README.md").write_text("repo\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
            (repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
            store = workflow.WorkflowStore(root / "state")
            task = _workflow_tests.TaskValidationTest().valid_task()
            task["repository_root"] = str(repo)
            store.create_task(task)
            task_id = str(task["task_id"])
            request = {
                "schema_version": "ai-route-request-1",
                "task_id": task_id,
                "work_class": "PLANNING_ONLY",
                "execution_need": "READ_ONLY",
                "decomposable": True,
                "risk_flags": [],
                "reason_codes": ["PLAN_IS_DELIVERABLE"],
            }
            computed = workflow.decide_route(task, request, "legacy")
            first = workflow.persist_or_reuse_route_decision(store, task_id, computed)
            decision_path = store._require_task(task_id) / "route-decision.json"
            before = decision_path.read_bytes()
            before_wire = json.loads(before)
            self.assertEqual(FROZEN_ROUTE_DECISION_FIELDS, set(before_wire))
            _install_declaration(
                store, task, allowed_roles=("luna",), active_roles=("luna",)
            )
            reused = workflow.persist_or_reuse_route_decision(store, task_id, first)
            after = decision_path.read_bytes()
            self.assertEqual(before, after)
            self.assertEqual(first.to_dict(), reused.to_dict())
            self.assertEqual(before_wire, json.loads(after))

    def test_cost_fields_are_not_parameters_of_optimization_gate(self):
        signature = inspect.signature(costs.evaluate_optimization_gate)
        self.assertEqual(
            ["metrics", "minimum_cases", "quality_margin_points"],
            list(signature.parameters),
        )
        for forbidden in (
            "cost_estimate",
            "effective_route",
            "rate_snapshot",
            "estimated_cost_minor",
            "total_cost_minor",
        ):
            self.assertNotIn(forbidden, signature.parameters)
        summary = {
            f"case-{index:02d}": {
                "net_measured_cost_delta": -1.0,
                "quality_delta_points": 0.0,
                "measured_attempt_count": 1,
            }
            for index in range(8)
        }
        metrics = {
            "cost_summary": summary,
            "p0_miss_count": 0,
            "p1_miss_count": 0,
            "calibration_first_delivery_pass_rate": 0.5,
            "experiment_first_delivery_pass_rate": 0.6,
            "synthetic": False,
        }
        without = costs.evaluate_optimization_gate(metrics, minimum_cases=8)
        with_cost = costs.evaluate_optimization_gate(
            {
                **metrics,
                "cost_estimate": {
                    "type": "COST_ESTIMATE_UNDER_SNAPSHOT",
                    "total": {"type": "COST_TOTAL_UNDER_SNAPSHOT", "total_cost_minor": 1},
                },
                "effective_route": "luna",
            },
            minimum_cases=8,
        )
        self.assertEqual(without, with_cost)
        self.assertEqual("ALLOW_ENFORCED", without)

    def test_identity_probe_and_evidence_chain_are_outside_runtime_files(self):
        for name in EXCLUDED_RUNTIME_SCRIPTS:
            self.assertNotIn(name, sync_plugin.RUNTIME_FILES)
            self.assertFalse((ROOT / "plugins" / "ai-workflow" / "runtime" / name).exists())
        self.assertTrue((ROOT / "scripts" / "ai_workflow_identity_probe.py").is_file())
        self.assertTrue((ROOT / "scripts" / "ai_workflow_evidence_chain.py").is_file())


class FourDirectPathMissingDeclarationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repo = root / "repository"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "golden@example.test"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Golden Test"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
        self.state_root = root / "state"
        self.store = workflow.WorkflowStore(self.state_root)
        self.task = _workflow_tests.TaskValidationTest().valid_task()
        self.task["repository_root"] = str(self.repo)
        self.store.create_task(self.task)
        self.task_id = str(self.task["task_id"])
        _RecordingPopen.reset()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_run_live_luna_missing_declaration_does_not_call_run_codex(self):
        events = self.store._require_task(self.task_id) / "events.jsonl"
        events.write_text(
            json.dumps({"event_type": "STATE_TRANSITION", "new_state": "DRAFT"}) + "\n",
            encoding="utf-8",
        )
        args = argparse.Namespace(
            allow_live_model=True,
            role="luna",
            root=self.state_root,
            runtime_sessions_dir=self.repo / ".codex" / "sessions",
        )
        with mock.patch.object(workflow, "run_codex") as run_codex:
            with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_DECLARATION_MISSING"):
                workflow._run_live_luna(self.task, args)
        run_codex.assert_not_called()

    def test_team_call_l1_production_controller_missing_declaration_does_not_spawn(self):
        receipt = workflow.run_team_call(
            "team call 核对文件 README.md",
            repository_root=self.repo,
            state_root=self.state_root,
            controller=workflow.TeamCallFakeController(),
        )
        self.assertEqual("DIRECT_L1", receipt.disposition)
        task_id = str(receipt.task_id)
        declaration = self.state_root / task_id / "route-declaration.json"
        self.assertTrue(declaration.is_file())
        declaration.unlink()
        task = workflow.load_task(self.state_root / task_id / "task.json")
        evidence_pin = workflow._team_call_l1_evidence(self.repo, "README.md")
        execution = workflow._team_call_l1_execution(
            task,
            task,
            self.state_root / task_id / "task.json",
            evidence_pin,
            workflow.CODEX_EXEC_ROLE_CONTRACT,
        )
        controller = workflow.TeamCallProductionController(
            self.state_root,
            allow_live_model=True,
            runtime_sessions_dir=self.repo / ".codex" / "sessions",
        )
        popen = mock.Mock()
        with (
            mock.patch.object(
                workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())
            ),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            with self.assertRaisesRegex(
                workflow.WorkflowError,
                "ROUTE_DECLARATION_MISSING|ROUTE_DECLARATION_CORRUPT",
            ):
                controller.run_l1(execution, role="luna")
        popen.assert_not_called()

    def test_construction_runner_missing_declaration_does_not_spawn(self):
        task = remediation_task()
        task_id = str(task["task_id"])
        worktree = self.repo / ".codex-worktrees" / task_id.lower()
        subprocess.run(
            ["git", "worktree", "add", str(worktree), "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        (worktree / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
        task["repository_root"] = str(self.repo)
        task["source_worktree"] = str(worktree)
        task["base_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        frozen = workflow.validate_plan(construction_plan(task=task), task)
        context = workflow.ConstructionExecutionContext(
            plan=frozen,
            step=frozen.tasks[0],
            dispatch_id="d" * 64,
            task_sha256=frozen.task_sha256,
            request_sha256="e" * 64,
            role="luna_construction",
        )
        store = workflow.WorkflowStore(self.state_root)
        store.create_task(task)
        runner = workflow.CodexConstructionRunner(
            self.state_root, worktree / ".codex" / "sessions"
        )

        class Popen(_RecordingPopen):
            _result = _workflow_tests.CodexRunnerTest().valid_result(
                "luna_construction", "IMPLEMENTED_CANDIDATE"
            )

        with (
            mock.patch.object(
                workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())
            ),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow, "_assert_terra_worktree_authorized"),
            mock.patch.object(workflow, "_reject_dirty_input"),
            mock.patch.object(workflow.subprocess, "Popen", Popen),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_DECLARATION_MISSING"):
                runner.run_construction("luna_construction", task, context)
        self.assertEqual([], Popen.calls)

    def test_run_assignment_missing_declaration_does_not_spawn(self):
        from tests.test_ai_workflow_adversarial_acceptance import (
            AcceptanceLedgerV2ContractTest,
        )

        fx = AcceptanceLedgerV2ContractTest()
        fx.setUp()
        try:
            fx.task["source_worktree"] = str(fx.repository_root)
            (fx.repository_root / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
            fx._open_with_owner("luna-owner", "luna")
            reviewer_thread = str(uuid.uuid4())
            review = fx._issue(
                "REVIEW_1",
                fx._expected_actor(
                    "terra-gate-review",
                    "terra_xhigh_reviewer",
                    runtime_instance_id=reviewer_thread,
                ),
            )
            sessions = fx.repository_root.parent / "gate-sessions"
            sessions.mkdir(parents=True, exist_ok=True)
            (sessions / f"rollout-{reviewer_thread}").write_text(
                json.dumps(
                    {
                        "thread_id": reviewer_thread,
                        "agent_type": None,
                        "model": "gpt-5.6-terra",
                        "reasoning_effort": "xhigh",
                        "sandbox_policy": "read-only",
                        "permission_profile": "read-only",
                        "cwd": str(fx.repository_root),
                    }
                ),
                encoding="utf-8",
            )
            (fx.store._require_task(fx.TASK_ID) / "route-declaration.json").unlink()
            _RecordingPopen.reset()
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
                mock.patch.object(
                    repairs.subprocess,
                    "Popen",
                    _compat_popen(controller_process),
                ),
                fx._controller_codex_lookup(),
            ):
                with self.assertRaisesRegex(
                    workflow.WorkflowError,
                    "ROUTE_DECLARATION_MISSING|ROUTE_DECLARATION_CORRUPT",
                ):
                    repairs.run_assignment(fx.store, fx.TASK_ID, review, sessions)
            self.assertEqual([], _RecordingPopen.calls)
        finally:
            fx.tearDown()


class DirectL0NeverReachesModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "state"
        self.repo = Path(self.temporary.name) / "repository"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "l0@example.test"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "L0 Test"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_direct_l0_never_reaches_run_codex_or_consumes_a_permit(self):
        controller = workflow.TeamCallProductionController(self.root)
        with (
            mock.patch.object(workflow, "run_codex") as run_codex,
            mock.patch.object(workflow, "_run_trusted_team_call_l0", wraps=workflow._run_trusted_team_call_l0) as l0,
        ):
            receipt = workflow.run_team_call(
                "team call 检查当前工作区状态",
                repository_root=self.repo,
                state_root=self.root,
                controller=controller,
            )
        self.assertEqual("DIRECT_L0", receipt.disposition)
        self.assertIsNone(receipt.task_id)
        run_codex.assert_not_called()
        l0.assert_called_once()
        self.assertFalse(
            any(path.is_dir() and path.name.startswith("AWF-") for path in self.root.iterdir())
            if self.root.exists()
            else False
        )
        permit_ledgers = list(self.root.rglob("dispatch-permits.jsonl")) if self.root.exists() else []
        self.assertEqual([], permit_ledgers)


class PermitTerminalStateRegressionTest(_DispatchStoreMixin, unittest.TestCase):
    def test_replayed_terminal_permit_cannot_reenter(self):
        self._preflight()
        started = self._require(attempt_id="started")
        with self.store.lock(TASK_ID):
            policy.claim_permit_start_locked(self.store, TASK_ID, started)
        replayed = policy.replay_permit_ledger(self.store, TASK_ID)
        self.assertEqual("STARTED", policy.permit_latest_states(replayed)[started.permit_id])
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_PERMIT_ALREADY_STARTED"
        ):
            self._require(attempt_id="started")

        released = self._require(attempt_id="released")
        with self.store.lock(TASK_ID):
            policy.release_permit_before_start_locked(
                self.store, TASK_ID, released, reason="spawn-failed"
            )
        replayed = policy.replay_permit_ledger(self.store, TASK_ID)
        self.assertEqual(
            "RELEASED_BEFORE_START",
            policy.permit_latest_states(replayed)[released.permit_id],
        )
        with self.assertRaisesRegex(artifacts.WorkflowError, "DISPATCH_IDENTITY_RETIRED"):
            self._require(attempt_id="released")

        orphan = self._require(attempt_id="orphan")
        replayed = policy.replay_permit_ledger(self.store, TASK_ID)
        self.assertEqual("RESERVED", policy.permit_latest_states(replayed)[orphan.permit_id])
        with self.assertRaisesRegex(artifacts.WorkflowError, "DISPATCH_PERMIT_UNCLAIMED"):
            self._require(attempt_id="orphan")


class ReadmeDisclaimerTest(unittest.TestCase):
    def test_readme_keeps_non_measured_cost_winner_disclaimer(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(README_DISCLAIMER, text)
        self.assertNotIn("实测成本赢家", text.replace(README_DISCLAIMER, ""))
        self.assertNotIn("实测推荐", text)


if __name__ == "__main__":
    unittest.main()
