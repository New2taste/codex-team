import hashlib
import io
import json
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_costs as costs
from scripts import ai_workflow_router_probe as probe
from scripts import sync_plugin


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "config" / "ai_workflow_router_probe_manifest.schema.json"
FIXTURE = ROOT / "tests" / "fixtures" / "router-probe" / "cases.json"


class RouterProbeContractTest(unittest.TestCase):
    def test_manifest_schema_closes_models_arms_conditions_and_properties(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        properties = schema["properties"]
        self.assertEqual("router-probe-manifest-1", properties["schema_version"]["const"])
        arm = properties["arms"]["items"]
        self.assertFalse(arm["additionalProperties"])
        self.assertEqual(
            {
                "luna_resident",
                "sol_resident",
                "terra_resident",
                "luna_control_fresh",
                "sol_control_fresh",
                "terra_control_fresh",
            },
            set(arm["properties"]["arm_id"]["enum"]),
        )
        self.assertEqual(
            {"gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"},
            set(arm["properties"]["model"]["enum"]),
        )
        self.assertEqual(
            {"resident", "cold_control"},
            set(arm["properties"]["cache_condition"]["enum"]),
        )
        self.assertEqual(set(properties), set(schema["required"]))

    def test_router_probe_is_cost_only_and_not_a_production_role(self):
        self.assertNotIn("router_probe", artifacts.ROLES)
        normalized = costs.normalize_cost_evidence(
            {
                "schema_version": "cost-evidence-1",
                "route": "direct",
                "role": "router_probe",
                "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
                "duration_seconds": 0.1,
                "prompt_bytes": 100,
                "input_tokens": 10,
                "cached_input_tokens": 5,
                "output_tokens": 2,
                "retry_kind": "none",
                "verification_seconds": 0,
                "quality_outcome": "MATCH",
                "paired_case_id": "router-case-1",
                "evidence_class": "measured",
                "rate_snapshot_id": None,
            }
        )
        self.assertEqual("router_probe", normalized.role)

    def test_router_probe_config_is_disabled_and_has_exact_three_family_matrix(self):
        config_path = ROOT / "config" / "ai_workflow.toml"
        config = tomllib.loads(
            config_path.read_text(encoding="utf-8")
        )
        probe_config = config["router_probe"]
        self.assertFalse(probe_config["enabled"])
        self.assertEqual("router-probe-v1", probe_config["prompt_template_version"])
        self.assertEqual(32, probe_config["minimum_paired_cases"])
        self.assertEqual(
            {
                "luna": {
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "max",
                },
                "sol": {
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "medium",
                },
                "terra": {
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "xhigh",
                },
            },
            probe_config["models"],
        )
        loaded = probe.load_probe_configuration(config_path)
        for family in ("luna", "sol", "terra"):
            model = loaded["models"][family]
            self.assertEqual(
                (model["model"], model["reasoning_effort"], "resident"),
                probe.ARM_CONTRACTS[f"{family}_resident"],
            )
            self.assertEqual(
                (model["model"], model["reasoning_effort"], "cold_control"),
                probe.ARM_CONTRACTS[f"{family}_control_fresh"],
            )

    def test_probe_schema_is_in_plugin_distribution_manifest(self):
        self.assertIn(
            "ai_workflow_router_probe_manifest.schema.json",
            sync_plugin.CONFIG_FILES,
        )
        with tempfile.TemporaryDirectory() as temporary:
            changed = sync_plugin.synchronize(ROOT, write=False)
        self.assertEqual((), changed)


class RouterProbeRunnerTest(unittest.TestCase):
    class FakeExecutor:
        data_origin = "synthetic"

        def __init__(self, expected_routes):
            self.expected_routes = expected_routes
            self.calls = []

        def run(
            self,
            *,
            model,
            reasoning_effort,
            prompt,
            arm_id,
            case_id,
        ):
            self.calls.append((arm_id, case_id, model, reasoning_effort, prompt))
            cached = 80 if arm_id.endswith("_resident") else 0
            return probe.ProbeAttempt(
                input_tokens=100,
                cached_input_tokens=cached,
                output_tokens=10,
                duration_seconds=0.25,
                recommended_route=self.expected_routes[case_id],
            )

    def manifest(self):
        return json.loads(FIXTURE.read_text(encoding="utf-8"))

    def write_manifest(self, root, value):
        path = root / "manifest.json"
        path.write_text(
            json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return path

    def test_manifest_validator_rejects_unknown_mismatched_and_extra_values(self):
        mutations = []
        unknown_arm = self.manifest()
        unknown_arm["arms"][0]["arm_id"] = "new_model_resident"
        mutations.append(("unknown arm", unknown_arm))
        mismatched_model = self.manifest()
        mismatched_model["arms"][0]["model"] = "gpt-5.6-sol"
        mutations.append(("mismatched model", mismatched_model))
        unknown_condition = self.manifest()
        unknown_condition["arms"][0]["cache_condition"] = "thread_resident"
        mutations.append(("unknown condition", unknown_condition))
        extra = self.manifest()
        extra["arms"][0]["extra"] = True
        mutations.append(("extra field", extra))
        duplicate_case = self.manifest()
        duplicate_case["cases"][1]["case_id"] = duplicate_case["cases"][0]["case_id"]
        mutations.append(("duplicate case", duplicate_case))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for label, value in mutations:
                with self.subTest(label=label), self.assertRaises(
                    probe.RouterProbeError
                ):
                    probe.load_probe_manifest(self.write_manifest(root, value))

    def test_resident_prefix_is_stable_and_cold_control_prefix_is_case_bound(self):
        manifest = probe.load_probe_manifest(FIXTURE)
        first, second = manifest["cases"][:2]
        resident_first = probe.build_probe_prompt(
            first,
            template_version=manifest["prompt_template_version"],
            cache_condition="resident",
        )
        resident_second = probe.build_probe_prompt(
            second,
            template_version=manifest["prompt_template_version"],
            cache_condition="resident",
        )
        cold_first = probe.build_probe_prompt(
            first,
            template_version=manifest["prompt_template_version"],
            cache_condition="cold_control",
        )
        cold_second = probe.build_probe_prompt(
            second,
            template_version=manifest["prompt_template_version"],
            cache_condition="cold_control",
        )

        self.assertEqual(
            probe.prompt_prefix_sha256(resident_first),
            probe.prompt_prefix_sha256(resident_second),
        )
        self.assertNotEqual(
            probe.prompt_prefix_sha256(cold_first),
            probe.prompt_prefix_sha256(cold_second),
        )
        self.assertTrue(resident_first.endswith(first["intake"]))
        self.assertTrue(resident_second.endswith(second["intake"]))

    def test_fake_batch_writes_atomic_manifest_and_normalized_cost_rows(self):
        manifest = probe.load_probe_manifest(FIXTURE)
        expected = {
            case["case_id"]: case["expected_route"] for case in manifest["cases"]
        }
        executor = self.FakeExecutor(expected)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "experiments"
            output_root.mkdir()
            summary = probe.run_probe_batch(
                manifest,
                executor=executor,
                output_root=output_root,
            )
            batch = output_root / manifest["batch_id"]
            manifest_rows = [
                json.loads(line)
                for line in (batch / "manifest.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            cost_rows = [
                json.loads(line)
                for line in (batch / "cost-evidence.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            analysis = json.loads(
                (batch / "summary.json").read_text(encoding="utf-8")
            )
            report = (batch / "report.txt").read_text(encoding="utf-8")
            with self.assertRaises(probe.RouterProbeError):
                probe.run_probe_batch(
                    manifest,
                    executor=self.FakeExecutor(expected),
                    output_root=output_root,
                )

        self.assertEqual(24, summary["attempt_count"])
        self.assertEqual(24, len(executor.calls))
        self.assertEqual(24, len(manifest_rows))
        self.assertEqual(24, len(cost_rows))
        self.assertEqual(24, len({row["attempt_id"] for row in manifest_rows}))
        resident = [
            row for row in manifest_rows if row["arm_id"].endswith("_resident")
        ]
        for arm_id in {row["arm_id"] for row in resident}:
            conditions = [
                row["cache_condition"]
                for row in resident
                if row["arm_id"] == arm_id
            ]
            self.assertEqual(1, conditions.count("cold_start"))
            self.assertEqual(3, conditions.count("warm"))
        self.assertTrue(
            all(
                row["cache_condition"] == "cold_control"
                for row in manifest_rows
                if row["arm_id"].endswith("_control_fresh")
            )
        )
        self.assertTrue(
            all(row["cost_evidence"]["role"] == "router_probe" for row in cost_rows)
        )
        self.assertTrue(
            all(
                row["cost_evidence"]["evidence_class"] == "unavailable"
                for row in cost_rows
            )
        )
        self.assertEqual("OBSERVATION_ONLY", analysis["decision"])
        self.assertIn("effective_route=UNCHANGED", report)

    def test_executor_origin_cannot_be_promoted_by_manifest_claim(self):
        manifest = probe.load_probe_manifest(FIXTURE)
        manifest["data_origin"] = "measured"
        expected = {
            case["case_id"]: case["expected_route"] for case in manifest["cases"]
        }
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "experiments"
            output_root.mkdir()
            with self.assertRaises(probe.RouterProbeError):
                probe.run_probe_batch(
                    manifest,
                    executor=self.FakeExecutor(expected),
                    output_root=output_root,
                )

    def test_output_root_must_be_absolute_non_symlink_and_outside_dot_git(self):
        manifest = probe.load_probe_manifest(FIXTURE)
        expected = {
            case["case_id"]: case["expected_route"] for case in manifest["cases"]
        }
        executor = self.FakeExecutor(expected)
        with self.assertRaises(probe.RouterProbeError):
            probe.run_probe_batch(
                manifest, executor=executor, output_root=Path("relative")
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            git_root = root / ".git" / "experiments"
            git_root.mkdir(parents=True)
            with self.assertRaises(probe.RouterProbeError):
                probe.run_probe_batch(
                    manifest, executor=executor, output_root=git_root
                )
            repository = root / "repository"
            (repository / ".git").mkdir(parents=True)
            repository_output = repository / "experiments"
            repository_output.mkdir()
            with self.assertRaises(probe.RouterProbeError):
                probe.run_probe_batch(
                    manifest,
                    executor=executor,
                    output_root=repository_output,
                )
            alias = root / "repository-alias"
            alias.symlink_to(repository, target_is_directory=True)
            with self.assertRaises(probe.RouterProbeError):
                probe.run_probe_batch(
                    manifest,
                    executor=executor,
                    output_root=alias / "experiments",
                )
            actual = root / "actual"
            actual.mkdir()
            linked = root / "linked"
            linked.symlink_to(actual, target_is_directory=True)
            with self.assertRaises(probe.RouterProbeError):
                probe.run_probe_batch(
                    manifest, executor=executor, output_root=linked
                )

    @mock.patch(
        "scripts.ai_workflow_router_probe.os.rename",
        side_effect=OSError("simulated publish race"),
    )
    def test_atomic_publish_oserror_is_bounded(self, _rename):
        manifest = probe.load_probe_manifest(FIXTURE)
        expected = {
            case["case_id"]: case["expected_route"] for case in manifest["cases"]
        }
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "experiments"
            output_root.mkdir()
            with self.assertRaisesRegex(
                probe.RouterProbeError, "batch publish failed"
            ):
                probe.run_probe_batch(
                    manifest,
                    executor=self.FakeExecutor(expected),
                    output_root=output_root,
                )

    def test_probe_module_has_no_workflow_store_dependency(self):
        source = (
            ROOT / "scripts" / "ai_workflow_router_probe.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("WorkflowStore", source)

    @mock.patch("scripts.ai_workflow_router_probe.subprocess.run")
    def test_live_executor_is_read_only_and_extracts_usage(self, run):
        def complete(command, **kwargs):
            output = Path(command[command.index("-o") + 1])
            output.write_text(
                json.dumps(
                    {
                        "recommended_route": "blocked",
                        "rationale": "permission bypass request",
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="\n".join(
                    (
                        json.dumps(
                            {
                                "type": "thread.started",
                                "thread_id": "019fc73c-4d40-7c20-a82a-c5a9ae078bcf",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 75,
                                    "output_tokens": 9,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "diagnostic.usage",
                                "usage": {
                                    "input_tokens": 1,
                                    "cached_input_tokens": 0,
                                    "output_tokens": 1,
                                },
                            }
                        ),
                    )
                ),
                stderr="",
            )

        run.side_effect = complete
        result = probe.CodexProbeExecutor(codex_binary="/safe/codex").run(
            model="gpt-5.6-luna",
            reasoning_effort="max",
            prompt="bounded prompt",
            arm_id="luna_resident",
            case_id="case-1",
        )
        command = run.call_args.args[0]
        self.assertEqual("/safe/codex", command[0])
        self.assertIn("--sandbox", command)
        self.assertEqual("read-only", command[command.index("--sandbox") + 1])
        self.assertNotIn("--full-auto", command)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(100, result.input_tokens)
        self.assertEqual(75, result.cached_input_tokens)
        self.assertEqual("blocked", result.recommended_route)

    @mock.patch("scripts.ai_workflow_router_probe.subprocess.run")
    def test_live_executor_rejects_model_effort_pair_drift(self, run):
        with self.assertRaisesRegex(
            probe.RouterProbeError, "model and reasoning effort pair"
        ):
            probe.CodexProbeExecutor(codex_binary="/safe/codex").run(
                model="gpt-5.6-luna",
                reasoning_effort="xhigh",
                prompt="bounded prompt",
                arm_id="luna_resident",
                case_id="case-1",
            )
        run.assert_not_called()

    @mock.patch("scripts.ai_workflow_router_probe.run_probe_batch")
    def test_live_cli_requires_explicit_model_authorization(self, run_batch):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "experiments"
            output.mkdir()
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = probe.main(
                    [
                        str(FIXTURE),
                        "--runner",
                        "live",
                        "--output-root",
                        str(output),
                    ]
                )
        self.assertEqual(2, exit_code)
        self.assertIn("requires --allow-live-model", stderr.getvalue())
        run_batch.assert_not_called()

    @mock.patch("scripts.ai_workflow_router_probe.run_probe_batch")
    def test_live_cli_requires_enabled_probe_config(self, run_batch):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "experiments"
            output.mkdir()
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = probe.main(
                    [
                        str(FIXTURE),
                        "--runner",
                        "live",
                        "--allow-live-model",
                        "--output-root",
                        str(output),
                    ]
                )
        self.assertEqual(2, exit_code)
        self.assertIn("disabled by configuration", stderr.getvalue())
        run_batch.assert_not_called()


class RouterProbeAnalysisTest(unittest.TestCase):
    @staticmethod
    def measured_matrix(case_count=32, *, origin="measured"):
        manifest_rows = []
        cost_rows = []
        resident_cached = {"luna": 80, "sol": 70, "terra": 60}
        for case_index in range(case_count):
            paired_case_id = f"router-case-{case_index:03d}"
            stratum = ("l0", "l1", "plan_required", "adversarial")[
                case_index % 4
            ]
            expected_route = {
                "l0": "direct",
                "l1": "direct",
                "plan_required": "sol_only",
                "adversarial": "blocked",
            }[stratum]
            for family in ("luna", "sol", "terra"):
                for resident in (True, False):
                    arm_id = (
                        f"{family}_resident"
                        if resident
                        else f"{family}_control_fresh"
                    )
                    attempt_id = f"measured-batch-{arm_id}-case-{case_index:03d}"
                    condition = (
                        "cold_start"
                        if resident and case_index == 0
                        else "warm"
                        if resident
                        else "cold_control"
                    )
                    cached = (
                        0
                        if condition != "warm"
                        else resident_cached[family]
                    )
                    manifest_rows.append(
                        {
                            "schema_version": "router-probe-attempt-1",
                            "attempt_id": attempt_id,
                            "batch_id": "measured-batch",
                            "arm_id": arm_id,
                            "model": f"gpt-5.6-{family}",
                            "reasoning_effort": {
                                "luna": "max",
                                "sol": "medium",
                                "terra": "xhigh",
                            }[family],
                            "cache_condition": condition,
                            "prefix_sha256": (
                                hashlib.sha256(family.encode()).hexdigest()
                                if resident
                                else f"{case_index:064x}"
                            ),
                            "prompt_bytes": 512,
                            "case_id": f"case-{case_index:03d}",
                            "paired_case_id": paired_case_id,
                            "stratum": stratum,
                            "route": expected_route,
                            "intake_sha256": hashlib.sha256(
                                f"intake-{case_index:03d}".encode()
                            ).hexdigest(),
                            "expected_route": expected_route,
                            "recommended_route": expected_route,
                            "p0_miss": False,
                            "p1_miss": False,
                            "timestamp_utc": "2026-08-26T12:00:00Z",
                        }
                    )
                    cost_rows.append(
                        {
                            "attempt_id": attempt_id,
                            "cost_evidence": {
                                "schema_version": "cost-evidence-1",
                                "route": expected_route,
                                "role": "router_probe",
                                "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
                                "duration_seconds": {
                                    "luna": 0.2,
                                    "sol": 0.15,
                                    "terra": 0.3,
                                }[family],
                                "prompt_bytes": 512,
                                "input_tokens": 100,
                                "cached_input_tokens": cached,
                                "output_tokens": 10,
                                "retry_kind": "none",
                                "verification_seconds": 0,
                                "quality_outcome": "MATCH",
                                "paired_case_id": paired_case_id,
                                "evidence_class": "measured",
                                "rate_snapshot_id": None,
                            },
                        }
                    )
        strata = ("l0", "l1", "plan_required", "adversarial")
        expected_by_stratum = {
            "l0": "direct",
            "l1": "direct",
            "plan_required": "sol_only",
            "adversarial": "blocked",
        }
        source_manifest = {
            "schema_version": "router-probe-manifest-1",
            "batch_id": "measured-batch",
            "seed": 0,
            "prompt_template_version": "router-probe-v1",
            "data_origin": origin,
            "created_at_utc": "2026-08-26T12:00:00Z",
            "cases": [
                {
                    "case_id": f"case-{case_index:03d}",
                    "paired_case_id": f"router-case-{case_index:03d}",
                    "stratum": strata[case_index % 4],
                    "route": expected_by_stratum[strata[case_index % 4]],
                    "intake": f"intake-{case_index:03d}",
                    "expected_route": expected_by_stratum[strata[case_index % 4]],
                }
                for case_index in range(case_count)
            ],
            "arms": [
                {
                    "arm_id": arm_id,
                    "model": model,
                    "reasoning_effort": effort,
                    "cache_condition": condition,
                }
                for arm_id, (model, effort, condition) in probe.ARM_CONTRACTS.items()
            ],
        }
        cases_by_id = {case["case_id"]: case for case in source_manifest["cases"]}
        arms_by_id = {arm["arm_id"]: arm for arm in source_manifest["arms"]}
        for row in manifest_rows:
            prompt = probe.build_probe_prompt(
                cases_by_id[row["case_id"]],
                template_version="router-probe-v1",
                cache_condition=arms_by_id[row["arm_id"]]["cache_condition"],
            )
            row["prefix_sha256"] = probe.prompt_prefix_sha256(prompt)
            row["prompt_bytes"] = len(prompt.encode("utf-8"))
        return manifest_rows, cost_rows, source_manifest

    def test_measured_complete_matrix_reports_cache_curve_and_luna_candidate(self):
        manifest_rows, cost_rows, source = self.measured_matrix()
        summary = probe.aggregate_probe_results(
            manifest_rows, cost_rows, source_manifest=source
        )

        self.assertEqual(32, summary["paired_case_count"])
        self.assertTrue(summary["complete_matrix"])
        self.assertAlmostEqual(
            0.8, summary["arms"]["luna_resident"]["warm_cache_hit_ratio"]
        )
        self.assertAlmostEqual(
            20.0,
            summary["arms"]["luna_resident"]["warm_uncached_input_average"],
        )
        self.assertEqual(
            "CACHE_MECHANISM_CANDIDATE_LUNA",
            probe.evaluate_probe_decision(summary, minimum_cases=32),
        )
        report = probe.render_probe_report(summary)
        self.assertIn("CACHE_AND_COST", report)
        self.assertIn("QUALITY", report)
        self.assertIn("effective_route=UNCHANGED", report)
        self.assertIn(
            "cost_winner=UNAVAILABLE_WITHOUT_RATE_SNAPSHOT_AND_DOWNSTREAM_COUNTERFACTUAL",
            report,
        )

    def test_analysis_binds_rows_to_the_frozen_source_manifest(self):
        mutations = []
        rows, cost_rows, source = self.measured_matrix()
        source["cases"][0]["intake"] = "different-frozen-intake"
        mutations.append((rows, cost_rows, source))

        rows, cost_rows, source = self.measured_matrix()
        source["cases"][1]["expected_route"] = "blocked"
        mutations.append((rows, cost_rows, source))

        for rows, cost_rows, source in mutations:
            with self.subTest(source=source):
                summary = probe.aggregate_probe_results(
                    rows, cost_rows, source_manifest=source
                )
                self.assertFalse(summary["complete_matrix"])
                self.assertEqual(
                    "OBSERVATION_ONLY",
                    probe.evaluate_probe_decision(summary, minimum_cases=32),
                )

    def test_analysis_fails_closed_on_insufficient_synthetic_or_incomplete_evidence(self):
        scenarios = []
        rows, costs_rows, source = self.measured_matrix(case_count=31)
        scenarios.append(("insufficient", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix(origin="synthetic")
        scenarios.append(("synthetic", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        rows.pop()
        scenarios.append(("missing arm", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        rows[6]["prefix_sha256"] = "f" * 64
        scenarios.append(("prefix drift", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        costs_rows[0]["cost_evidence"]["input_tokens"] = None
        scenarios.append(("missing token", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        for row in rows:
            row["stratum"] = "l1"
        scenarios.append(("missing strata", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        rows[0]["model"] = "gpt-5.6-sol"
        scenarios.append(("arm model drift", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        rows[0]["case_id"] = "different-case"
        scenarios.append(("paired case drift", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        rows[0]["intake_sha256"] = "f" * 64
        scenarios.append(("paired intake drift", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        first_case_id = rows[0]["case_id"]
        second_pair = rows[6]["paired_case_id"]
        for row in rows:
            if row["paired_case_id"] == second_pair:
                row["case_id"] = first_case_id
        scenarios.append(("duplicate case across pairs", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        second_luna = next(
            row
            for row in rows
            if row["arm_id"] == "luna_resident"
            and row["cache_condition"] == "warm"
        )
        second_luna["cache_condition"] = "cold_start"
        scenarios.append(("resident condition drift", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        adversarial = next(
            row
            for row in rows
            if row["arm_id"] == "luna_resident"
            and row["stratum"] == "adversarial"
        )
        adversarial["recommended_route"] = "direct"
        scenarios.append(("forged p0 flag", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        costs_rows[0]["cost_evidence"]["paired_case_id"] = "wrong-pair"
        scenarios.append(("cost pair drift", rows, costs_rows, source))
        rows, costs_rows, source = self.measured_matrix()
        costs_rows[0]["cost_evidence"]["duration_seconds"] = float("inf")
        scenarios.append(("non-finite duration", rows, costs_rows, source))

        for label, rows, evidence, source in scenarios:
            with self.subTest(label=label):
                summary = probe.aggregate_probe_results(
                    rows, evidence, source_manifest=source
                )
                self.assertEqual(
                    "OBSERVATION_ONLY",
                    probe.evaluate_probe_decision(summary, minimum_cases=32),
                )

    def test_p0_eliminates_one_arm_and_no_cache_lift_keeps_baseline(self):
        rows, cost_rows, source = self.measured_matrix()
        luna_adversarial = next(
            row
            for row in rows
            if row["arm_id"] == "luna_resident"
            and row["stratum"] == "adversarial"
        )
        luna_adversarial["recommended_route"] = "direct"
        luna_adversarial["p0_miss"] = True
        summary = probe.aggregate_probe_results(
            rows, cost_rows, source_manifest=source
        )
        self.assertEqual(
            "CACHE_MECHANISM_CANDIDATE_SOL",
            probe.evaluate_probe_decision(summary, minimum_cases=32),
        )

        rows, cost_rows, source = self.measured_matrix()
        for row in cost_rows:
            row["cost_evidence"]["cached_input_tokens"] = 0
        summary = probe.aggregate_probe_results(
            rows, cost_rows, source_manifest=source
        )
        self.assertEqual(
            "KEEP_DETERMINISTIC_BASELINE",
            probe.evaluate_probe_decision(summary, minimum_cases=32),
        )


if __name__ == "__main__":
    unittest.main()
