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
        "objective": "route a bounded Terra OS workflow task",
        "repository_root": str(ROOT),
        "source_worktree": None,
        "base_commit": None,
        "candidate_commit": None,
        "authoritative_files": ["README.md"],
        "allowed_write_paths": ["scripts/"],
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

    def test_terra_os_roles_pin_only_the_allowed_model_tiers(self):
        roles = self.config["roles"]
        self.assertEqual(
            ("gpt-5.6-terra", "xhigh", "workspace-write"),
            tuple(roles["terra_xhigh"][key] for key in ("model", "reasoning_effort", "sandbox")),
        )
        for name in ("sol_medium_supervisor", "sol_medium_reviewer"):
            with self.subTest(name=name):
                self.assertEqual(
                    ("gpt-5.6-sol", "medium", "read-only"),
                    tuple(roles[name][key] for key in ("model", "reasoning_effort", "sandbox")),
                )
        self.assertEqual(
            ("gpt-5.6-sol", "xhigh", "read-only"),
            tuple(
                roles["sol_xhigh_planner"][key]
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
    def test_normal_writes_have_the_terra_os_construction_chain(self):
        roles = routing.roles_for_policy(
            valid_task(), route_request("BOUNDED", "WRITE"), "delegated", "terra_os"
        )

        self.assertEqual(
            ("sol_medium_supervisor", "terra_xhigh", "sol_medium_reviewer"), roles
        )
        self.assertTrue(
            {"luna", "terra_medium", "sol_high", "sol_xhigh"}.isdisjoint(roles)
        )

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
                "sol_medium_supervisor",
                "terra_xhigh",
                "sol_medium_reviewer",
            ),
            roles,
        )

    def test_sol_only_policy_uses_the_medium_supervisor(self):
        self.assertEqual(
            ("sol_medium_supervisor",),
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
                    self.assertEqual(("sol_medium_supervisor",), decision.roles)
                    self.assertEqual(
                        (
                            "DECOMPOSABLE_READ_ONLY_ROUTE"
                            if execution_need == "READ_ONLY"
                            else "DECOMPOSABLE_SOL_ONLY_ROUTE"
                        ),
                        decision.rule_id,
                    )

    def test_bounded_and_multi_stage_writes_use_the_terra_os_construction_chain(self):
        for work_class in ("BOUNDED", "MULTI_STAGE"):
            with self.subTest(work_class=work_class):
                decision = workflow.decide_route(
                    valid_task(), route_request(work_class, "WRITE"), "enforced"
                )

                self.assertEqual("delegated", decision.route)
                self.assertEqual(
                    ("sol_medium_supervisor", "terra_xhigh", "sol_medium_reviewer"),
                    decision.roles,
                )

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
            ("sol_medium_supervisor", "terra_xhigh", "sol_medium_reviewer"),
            decision.roles,
        )
        self.assertNotIn("sol_xhigh_planner", decision.roles)

    def test_shadow_preserves_legacy_effective_roles_without_leaking_policy_to_wire(self):
        decision = workflow.decide_route(
            valid_task(), route_request("BOUNDED", "WRITE"), "shadow"
        )

        self.assertEqual(
            ("sol_medium_supervisor", "terra_xhigh", "sol_medium_reviewer"),
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
    def test_new_role_names_are_accepted_by_pinned_role_configuration(self):
        for role in (
            "sol_medium_supervisor",
            "terra_xhigh",
            "sol_medium_reviewer",
            "sol_xhigh_planner",
        ):
            with self.subTest(role=role):
                command = workflow.build_codex_command(
                    role, ROOT, ROOT / "result.json", ROOT / "config" / "ai_workflow_result.schema.json"
                )
                self.assertEqual("codex", command[0])


if __name__ == "__main__":
    unittest.main()
