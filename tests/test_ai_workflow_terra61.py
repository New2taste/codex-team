"""Behavioral boundaries, dispatch identity and persisted 0.4 compatibility."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_routing as routing
from scripts import ai_workflow_ownership as ownership
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_scheduler as scheduler
from tests.test_ai_workflow_construction_execution import (
    BoundConstructionRunner, construction_plan, remediation_task, route_request,
)
from tests.test_ai_workflow_routing_v2 import valid_task
from tests.test_ai_workflow_runtime import runtime_expected, runtime_observation
from tests.test_ai_workflow_costs import cost_record


SCOPE = "tests/fixtures/paired-cases.json"


def routine_request(task, *, planning=False):
    request = route_request(task)
    request["reason_codes"] = ["ROUTINE_LOCAL_CONFIRMED", "SOURCE_INSPECTION_REQUIRED"]
    if planning:
        request.update(work_class="PLANNING_ONLY", execution_need="READ_ONLY")
    return request


def routine_construction():
    task = remediation_task(paths=[SCOPE])
    task["authoritative_files"] = [SCOPE]
    plan = construction_plan(task=task, scope=SCOPE, owner_role="terra61_worker")
    return task, plan, routine_request(task)


class Terra61PolicyTest(unittest.TestCase):
    def test_explicit_simple_plan_selects_actual_terra_medium_without_sol_chain(self):
        task = valid_task()
        decision = workflow.decide_route(task, routine_request(task, planning=True))
        self.assertEqual(("terra61_planner",), decision.effective_roles)
        self.assertEqual("BALANCED_TERRA61_ROUTINE_PLAN", decision.rule_id)
        config = workflow._load_workflow_config()
        for role in ("terra61_planner", "terra61_worker"):
            self.assertEqual("gpt-6.1-terra", config["roles"][role]["model"])
            self.assertEqual("medium", config["roles"][role]["reasoning_effort"])
        for role in ("terra", "terra_xhigh", "terra_xhigh_planner", "terra_xhigh_reviewer"):
            self.assertEqual("gpt-6.1-sol", config["roles"][role]["model"])

    def test_local_write_needs_real_validated_step_not_a_routine_label(self):
        task, plan, request = routine_construction()
        self.assertEqual(("terra_xhigh",), workflow.decide_route(task, request).roles)
        decision = workflow.decide_route(
            task, request, construction_plan=plan, construction_step_id="construction-601"
        )
        self.assertEqual(("terra61_worker",), decision.roles)
        self.assertEqual("BALANCED_TERRA61_ROUTINE_WRITE", decision.rule_id)
        for edit in ("missing_source", "directory_scope", "multiple_steps", "wrong_owner"):
            bad_task, bad_plan = copy.deepcopy(task), copy.deepcopy(plan)
            if edit == "missing_source":
                bad_task["authoritative_files"] = ["README.md"]
            elif edit == "directory_scope":
                bad_plan["tasks"][0]["write_scope"] = ["tests/fixtures"]
            elif edit == "multiple_steps":
                extra = copy.deepcopy(bad_plan["tasks"][0])
                extra.update(id="second", write_scope=[])
                bad_plan["tasks"].append(extra)
                bad_plan["stages"] = [["construction-601"], ["second"]]
            else:
                bad_plan["tasks"][0]["owner_role"] = "terra_xhigh"
            with self.subTest(edit=edit):
                self.assertEqual(("terra_xhigh",), workflow.decide_route(
                    bad_task, request, construction_plan=bad_plan,
                    construction_step_id="construction-601",
                ).roles)

    def test_missing_or_contradictory_classification_stays_sol(self):
        task = valid_task()
        request = routine_request(task, planning=True)
        variants = [[], ["SOURCE_INSPECTION_REQUIRED"],
                    request["reason_codes"] + ["DEPENDENT_STEPS_REQUIRED"],
                    request["reason_codes"] + ["MATERIAL_CONSEQUENCE_PRESENT"]]
        for reasons in variants[1:]:
            with self.subTest(reasons=reasons):
                changed = {**request, "reason_codes": reasons}
                self.assertEqual(("terra_xhigh_planner",), workflow.decide_route(task, changed).roles)
        request["work_class"] = "MULTI_STAGE"
        self.assertEqual(("terra_xhigh_planner",), workflow.decide_route(task, request).roles)

    def test_each_complexity_risk_wins_over_routine_classification(self):
        for risk in ("CROSS_MODULE", "STATE_MACHINE", "PERMISSIONS", "CONCURRENCY", "MIGRATION", "SECURITY"):
            task = valid_task(risk_flags=[risk])
            request = routine_request(task, planning=True)
            request["risk_flags"] = [risk]
            with self.subTest(risk=risk):
                self.assertEqual(("terra_xhigh_planner",), workflow.decide_route(task, request).roles)

    def test_acceptance_selects_sol_high_even_if_caller_claims_routine(self):
        task = valid_task(task_type="ACCEPTANCE")
        request = routine_request(task, planning=True)
        for work_class in ("SIMPLE", "BOUNDED", "PLANNING_ONLY"):
            request["work_class"] = work_class
            decision = workflow.decide_route(task, request)
            self.assertEqual(("sol_reviewer",), decision.roles)
            self.assertEqual("BALANCED_SOL_HIGH_FINAL_ACCEPTANCE", decision.rule_id)

    def test_luna_verified_mechanical_envelope_remains_max(self):
        task = remediation_task()
        plan = construction_plan(task=task)
        request = route_request(task)
        self.assertEqual(("luna_construction",), workflow.decide_route(
            task, request, construction_plan=plan, construction_step_id="construction-601"
        ).roles)
        self.assertEqual("max", workflow._load_workflow_config()["roles"]["luna_construction"]["reasoning_effort"])

    def test_new_roles_are_cost_accounted_without_relabeling_legacy_roles(self):
        for role in ("terra61_worker", "terra61_planner", "terra_xhigh"):
            evidence = workflow.normalize_cost_evidence(cost_record(role=role))
            self.assertEqual(role, evidence.role)
            self.assertIsNone(evidence.input_tokens)

    def test_runtime_requires_real_terra_not_historical_sol_identity(self):
        for role, sandbox in (("terra61_planner", "read-only"), ("terra61_worker", "workspace-write")):
            expected = runtime_expected(requested_role=role, model="gpt-6.1-terra", reasoning_effort="medium", sandbox_policy=sandbox, permission_profile=sandbox, hard_read_only=sandbox == "read-only")
            observed = runtime_observation(model="gpt-6.1-terra", reasoning_effort="medium", sandbox_policy=sandbox, permission_profile=sandbox)
            evidence = workflow.verify_runtime_identity(expected, observed)
            self.assertEqual("gpt-6.1-terra", evidence.observed_model)
            with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_IDENTITY_CONFLICT"):
                workflow.verify_runtime_identity(expected, {**observed, "model": "gpt-6.1-sol"})


class Terra61PersistenceTest(unittest.TestCase):
    def test_production_planner_prompt_carries_verified_frozen_routine_classification(self):
        task = valid_task()
        request = routine_request(task, planning=True)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = workflow.WorkflowStore(root)
            store.create_task(task)
            decision = workflow.decide_route(task, request)
            workflow.record_route_decision(store, task["task_id"], decision)
            with mock.patch.object(workflow, "run_codex", return_value={}) as run:
                workflow.CodexPlanningRunner(root, root).run("terra61_planner", task)
            prompt = run.call_args.args[2]
            self.assertIn('"routine_classification"', prompt)
            self.assertIn('"classification":"ROUTINE_LOW_RISK_LOCAL"', prompt)
            self.assertIn(decision.request_sha256, prompt)
            self.assertIn(decision.rule_id, prompt)
            self.assertIn("ROUTINE_LOCAL_CONFIRMED", prompt)
            # A role name alone must not manufacture classifier authority.
            broken = decision.to_dict()
            broken["request_sha256"] = "0" * 64
            (root / task["task_id"] / "route-decision.json").write_text(json.dumps(broken))
            with mock.patch.object(workflow, "run_codex") as run:
                with self.assertRaisesRegex(workflow.WorkflowError, "TERRA_OS_ROUTE_MISMATCH"):
                    workflow.CodexPlanningRunner(root, root).run("terra61_planner", task)
                run.assert_not_called()

    def test_frozen_worker_prompt_carries_same_classification_and_reconciles(self):
        task, plan, request = routine_construction()
        frozen = workflow.validate_plan(plan, task)
        context = workflow.ConstructionExecutionContext(
            plan=frozen, step=frozen.tasks[0], dispatch_id="routine-dispatch",
            task_sha256=artifacts.artifact_sha256(task),
            request_sha256=artifacts.artifact_sha256(request), role="terra61_worker",
        )
        built = workflow.build_construction_role_prompt_result(task, context)
        self.assertIn('"routine_classification"', built.prompt)
        self.assertIn('"classification":"ROUTINE_LOW_RISK_LOCAL"', built.prompt)
        self.assertIn(context.request_sha256, built.prompt)
        self.assertEqual("full", workflow._reconcile_construction_prompt(task, context, built.prompt, built))

    def test_uncreated_task_worktree_preflight_anchors_to_original_repo_only(self):
        from scripts import ai_workflow_preflight as preflight
        task = remediation_task()
        task["source_worktree"] = str(workflow._expected_worktree(task))
        self.assertEqual(Path(task["repository_root"]), preflight._envelope_repo(task))
        task["source_worktree"] = "/private/tmp/not-a-bound-worktree"
        self.assertEqual(Path(task["source_worktree"]), preflight._envelope_repo(task))

    def test_old_route_restores_sol_and_new_route_restores_terra_without_request(self):
        task = valid_task()
        request = routine_request(task, planning=True)
        for policy, expected in (("terra_os", "terra_xhigh_planner"), ("balanced_terra61", "terra61_planner")):
            decision = routing.decide_route(task, request, "enforced", role_policy=policy)
            restored = workflow._runtime_decision_from_stored(decision.to_dict(), task)
            self.assertEqual((expected,), restored.effective_roles)
            self.assertEqual(decision.to_dict(), restored.to_dict())

    def test_old_construction_recovers_old_owner_under_new_install_default(self):
        task, plan, request = routine_construction()
        plan["tasks"][0]["owner_role"] = "terra_xhigh"
        decision = routing.decide_route(task, request, "enforced", role_policy="terra_os")
        with tempfile.TemporaryDirectory() as folder:
            store = workflow.WorkflowStore(Path(folder))
            store.create_task(task)
            workflow.record_route_decision(store, task["task_id"], decision)
            _, _, _, role, wire = workflow._load_enforced_construction_artifacts(
                store, task["task_id"], plan, request, "construction-601"
            )
            self.assertEqual("terra_xhigh", role)
            self.assertEqual(decision.rule_id, wire["rule_id"])

    def test_routine_construction_runs_selected_owner_and_freezes_authority(self):
        task, plan, request = routine_construction()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = workflow.WorkflowStore(root)
            store.create_task(task)
            frozen = workflow.validate_plan(plan, task)
            registry = ownership.build_ownership_registry(
                task_id=task["task_id"], envelope_hash=artifacts.artifact_sha256(task),
                plan=frozen, registered_at_utc="2026-10-29T00:00:00Z",
            )
            with store.lock(task["task_id"]):
                ownership.record_ownership_registry(store, task["task_id"], registry)
            runner = BoundConstructionRunner()
            def run():
                return workflow.run_until_gate(
                    task["task_id"], construction_plan=plan, construction_request=request,
                    construction_step_id="construction-601", construction_attempt=1,
                    runner=runner, allow_live_model=False, state_root=root,
                )
            self.assertEqual("AWAITING_OWNER_DECISION", run())
            self.assertEqual([], runner.calls)
            workflow._apply_owner_decision(store, task["task_id"], "approve_execution", "owner")
            self.assertEqual("IMPLEMENTED_CANDIDATE", run())
            self.assertEqual("terra61_worker", runner.calls[0][0])
            self.assertEqual("IMPLEMENTED_CANDIDATE", run())
            self.assertEqual(1, len(runner.calls))
            declaration = json.loads((root / task["task_id"] / "route-declaration.json").read_text())
            self.assertEqual(["terra61_worker"], declaration["active_roles"])
            changed_plan = copy.deepcopy(plan)
            changed_plan["tasks"][0]["owner_role"] = "terra_xhigh"
            with self.assertRaisesRegex(workflow.WorkflowError, "CONSTRUCTION_ROUTE_MISMATCH"):
                workflow._load_enforced_construction_artifacts(store, task["task_id"], changed_plan, request, "construction-601")

    def test_scheduler_counts_terra_as_writer_not_reader(self):
        from types import SimpleNamespace
        plan = SimpleNamespace(tasks=(SimpleNamespace(id="terra", owner_role="terra61_worker"), SimpleNamespace(id="sol", owner_role="terra_xhigh")))
        self.assertEqual(("terra",), scheduler._select_ready(plan, ("terra", "sol"), {}, 2, 1, 2))

    def test_provider_access_blockers_are_terminal_not_technical_retries(self):
        for text, code in (("invalid_api_key", "CREDENTIALS_UNAVAILABLE"), ("model_not_found", "MODEL_UNAVAILABLE")):
            self.assertEqual(code, workflow.provider_access_blocker(json.dumps({"type": "error", "message": text}), ""))
            self.assertIn(code, workflow.ROLE_GUARD_FAILURES)
            self.assertIsNone(workflow.provider_access_blocker(json.dumps({"type": "item.completed", "message": text}), ""))

    def test_actual_workspace_out_of_credits_event_is_terminal_but_body_is_not(self):
        message = "Your workspace is out of credits. Ask your workspace owner to refill."
        self.assertTrue(workflow.model_quota_exhausted(json.dumps({"type": "error", "message": message}), ""))
        self.assertFalse(workflow.model_quota_exhausted(json.dumps({"type": "item.completed", "message": message}), ""))

    def test_live_cli_plan_uses_selected_planner_not_luna_evidence_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "task.json"
            path.write_text(json.dumps(valid_task()))
            with mock.patch.object(workflow, "run_until_gate", return_value="AWAITING_OWNER_DECISION") as run, mock.patch.object(workflow, "_run_live_luna") as luna, mock.patch.object(workflow, "_load_enforced_read_only_route_role", return_value="terra61_planner"):
                self.assertEqual(0, workflow.main([
                    "run", str(path), "--root", folder, "--runner", "live",
                    "--allow-live-model", "--runtime-sessions-dir", folder, "--role", "terra61_planner",
                ]))
                self.assertIsInstance(run.call_args.kwargs["runner"], workflow.CodexPlanningRunner)
                luna.assert_not_called()


if __name__ == "__main__":
    unittest.main()
