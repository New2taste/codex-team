import tomllib
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_routing as routing


ROOT = Path(__file__).resolve().parents[1]


def valid_task(*, task_type="REMEDIATION", risk_flags=None):
    return {
        "schema_version": "ai-task-1",
        "task_id": "AWF-20260808-001",
        "task_type": task_type,
        "objective": "implement one bounded parser behavior",
        "repository_root": str(ROOT),
        "source_worktree": None,
        "base_commit": None,
        "candidate_commit": None,
        "authoritative_files": ["README.md"],
        "allowed_write_paths": ["scripts"],
        "forbidden_actions": ["merge", "push"],
        "risk_flags": [] if risk_flags is None else risk_flags,
        "acceptance_commands": ["python -m unittest"],
        "verification_level": "L1",
        "human_gates": ["PLAN_APPROVAL", "EXECUTION_APPROVAL"],
    }


def route_request(work_class, execution_need, *, decomposable=True, risk_flags=None):
    return {
        "schema_version": "ai-route-request-1",
        "task_id": "AWF-20260808-001",
        "work_class": work_class,
        "execution_need": execution_need,
        "decomposable": decomposable,
        "risk_flags": [] if risk_flags is None else risk_flags,
        "reason_codes": ["PLAN_IS_DELIVERABLE"],
    }


def approved_luna_construction_plan(*, owner_role="luna_construction"):
    artifact = "scripts/bounded_fixture.py"
    command = f"/usr/bin/grep -F bounded {artifact}"
    return {
        "schema_version": "ai-plan-1",
        "plan_id": "plan-20260808-luna-construction",
        "task_id": "AWF-20260808-001",
        "goal": "apply one fully bounded construction change",
        "done_when": ["the approved construction step has completed"],
        "tasks": [
            {
                "id": "luna-construction-step",
                "owner_role": owner_role,
                "read_scope": [artifact],
                "write_scope": [artifact],
                "do_not_touch": ["plugins"],
                "depends_on": [],
                "expected_result": "the bounded parser behavior is implemented",
                "verification_commands": [
                    "python -m unittest tests.test_ai_workflow_terra_os"
                ],
                "first_artifact": artifact,
                "evidence_level": "L2",
                "construction_envelope": {
                    "allowed_paths": [artifact],
                    "done_when": {
                        "kind": "TEST",
                        "command": command,
                        "expected_exit": 0,
                        "assertion": "bounded",
                        "artifact": artifact,
                    },
                    "evidence": {
                        "L0": {"kind": "HASH", "artifact": artifact, "sha256": "a" * 64},
                        "L1": {"kind": "COMMAND", "command": command, "expected_exit": 0, "assertion": "bounded", "artifact": artifact},
                        "L2": {"kind": "TEST", "command": command, "expected_exit": 0, "assertion": "bounded", "artifact": artifact},
                    },
                    "negative_checks": [{"kind": "COMMAND", "command": f"/usr/bin/grep -F definitely-absent {artifact}", "expected_exit": 1, "assertion": "exit=1", "artifact": artifact}],
                    "risk_classification": {"kind": "LOCAL_DETERMINISTIC_IMPLEMENTATION", "security": False, "authorization": False, "protocol": False, "control_plane": False},
                },
            }
        ],
        "stages": [["luna-construction-step"]],
    }


class TerraOSConfigTest(unittest.TestCase):
    def setUp(self):
        with (ROOT / "config" / "ai_workflow.toml").open("rb") as handle:
            self.config = tomllib.load(handle)

    def test_terra_os_is_the_enforced_default_with_bounded_repairs(self):
        self.assertEqual(
            {"mode": "enforced", "role_policy": "terra_os"},
            self.config["routing"],
        )
        self.assertEqual(2, self.config["policy"]["max_implementation_reworks"])
        self.assertFalse(self.config["policy"]["automatic_xhigh"])
        self.assertFalse(self.config["policy"]["automatic_sol_high"])
        self.assertFalse(self.config["policy"]["automatic_merge"])
        self.assertFalse(self.config["policy"]["automatic_push"])
        self.assertEqual(
            {
                "terra_max_rounds": 2,
                "round_1_fixer": "terra",
                "round_2_fixer": "terra",
                "post_terra_fixer": "original_sol_medium_reviewer",
                "post_terra_reviewer": "distinct_sol_medium_peer",
            },
            self.config["repair"],
        )

    def test_terra_os_roles_pin_luna_construction_and_terra_xhigh(self):
        roles = self.config["roles"]
        self.assertEqual(
            ("gpt-5.6-luna", "max", "workspace-write"),
            tuple(
                roles["luna_construction"][key]
                for key in ("model", "reasoning_effort", "sandbox")
            ),
        )
        self.assertEqual(
            ("gpt-5.6-terra", "xhigh", "workspace-write"),
            tuple(roles["terra_xhigh"][key] for key in ("model", "reasoning_effort", "sandbox")),
        )
        self.assertEqual(
            ("gpt-5.6-sol", "xhigh", "read-only"),
            tuple(
                roles["sol_xhigh_planner"][key]
                for key in ("model", "reasoning_effort", "sandbox")
            ),
        )
        for role in ("terra_xhigh_planner", "terra_xhigh_reviewer"):
            with self.subTest(role=role):
                self.assertEqual(
                    ("gpt-5.6-terra", "xhigh", "read-only"),
                    tuple(
                        roles[role][key]
                        for key in ("model", "reasoning_effort", "sandbox")
                    ),
                )

    def test_role_policy_resolution_is_closed_and_honors_explicit_override(self):
        self.assertEqual("terra_os", routing.resolve_role_policy(self.config))
        self.assertEqual("legacy", routing.resolve_role_policy(self.config, override="legacy"))
        with self.assertRaisesRegex(workflow.WorkflowError, "ROLE_POLICY_INVALID"):
            routing.resolve_role_policy(self.config, override="unreviewed")
        with self.assertRaisesRegex(workflow.WorkflowError, "ROLE_POLICY_INVALID"):
            routing.resolve_role_policy({"routing": {} })


class TerraOSRolePolicyTest(unittest.TestCase):
    def test_normal_writes_default_to_the_terra_xhigh_construction_owner(self):
        roles = routing.roles_for_policy(
            valid_task(), route_request("BOUNDED", "WRITE"), "delegated", "terra_os"
        )

        self.assertEqual(("terra_xhigh",), roles)
        self.assertTrue(
            {
                "luna_construction",
                "sol_medium_supervisor",
                "sol_medium_reviewer",
                "terra_medium",
                "sol_high",
                "sol_xhigh",
            }.isdisjoint(roles)
        )

    def test_only_a_verified_luna_construction_envelope_can_select_luna(self):
        decision = workflow.decide_route(
            valid_task(),
            route_request("BOUNDED", "WRITE"),
            "enforced",
            construction_plan=approved_luna_construction_plan(),
            construction_step_id="luna-construction-step",
        )

        self.assertEqual("delegated", decision.route)
        self.assertEqual(("luna_construction",), decision.roles)
        self.assertNotIn("construction_envelope", decision.to_dict())

    def test_missing_or_invalid_luna_envelope_fails_closed_to_terra(self):
        incomplete = approved_luna_construction_plan()
        del incomplete["tasks"][0]["construction_envelope"]["negative_checks"]
        out_of_scope = approved_luna_construction_plan()
        out_of_scope["tasks"][0]["construction_envelope"]["allowed_paths"] = ["README.md"]

        for plan, step_id in (
            (None, None),
            (incomplete, "luna-construction-step"),
            (out_of_scope, "luna-construction-step"),
            (approved_luna_construction_plan(owner_role="terra_xhigh"), "luna-construction-step"),
        ):
            with self.subTest(plan=plan is not None, step_id=step_id):
                decision = workflow.decide_route(
                    valid_task(),
                    route_request("BOUNDED", "WRITE"),
                    "enforced",
                    construction_plan=plan,
                    construction_step_id=step_id,
                )

                self.assertEqual(("terra_xhigh",), decision.roles)

    def test_unselectable_terra_medium_and_sol_high_owner_roles_are_rejected(self):
        for owner_role in ("terra_medium", "sol_high"):
            with self.subTest(owner_role=owner_role):
                plan = approved_luna_construction_plan(owner_role=owner_role)
                del plan["tasks"][0]["construction_envelope"]

                with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
                    workflow.validate_plan(plan, valid_task())

    def test_authorized_large_project_chain_is_pure_policy_only(self):
        roles = routing.roles_for_policy(
            valid_task(),
            route_request("MULTI_STAGE", "WRITE"),
            routing.OWNER_AUTHORIZED_LARGE_PROJECT_ROUTE,
            "terra_os",
        )

        self.assertEqual(
            (
                "sol_xhigh_planner",
                "terra_xhigh",
            ),
            roles,
        )

    def test_sol_only_policy_uses_read_only_terra_xhigh_not_ordinary_sol_medium(self):
        self.assertEqual(
            ("terra_xhigh_planner",),
            routing.roles_for_policy(
                valid_task(), route_request("PLANNING_ONLY", "READ_ONLY"), "sol_only", "terra_os"
            ),
        )

    def test_bounded_and_multi_stage_nonwrite_work_are_sol_only(self):
        for work_class in ("BOUNDED", "MULTI_STAGE"):
            for execution_need in ("NONE", "READ_ONLY"):
                with self.subTest(work_class=work_class, execution_need=execution_need):
                    decision = workflow.decide_route(
                        valid_task(), route_request(work_class, execution_need), "enforced"
                    )

                    self.assertEqual("sol_only", decision.route)
                    self.assertEqual(("terra_xhigh_planner",), decision.roles)
                    self.assertEqual(
                        (
                            "DECOMPOSABLE_READ_ONLY_ROUTE"
                            if execution_need == "READ_ONLY"
                            else "DECOMPOSABLE_SOL_ONLY_ROUTE"
                        ),
                        decision.rule_id,
                    )

    def test_bounded_and_multi_stage_writes_use_terra_without_an_envelope(self):
        for work_class in ("BOUNDED", "MULTI_STAGE"):
            with self.subTest(work_class=work_class):
                decision = workflow.decide_route(
                    valid_task(), route_request(work_class, "WRITE"), "enforced"
                )

                self.assertEqual("delegated", decision.route)
                self.assertEqual(("terra_xhigh",), decision.roles)

    def test_security_and_open_ended_work_cannot_gain_luna_from_an_envelope(self):
        security_task = valid_task(risk_flags=["SECURITY"])
        security_request = route_request("BOUNDED", "WRITE", risk_flags=["SECURITY"])
        for request, task in (
            (security_request, security_task),
            (route_request("MULTI_STAGE", "WRITE"), valid_task()),
            (route_request("BOUNDED", "WRITE"), valid_task(task_type="PLAN")),
        ):
            with self.subTest(work_class=request["work_class"], risk_flags=task["risk_flags"]):
                decision = workflow.decide_route(
                    task,
                    request,
                    "enforced",
                    construction_plan=approved_luna_construction_plan(),
                    construction_step_id="luna-construction-step",
                )

                self.assertEqual(("terra_xhigh",), decision.roles)

    def test_non_decomposable_bounded_and_multi_stage_work_is_blocked(self):
        for work_class in ("BOUNDED", "MULTI_STAGE"):
            for execution_need in ("NONE", "READ_ONLY", "WRITE"):
                for risk_flags in ([], ["SECURITY"]):
                    with self.subTest(
                        work_class=work_class,
                        execution_need=execution_need,
                        risk_flags=risk_flags,
                    ):
                        decision = workflow.decide_route(
                            valid_task(risk_flags=risk_flags),
                            route_request(
                                work_class,
                                execution_need,
                                decomposable=False,
                                risk_flags=risk_flags,
                            ),
                            "enforced",
                        )

                        self.assertEqual("blocked", decision.route)
                        self.assertEqual((), decision.roles)

    def test_direct_and_blocked_routes_never_start_a_model(self):
        for route_name in ("direct", "blocked"):
            with self.subTest(route_name=route_name):
                self.assertEqual(
                    (),
                    routing.roles_for_policy(
                        valid_task(), route_request("SIMPLE", "NONE"), route_name, "terra_os"
                    ),
                )

    def test_legacy_policy_keeps_the_frozen_luna_first_chain(self):
        self.assertEqual(
            ("luna", "sol_planner"),
            routing.roles_for_policy(
                valid_task(task_type="PLAN"),
                route_request("PLANNING_ONLY", "READ_ONLY"),
                "delegated",
                "legacy",
            ),
        )

    def test_public_facade_never_guesses_large_project_authorization(self):
        decision = workflow.decide_route(
            valid_task(), route_request("MULTI_STAGE", "WRITE"), "enforced"
        )

        self.assertEqual(
            ("terra_xhigh",),
            decision.roles,
        )
        self.assertNotIn("sol_xhigh_planner", decision.roles)

    def test_shadow_preserves_legacy_effective_roles_without_leaking_policy_to_wire(self):
        decision = workflow.decide_route(
            valid_task(), route_request("BOUNDED", "WRITE"), "shadow"
        )

        self.assertEqual(
            ("terra_xhigh",),
            decision.roles,
        )
        self.assertEqual(("terra", "luna", "sol_reviewer"), decision.effective_roles)
        self.assertNotIn("role_policy", decision.to_dict())

    def test_route_cli_default_comes_from_the_loaded_config(self):
        config = {"routing": {"mode": "shadow", "role_policy": "legacy"}}
        with mock.patch.object(workflow, "_load_workflow_config", return_value=config):
            parser = workflow.build_parser()
        args = parser.parse_args(
            ["route", "--task", "task.json", "--request", "request.json"]
        )

        self.assertEqual("shadow", args.mode)


class TerraOSExecutionGuardTest(unittest.TestCase):
    def test_luna_construction_cannot_bypass_the_verified_plan_step_at_launch(self):
        paths = workflow.RunPaths(
            repo=ROOT,
            output_path=ROOT / ".luna-construction-should-not-run.json",
            schema_path=ROOT / "config" / "ai_workflow_result.schema.json",
            logs_dir=ROOT / ".luna-construction-should-not-run-logs",
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "LUNA_ENVELOPE_INVALID"):
            workflow.run_codex("luna_construction", valid_task(), "bounded", paths)

    def test_luna_construction_role_closes_plan_result_runtime_and_cost_validation(self):
        result = workflow.FakeRunner().run("luna_construction", valid_task())
        result["changed_files"] = ["scripts/ai_workflow.py"]
        workflow.validate_role_result("luna_construction", result, {"scripts/ai_workflow.py"})
        workflow.validate_runtime_evidence(
            {
                "schema_version": "runtime-evidence-1",
                "attempt_id": "luna-construction-attempt",
                "requested_role": "luna_construction",
                "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
                "observed_agent_type": None,
                "observed_model": "gpt-5.6-luna",
                "observed_reasoning_effort": "max",
                "observed_sandbox_policy": "workspace-write",
                "observed_permission_profile": "workspace-write",
                "observed_cwd": str(ROOT),
                "evidence_source": "LOCAL_ROLLOUT",
                "observed_at_utc": "2026-08-08T00:00:00Z",
                "verification_status": "VERIFIED",
                "failure_reasons": [],
            }
        )
        workflow.validate_cost_evidence(
            {
                "schema_version": "cost-evidence-1",
                "route": "delegated",
                "role": "luna_construction",
                "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
                "duration_seconds": 1.0,
                "prompt_bytes": 1,
                "input_tokens": None,
                "cached_input_tokens": None,
                "output_tokens": None,
                "retry_kind": "none",
                "verification_seconds": 0.0,
                "quality_outcome": "IMPLEMENTED_CANDIDATE",
                "paired_case_id": None,
                "evidence_class": "unavailable",
                "rate_snapshot_id": None,
            }
        )

    def test_new_role_names_are_accepted_by_pinned_role_configuration(self):
        for role in (
            "luna_construction",
            "terra_xhigh",
            "terra_xhigh_planner",
            "terra_xhigh_reviewer",
            "sol_xhigh_planner",
        ):
            with self.subTest(role=role):
                command = workflow.build_codex_command(
                    role, ROOT, ROOT / "result.json", ROOT / "config" / "ai_workflow_result.schema.json"
                )
                self.assertEqual("codex", command[0])


if __name__ == "__main__":
    unittest.main()
