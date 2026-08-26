import copy
import inspect
import json
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_routing as routing_mod


ROOT = Path(__file__).resolve().parents[1]
DECISION_WIRE_KEYS = {
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
ADVICE_FIELDS = {
    "schema_version",
    "task_id",
    "actual_route",
    "recommended_route",
    "optimization_mode",
    "gate_result",
    "applied",
    "task_sha256",
    "request_sha256",
}


def valid_task(*, risk_flags=None, task_type="PLAN"):
    task = {
        "schema_version": "ai-task-1",
        "task_id": "AWF-20260803-001",
        "task_type": task_type,
        "objective": "observe shadow cost routing",
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


def supporting_metrics(
    *,
    paired=8,
    net=-1.0,
    quality=0.0,
    p0=0,
    p1=0,
    calibration=0.8,
    experiment=0.8,
    **overrides,
):
    metrics = {
        "cost_summary": {
            "paired_case_count": paired,
            "quality_delta_points": quality,
            "net_measured_cost_delta": net,
        },
        "p0_miss_count": p0,
        "p1_miss_count": p1,
        "calibration_first_delivery_pass_rate": calibration,
        "experiment_first_delivery_pass_rate": experiment,
    }
    metrics.update(overrides)
    return metrics


SHADOW_CONFIG = {
    "routing": {"mode": "enforced", "role_policy": "terra_os"},
    "optimization": {
        "mode": "shadow",
        "minimum_paired_cases": 8,
        "compact_prompts": False,
    }
}
ENFORCED_CONFIG = {
    "routing": {"mode": "enforced", "role_policy": "terra_os"},
    "optimization": {
        "mode": "enforced",
        "minimum_paired_cases": 8,
        "compact_prompts": False,
    }
}
ARMED_COMPACT_CONFIG = {
    "routing": {"mode": "enforced", "role_policy": "terra_os"},
    "optimization": {
        "mode": "enforced",
        "minimum_paired_cases": 8,
        "compact_prompts": True,
    }
}
SHADOW_COMPACT_CONFIG = {
    "routing": {"mode": "enforced", "role_policy": "terra_os"},
    "optimization": {
        "mode": "shadow",
        "minimum_paired_cases": 8,
        "compact_prompts": True,
    }
}


TASK_FIDELITY_FIELDS = (
    "task_id",
    "schema_version",
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
)
CONTRACT_FIDELITY_FIELDS = (
    "dispatch_id",
    "plan_sha256",
    "task_sha256",
    "request_sha256",
    "subtask_id",
    "write_scope",
    "acceptance_criteria",
    "dependencies",
    "permission_profile",
    "candidate_sha256",
    "evidence_sha256",
    "authorization_ticket",
    "required_output_schema",
    "required_output_path",
)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _assert_value_preserved(test, prompt, value):
    if isinstance(value, str):
        test.assertIn(value, prompt)
        return
    test.assertIn(_canonical(value), prompt)


def pinned_config_with(optimization):
    with (ROOT / "config" / "ai_workflow.toml").open("rb") as handle:
        config = tomllib.load(handle)
    config["optimization"] = copy.deepcopy(optimization)
    return config


def compact_role_prompt(role, task, contract, evidence_paths=(), **kwargs):
    load = pinned_config_with(kwargs.get("config", ARMED_COMPACT_CONFIG)["optimization"])
    gate = kwargs.get("metrics", supporting_metrics())
    root = kwargs.get("state_root", Path("."))
    with mock.patch.object(workflow, "_load_workflow_config", return_value=load), mock.patch.object(
        workflow, "aggregate_metrics", return_value=gate
    ):
        return workflow.build_role_prompt_result(
            role,
            task,
            contract,
            evidence_paths,
            state_root=root,
        )


def evaluate_test_advice(decision, **kwargs):
    config = kwargs.pop("config", None)
    metrics = kwargs.pop("metrics", None)
    if config is None and metrics is None:
        return workflow.evaluate_and_apply_route_advice(decision, **kwargs)
    effective_config = workflow._load_workflow_config() if config is None else config
    effective_metrics = {} if metrics is None else metrics
    with mock.patch.object(
        routing_mod,
        "_load_optimization_inputs",
        return_value=(effective_config, effective_metrics),
    ):
        return workflow.evaluate_and_apply_route_advice(
            decision,
            state_root=Path("."),
            **kwargs,
        )


def valid_advice_document(**overrides):
    value = {
        "schema_version": "ai-route-advice-1",
        "task_id": "AWF-20260803-001",
        "actual_route": "delegated",
        "recommended_route": "direct",
        "optimization_mode": "shadow",
        "gate_result": "KEEP_SHADOW",
        "applied": False,
        "task_sha256": "a" * 64,
        "request_sha256": "b" * 64,
    }
    value.update(overrides)
    return value


class RouteAdviceSchemaTest(unittest.TestCase):
    def test_schema_is_strict_ai_route_advice_1(self):
        schema = json.loads(
            (ROOT / "config" / "ai_workflow_route_advice.schema.json").read_text()
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual("ai-route-advice-1", schema["properties"]["schema_version"]["const"])
        self.assertEqual(ADVICE_FIELDS, set(schema["properties"]))
        self.assertEqual(ADVICE_FIELDS, set(schema["required"]))
        self.assertEqual(["shadow", "enforced"], schema["properties"]["optimization_mode"]["enum"])
        self.assertEqual(
            ["KEEP_SHADOW", "ALLOW_ENFORCED", "FALLBACK_FIXED"],
            schema["properties"]["gate_result"]["enum"],
        )

    def test_unknown_and_missing_fields_are_rejected(self):
        extra = valid_advice_document(surprise=True)
        with self.assertRaisesRegex(workflow.WorkflowError, "UNKNOWN_FIELD"):
            workflow.validate_route_advice(extra)
        missing = valid_advice_document()
        missing.pop("applied")
        with self.assertRaisesRegex(workflow.WorkflowError, "MISSING_FIELD"):
            workflow.validate_route_advice(missing)


class OptimizationPolicyTest(unittest.TestCase):
    def test_unknown_config_fails_closed(self):
        valid = {
            "optimization": {
                "mode": "shadow",
                "minimum_paired_cases": 8,
                "compact_prompts": False,
            }
        }
        policy = workflow.resolve_optimization_policy(valid)
        self.assertEqual("shadow", policy.mode)
        self.assertEqual(8, policy.minimum_paired_cases)
        self.assertIs(False, policy.compact_prompts)

        for config in (
            {},
            {"optimization": {"mode": "trial", "minimum_paired_cases": 8, "compact_prompts": False}},
            {"optimization": {"mode": "shadow", "minimum_paired_cases": True, "compact_prompts": False}},
            {"optimization": {"mode": "shadow", "minimum_paired_cases": -1, "compact_prompts": False}},
            {"optimization": {"mode": "shadow", "minimum_paired_cases": 8, "compact_prompts": "yes"}},
            {"optimization": {"mode": "legacy", "minimum_paired_cases": 8, "compact_prompts": False}},
        ):
            with self.subTest(config=config), self.assertRaisesRegex(
                workflow.WorkflowError, "OPTIMIZATION_POLICY_INVALID"
            ):
                workflow.resolve_optimization_policy(config)


class OptimizationGateTest(unittest.TestCase):
    def test_eight_pairs_with_support_zero_miss_and_no_first_delivery_drop_allow(self):
        self.assertEqual(
            "ALLOW_ENFORCED",
            workflow.evaluate_optimization_gate(supporting_metrics()),
        )

    def test_insufficient_pairs_missing_net_non_negative_miss_and_first_delivery_fallback(self):
        cases = {
            "seven_pairs": supporting_metrics(paired=7),
            "net_missing": supporting_metrics(net=None),
            "net_non_negative": supporting_metrics(net=0.0),
            "p0_miss": supporting_metrics(p0=1),
            "p1_miss": supporting_metrics(p1=1),
            "first_delivery_drop": supporting_metrics(calibration=0.9, experiment=0.8),
            "missing_calibration_rate": supporting_metrics(calibration=None),
            "missing_experiment_rate": supporting_metrics(experiment=None),
        }
        for name, metrics in cases.items():
            with self.subTest(name=name):
                self.assertEqual(
                    "FALLBACK_FIXED",
                    workflow.evaluate_optimization_gate(metrics),
                )

    def test_evaluate_cost_claim_default_minimum_stays_thirty(self):
        self.assertEqual(30, workflow.evaluate_cost_claim.__defaults__[0])

    def test_synthetic_fixture_cannot_open_the_gate(self):
        good = supporting_metrics()
        good["synthetic"] = True
        self.assertEqual("FALLBACK_FIXED", workflow.evaluate_optimization_gate(good))
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "paired-cases.json").read_text(encoding="utf-8")
        )
        records = [attempt for case in fixture["cases"] for attempt in case["attempts"]]
        self.assertEqual(
            "FALLBACK_FIXED",
            workflow.evaluate_optimization_gate(
                supporting_metrics(cost_summary=workflow.aggregate_paired_cases(records))
            ),
        )


class ShadowAndEnforcedAdviceTest(unittest.TestCase):
    def test_shadow_can_recommend_a_different_route_without_changing_roles_or_wire(self):
        task = valid_task()
        request = route_request("SIMPLE", "WRITE")
        decision = workflow.decide_route(task, request, "shadow")
        original_roles = decision.effective_roles

        advice = evaluate_test_advice(
            decision,
            recommended_route="direct",
            config=SHADOW_CONFIG,
            task=task,
            request=request,
        )

        self.assertEqual("delegated", advice.actual_route)
        self.assertEqual("direct", advice.recommended_route)
        self.assertEqual("KEEP_SHADOW", advice.gate_result)
        self.assertIs(False, advice.applied)
        self.assertEqual(original_roles, advice.roles)
        self.assertEqual(original_roles, advice.effective_roles)
        self.assertEqual(original_roles, decision.effective_roles)
        self.assertEqual(DECISION_WIRE_KEYS, set(decision.to_dict()))
        self.assertEqual(DECISION_WIRE_KEYS, set(advice.to_dict()))
        self.assertNotIn("recommended_route", decision.to_dict())

    def test_shadow_does_not_record_an_unsafe_direct_recommendation(self):
        task = valid_task(risk_flags=["SECURITY"])
        request = route_request("HIGH_CONSEQUENCE", "WRITE", ["SECURITY"])
        decision = workflow.decide_route(task, request, "shadow")

        advice = evaluate_test_advice(
            decision,
            recommended_route="direct",
            config=SHADOW_CONFIG,
            task=task,
            request=request,
        )

        self.assertEqual("delegated", decision.effective_route)
        self.assertEqual(decision.effective_route, advice.actual_route)
        self.assertEqual(decision.effective_route, advice.recommended_route)
        self.assertEqual(decision.effective_roles, advice.effective_roles)
        self.assertIs(False, advice.applied)
        self.assertEqual("KEEP_SHADOW", advice.gate_result)

    def test_enforced_below_eight_pairs_keeps_the_fixed_chain(self):
        task = valid_task()
        request = route_request("BOUNDED", "WRITE")
        decision = workflow.decide_route(task, request, "enforced")
        advice = evaluate_test_advice(
            decision,
            recommended_route="direct",
            metrics=supporting_metrics(paired=7),
            config=ENFORCED_CONFIG,
            task=task,
            request=request,
        )
        self.assertIn(advice.gate_result, {"KEEP_SHADOW", "FALLBACK_FIXED"})
        self.assertIs(False, advice.applied)
        self.assertEqual(decision.effective_roles, advice.roles)
        self.assertEqual(decision.effective_roles, advice.roles)
        self.assertEqual(decision.effective_route, advice.actual_route)
        self.assertEqual(DECISION_WIRE_KEYS, set(advice.to_dict()))

    def test_enforced_applies_only_when_all_four_gates_pass_and_recommendation_is_legal(self):
        task = valid_task()
        request = route_request("BOUNDED", "WRITE")
        decision = workflow.decide_route(task, request, "enforced")
        self.assertEqual("delegated", decision.route)
        self.assertEqual(("terra_xhigh",), decision.effective_roles)

        advice = evaluate_test_advice(
            decision,
            recommended_route="direct",
            metrics=supporting_metrics(),
            config=ENFORCED_CONFIG,
            task=task,
            request=request,
        )
        self.assertEqual("ALLOW_ENFORCED", advice.gate_result)
        self.assertIs(True, advice.applied)
        self.assertEqual((), advice.roles)
        self.assertEqual("delegated", advice.actual_route)
        self.assertEqual("direct", advice.recommended_route)
        self.assertEqual(("terra_xhigh",), decision.effective_roles)
        self.assertEqual(DECISION_WIRE_KEYS, set(advice.to_dict()))

    def test_blocked_and_high_risk_direct_recommendations_are_not_applied(self):
        blocked_task = valid_task()
        blocked_request = route_request("BOUNDED", "WRITE", decomposable=False)
        blocked = workflow.decide_route(blocked_task, blocked_request, "enforced")
        blocked_advice = evaluate_test_advice(
            blocked,
            recommended_route="delegated",
            metrics=supporting_metrics(),
            config=ENFORCED_CONFIG,
            task=blocked_task,
            request=blocked_request,
        )
        self.assertEqual("blocked", blocked.route)
        self.assertEqual("FALLBACK_FIXED", blocked_advice.gate_result)
        self.assertIs(False, blocked_advice.applied)
        self.assertEqual((), blocked_advice.roles)

        risky_task = valid_task(risk_flags=["SECURITY"])
        risky_request = route_request("SIMPLE", "WRITE", ["SECURITY"])
        risky = workflow.decide_route(risky_task, risky_request, "enforced")
        risky_advice = evaluate_test_advice(
            risky,
            recommended_route="direct",
            metrics=supporting_metrics(),
            config=ENFORCED_CONFIG,
            task=risky_task,
            request=risky_request,
        )
        self.assertEqual("delegated", risky.route)
        self.assertEqual("FALLBACK_FIXED", risky_advice.gate_result)
        self.assertIs(False, risky_advice.applied)
        self.assertEqual(risky.effective_roles, risky_advice.roles)

        high_task = valid_task()
        high_request = route_request("HIGH_CONSEQUENCE", "WRITE")
        high = workflow.decide_route(high_task, high_request, "enforced")
        high_advice = evaluate_test_advice(
            high,
            recommended_route="direct",
            metrics=supporting_metrics(),
            config=ENFORCED_CONFIG,
            task=high_task,
            request=high_request,
        )
        self.assertEqual("delegated", high.route)
        self.assertEqual("FALLBACK_FIXED", high_advice.gate_result)
        self.assertIs(False, high_advice.applied)

    def test_illegal_recommendation_falls_back_without_raising(self):
        task = valid_task()
        request = route_request("BOUNDED", "WRITE")
        decision = workflow.decide_route(task, request, "enforced")
        advice = evaluate_test_advice(
            decision,
            recommended_route="turbo",
            metrics=supporting_metrics(),
            config=ENFORCED_CONFIG,
            task=task,
            request=request,
        )
        self.assertIn(advice.gate_result, {"KEEP_SHADOW", "FALLBACK_FIXED"})
        self.assertIs(False, advice.applied)
        self.assertEqual(decision.effective_roles, advice.roles)
        self.assertEqual(decision.effective_roles, advice.roles)

    def test_illegal_optimization_mode_raises(self):
        task = valid_task()
        request = route_request("SIMPLE", "NONE")
        decision = workflow.decide_route(task, request, "shadow")
        with self.assertRaisesRegex(workflow.WorkflowError, "OPTIMIZATION_POLICY_INVALID"):
            evaluate_test_advice(
                decision,
                recommended_route="direct",
                config={
                    "optimization": {
                        "mode": "trial",
                        "minimum_paired_cases": 8,
                        "compact_prompts": False,
                    }
                },
                task=task,
                request=request,
            )


class RouteAdvicePersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)
        self.task = valid_task()
        self.task_path = self.store.create_task(self.task)
        self.request = route_request("SIMPLE", "READ_ONLY")
        self.decision = workflow.decide_route(self.task, self.request, "shadow")
        workflow.record_route_decision(self.store, self.task["task_id"], self.decision)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _advice(self, **overrides):
        kwargs = {
            "recommended_route": "direct",
            "config": SHADOW_CONFIG,
            "task": self.task,
            "request": self.request,
        }
        kwargs.update(overrides)
        return evaluate_test_advice(self.decision, **kwargs)

    def test_sidecar_binds_hashes_and_rejects_a_second_different_advice(self):
        advice = self._advice()
        path = workflow.record_route_advice(
            self.store,
            self.task["task_id"],
            advice,
            request_sha256=workflow.artifact_sha256(self.request),
        )
        self.assertEqual(self.state_root / self.task["task_id"] / "route-advice.json", path)
        persisted = json.loads(path.read_text(encoding="utf-8"))
        workflow.validate_route_advice(persisted)
        self.assertEqual(workflow.artifact_sha256(self.task), persisted["task_sha256"])
        self.assertEqual(workflow.artifact_sha256(self.request), persisted["request_sha256"])
        self.assertEqual(self.decision.request_sha256, persisted["request_sha256"])
        events = [
            json.loads(line)
            for line in (self.state_root / self.task["task_id"] / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual("ROUTE_ADVICE_RECORDED", events[-1]["event_type"])
        self.assertEqual(persisted["task_sha256"], events[-1]["task_sha256"])
        self.assertEqual(persisted["request_sha256"], events[-1]["request_sha256"])

        different = self._advice(recommended_route="sol_only")
        with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_ADVICE"):
            workflow.record_route_advice(
                self.store,
                self.task["task_id"],
                different,
                request_sha256=workflow.artifact_sha256(self.request),
            )

    def test_sidecar_rejects_hash_drift(self):
        advice = self._advice()
        drifted = dict(advice.to_advice_dict())
        drifted["request_sha256"] = "c" * 64
        with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_ADVICE"):
            workflow.record_route_advice(
                self.store,
                self.task["task_id"],
                drifted,
                request_sha256="c" * 64,
            )

    def test_schema_valid_enforced_advice_cannot_be_recorded_without_controller_evaluation(self):
        forged = valid_advice_document(
            actual_route="delegated",
            recommended_route="direct",
            optimization_mode="enforced",
            gate_result="ALLOW_ENFORCED",
            applied=True,
            task_sha256=self.decision.task_sha256,
            request_sha256=self.decision.request_sha256,
        )
        workflow.validate_route_advice(forged)
        with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_ADVICE_UNTRUSTED"):
            workflow.record_route_advice(
                self.store,
                self.task["task_id"],
                forged,
                request_sha256=workflow.artifact_sha256(self.request),
            )


class RoutingAtomicPersistenceTest(unittest.TestCase):
    def _fault_patch(self, stage):
        if stage in {"write", "flush"}:
            real_named_temporary_file = tempfile.NamedTemporaryFile

            class FailingTemporary:
                def __init__(self, *args, **kwargs):
                    self.handle = real_named_temporary_file(*args, **kwargs)
                    self.name = self.handle.name

                def __enter__(self):
                    self.handle.__enter__()
                    return self

                def write(self, value):
                    if stage == "write":
                        raise OSError("injected content write failure")
                    return self.handle.write(value)

                def flush(self):
                    if stage == "flush":
                        raise OSError("injected flush failure")
                    return self.handle.flush()

                def fileno(self):
                    return self.handle.fileno()

                def __exit__(self, exc_type, exc, traceback):
                    return self.handle.__exit__(exc_type, exc, traceback)

            return mock.patch.object(
                workflow.tempfile,
                "NamedTemporaryFile",
                side_effect=FailingTemporary,
            )
        if stage == "file_fsync":
            return mock.patch.object(
                workflow.os,
                "fsync",
                side_effect=OSError("injected file fsync failure"),
            )
        return mock.patch.object(
            workflow.os,
            "replace",
            side_effect=OSError("injected replace failure"),
        )

    def _case(self, root):
        store = workflow.WorkflowStore(root / "state")
        task = valid_task()
        store.create_task(task)
        request = route_request("SIMPLE", "READ_ONLY")
        decision = workflow.decide_route(task, request, "shadow")
        advice = evaluate_test_advice(
            decision,
            recommended_route="direct",
            config=SHADOW_CONFIG,
            task=task,
            request=request,
        )
        return store, task, request, decision, advice

    def test_route_decision_prepublication_failures_leave_no_frozen_file(self):
        for stage in ("write", "flush", "file_fsync", "replace"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store, task, _request, decision, _advice = self._case(root)
                path = store.root / task["task_id"] / "route-decision.json"
                with self._fault_patch(stage):
                    with self.assertRaisesRegex(
                        workflow.WorkflowError, "ATOMIC_WRITE_FAILED"
                    ):
                        workflow.record_route_decision(
                            store, task["task_id"], decision
                        )
                self.assertFalse(path.exists())
                self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))
                workflow.record_route_decision(store, task["task_id"], decision)
                self.assertTrue(path.is_file())

    def test_route_advice_prepublication_failures_leave_no_frozen_file(self):
        for stage in ("write", "flush", "file_fsync", "replace"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store, task, request, decision, advice = self._case(root)
                workflow.record_route_decision(store, task["task_id"], decision)
                path = store.root / task["task_id"] / "route-advice.json"
                with self._fault_patch(stage):
                    with self.assertRaisesRegex(
                        workflow.WorkflowError, "ATOMIC_WRITE_FAILED"
                    ):
                        workflow.record_route_advice(
                            store,
                            task["task_id"],
                            advice,
                            request_sha256=workflow.artifact_sha256(request),
                        )
                self.assertFalse(path.exists())
                self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))
                workflow.record_route_advice(
                    store,
                    task["task_id"],
                    advice,
                    request_sha256=workflow.artifact_sha256(request),
                )
                self.assertTrue(path.is_file())

    def test_directory_fsync_failure_reports_published_and_retry_recovers_events(self):
        for artifact in ("decision", "advice"):
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store, task, request, decision, advice = self._case(root)
                if artifact == "advice":
                    workflow.record_route_decision(store, task["task_id"], decision)
                real_fsync = workflow.os.fsync
                calls = 0

                def fail_directory_fsync(descriptor):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise OSError("injected directory fsync failure")
                    return real_fsync(descriptor)

                with mock.patch.object(
                    workflow.os, "fsync", side_effect=fail_directory_fsync
                ):
                    with self.assertRaisesRegex(
                        workflow.WorkflowError,
                        "ATOMIC_WRITE_PUBLISHED_UNSYNCED",
                    ):
                        if artifact == "decision":
                            workflow.record_route_decision(
                                store, task["task_id"], decision
                            )
                        else:
                            workflow.record_route_advice(
                                store,
                                task["task_id"],
                                advice,
                                request_sha256=workflow.artifact_sha256(request),
                            )

                if artifact == "decision":
                    recovered = workflow.persist_or_reuse_route_decision(
                        store, task["task_id"], decision
                    )
                    self.assertEqual(decision.to_dict(), recovered.to_dict())
                    event_type = "ROUTE_DECIDED"
                else:
                    workflow.record_route_advice(
                        store,
                        task["task_id"],
                        advice,
                        request_sha256=workflow.artifact_sha256(request),
                    )
                    event_type = "ROUTE_ADVICE_RECORDED"
                events = [
                    json.loads(line)
                    for line in (
                        store.root / task["task_id"] / "events.jsonl"
                    ).read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(
                    1,
                    sum(event["event_type"] == event_type for event in events),
                )


class OptimizationMetricsTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _create(self, task_id):
        task = valid_task()
        task["task_id"] = task_id
        self.store.create_task(task)
        return task_id

    def _record(self, task_id, run):
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            workflow.record_metrics(task_id, run)

    def test_aggregate_metrics_counts_misses_and_cohort_first_delivery(self):
        calibration = self._create("AWF-20260803-001")
        experiment = self._create("AWF-20260803-002")
        both = self._create("AWF-20260803-003")
        self._record(
            calibration,
            {
                "role": "terra",
                "period": "calibration",
                "data_origin": "runtime",
                "status": "IMPLEMENTED_CANDIDATE",
                "p0_miss_count": 0,
                "p1_miss_count": 0,
            },
        )
        self._record(
            experiment,
            {
                "role": "terra",
                "period": "experiment",
                "data_origin": "runtime",
                "status": "BLOCKED",
                "p0_miss_count": 1,
                "p1_miss_count": 2,
            },
        )
        self._record(
            both,
            {
                "role": "luna",
                "period": "calibration",
                "data_origin": "runtime",
                "p0_miss_count": 0,
                "p1_miss_count": 0,
            },
        )
        self._record(
            both,
            {
                "role": "terra",
                "period": "experiment",
                "data_origin": "runtime",
                "status": "IMPLEMENTED_CANDIDATE",
                "p0_miss_count": 0,
                "p1_miss_count": 1,
            },
        )
        metrics = workflow.aggregate_metrics(self.state_root)
        self.assertEqual(1, metrics["p0_miss_count"])
        self.assertEqual(3, metrics["p1_miss_count"])
        self.assertEqual(1.0, metrics["calibration_first_delivery_pass_rate"])
        self.assertEqual(0.5, metrics["experiment_first_delivery_pass_rate"])

    def test_invalid_miss_counts_fail_closed(self):
        task_id = self._create("AWF-20260803-004")
        self._record(
            task_id,
            {
                "role": "luna",
                "period": "experiment",
                "data_origin": "runtime",
                "p0_miss_count": True,
                "p1_miss_count": -1,
            },
        )
        metrics = workflow.aggregate_metrics(self.state_root)
        self.assertIsNone(metrics["p0_miss_count"])
        self.assertIsNone(metrics["p1_miss_count"])
        self.assertEqual("FALLBACK_FIXED", workflow.evaluate_optimization_gate(metrics))

    def test_synthetic_fixture_records_cannot_open_aggregated_gate(self):
        task_id = self._create("AWF-20260803-005")
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "paired-cases.json").read_text(encoding="utf-8")
        )
        for index, case in enumerate(fixture["cases"][:8]):
            attempt = dict(case["attempts"][0])
            attempt["net_measured_cost_delta"] = -1.0
            attempt["quality_delta_points"] = 0.0
            workflow._record_controller_metrics(
                task_id,
                {
                    "role": "terra",
                    "period": "experiment",
                    "status": "IMPLEMENTED_CANDIDATE",
                    "data_origin": "synthetic_fixture",
                    "p0_miss_count": 0,
                    "p1_miss_count": 0,
                    "cost_evidence": attempt,
                },
                state_root=self.state_root,
            )
        metrics = workflow.aggregate_metrics(self.state_root)
        self.assertEqual({}, metrics["cost_summary"])
        self.assertEqual("FALLBACK_FIXED", workflow.evaluate_optimization_gate(metrics))


class RouteCliShadowAdviceTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)
        self.task = valid_task()
        self.task_path = self.store.create_task(self.task)

    def tearDown(self):
        self.temporary_directory.cleanup()

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_route_cli_writes_sidecar_but_prints_unchanged_decision_wire(self, run_codex):
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
        self.assertEqual(DECISION_WIRE_KEYS, set(payload))
        self.assertNotIn("recommended_route", payload)
        self.assertNotIn("applied", payload)
        sidecar = json.loads(
            (self.state_root / self.task["task_id"] / "route-advice.json").read_text(
                encoding="utf-8"
            )
        )
        workflow.validate_route_advice(sidecar)
        self.assertEqual("shadow", sidecar["optimization_mode"])
        self.assertEqual("KEEP_SHADOW", sidecar["gate_result"])
        self.assertIs(False, sidecar["applied"])
        self.assertEqual(workflow.artifact_sha256(self.task), sidecar["task_sha256"])
        run_codex.assert_not_called()

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_route_cli_reads_optimization_policy_not_routing_mode(self, run_codex):
        request_path = Path(self.temporary_directory.name) / "request.json"
        request_path.write_text(
            json.dumps(route_request("BOUNDED", "WRITE")), encoding="utf-8"
        )
        config = {
            "routing": {"mode": "enforced", "role_policy": "terra_os"},
            "optimization": {
                "mode": "shadow",
                "minimum_paired_cases": 8,
                "compact_prompts": False,
            },
        }
        output = StringIO()
        with (
            mock.patch.object(workflow, "_load_workflow_config", return_value=config),
            redirect_stdout(output),
        ):
            exit_code = workflow.main(
                [
                    "route",
                    "--task",
                    str(self.task_path),
                    "--request",
                    str(request_path),
                    "--mode",
                    "enforced",
                    "--root",
                    str(self.state_root),
                ]
            )
        self.assertEqual(0, exit_code)
        payload = json.loads(output.getvalue())
        self.assertEqual("delegated", payload["route"])
        self.assertEqual("enforced", payload["routing_mode"])
        sidecar = json.loads(
            (self.state_root / self.task["task_id"] / "route-advice.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("shadow", sidecar["optimization_mode"])
        self.assertEqual("KEEP_SHADOW", sidecar["gate_result"])
        self.assertIs(False, sidecar["applied"])
        run_codex.assert_not_called()

    def _route_argv(self, request_path, *, mode="shadow"):
        return [
            "route",
            "--task",
            str(self.task_path),
            "--request",
            str(request_path),
            "--mode",
            mode,
            "--root",
            str(self.state_root),
        ]

    def _write_request(self, request):
        path = Path(self.temporary_directory.name) / f"{request['work_class']}-{request['execution_need']}.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return path

    def _task_events(self):
        return [
            json.loads(line)
            for line in (
                self.state_root / self.task["task_id"] / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_route_cli_recovers_advice_after_partial_write(self, run_codex):
        request_path = self._write_request(route_request("SIMPLE", "READ_ONLY"))
        with mock.patch.object(
            workflow,
            "record_route_advice",
            side_effect=RuntimeError("simulated advice write failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated advice write failure"):
                workflow.main(self._route_argv(request_path))

        stored = json.loads(
            (self.state_root / self.task["task_id"] / "route-decision.json").read_text(
                encoding="utf-8"
            )
        )
        workflow.validate_route_decision(stored)
        self.assertFalse(
            (self.state_root / self.task["task_id"] / "route-advice.json").exists()
        )
        self.assertEqual(
            ["ROUTE_DECIDED"],
            [event["event_type"] for event in self._task_events()],
        )

        output = StringIO()
        with redirect_stdout(output):
            exit_code = workflow.main(self._route_argv(request_path))

        self.assertEqual(0, exit_code)
        self.assertEqual(workflow._canonical_json(stored) + "\n", output.getvalue())
        sidecar = json.loads(
            (self.state_root / self.task["task_id"] / "route-advice.json").read_text(
                encoding="utf-8"
            )
        )
        workflow.validate_route_advice(sidecar)
        self.assertEqual(stored["task_sha256"], sidecar["task_sha256"])
        self.assertEqual(stored["request_sha256"], sidecar["request_sha256"])
        self.assertEqual(
            ["ROUTE_DECIDED", "ROUTE_ADVICE_RECORDED"],
            [event["event_type"] for event in self._task_events()],
        )
        run_codex.assert_not_called()

    def test_route_decision_retry_restores_exactly_one_missing_matching_event(self):
        request_path = self._write_request(route_request("SIMPLE", "READ_ONLY"))
        real_append = workflow.WorkflowStore.append_event

        def fail_route_event(store, task_id, event):
            if event.get("event_type") == "ROUTE_DECIDED":
                raise workflow.WorkflowError("APPEND_FAILED", "injected route event failure")
            return real_append(store, task_id, event)

        with mock.patch.object(
            workflow.WorkflowStore, "append_event", autospec=True, side_effect=fail_route_event
        ):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(2, workflow.main(self._route_argv(request_path)))
            self.assertIn("APPEND_FAILED", output.getvalue())
        self.assertTrue(
            (self.state_root / self.task["task_id"] / "route-decision.json").is_file()
        )
        self.assertFalse(
            (self.state_root / self.task["task_id"] / "events.jsonl").exists()
        )

        with redirect_stdout(StringIO()):
            self.assertEqual(0, workflow.main(self._route_argv(request_path)))
            self.assertEqual(0, workflow.main(self._route_argv(request_path)))
        events = [
            event
            for event in self._task_events()
            if event.get("event_type") == "ROUTE_DECIDED"
        ]
        self.assertEqual(1, len(events))
        decision = json.loads(
            (self.state_root / self.task["task_id"] / "route-decision.json").read_text()
        )
        for field in (
            "task_sha256",
            "request_sha256",
            "route",
            "routing_mode",
            "rule_id",
        ):
            self.assertEqual(decision[field], events[0][field])

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_route_cli_rejects_retry_with_different_request(self, run_codex):
        first = self._write_request(route_request("SIMPLE", "READ_ONLY"))
        with redirect_stdout(StringIO()):
            self.assertEqual(0, workflow.main(self._route_argv(first)))
        stored = (
            self.state_root / self.task["task_id"] / "route-decision.json"
        ).read_text(encoding="utf-8")
        sidecar = (
            self.state_root / self.task["task_id"] / "route-advice.json"
        ).read_text(encoding="utf-8")
        events = (
            self.state_root / self.task["task_id"] / "events.jsonl"
        ).read_text(encoding="utf-8")

        different = self._write_request(route_request("BOUNDED", "WRITE"))
        output = StringIO()
        with redirect_stdout(output):
            exit_code = workflow.main(self._route_argv(different))

        self.assertEqual(2, exit_code)
        self.assertIn("ROUTE_ALREADY_FROZEN", output.getvalue())
        self.assertEqual(
            stored,
            (self.state_root / self.task["task_id"] / "route-decision.json").read_text(
                encoding="utf-8"
            ),
        )
        self.assertEqual(
            sidecar,
            (self.state_root / self.task["task_id"] / "route-advice.json").read_text(
                encoding="utf-8"
            ),
        )
        self.assertEqual(
            events,
            (self.state_root / self.task["task_id"] / "events.jsonl").read_text(
                encoding="utf-8"
            ),
        )
        run_codex.assert_not_called()

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_route_cli_rejects_retry_with_different_mode(self, run_codex):
        request_path = self._write_request(route_request("SIMPLE", "READ_ONLY"))
        with redirect_stdout(StringIO()):
            self.assertEqual(0, workflow.main(self._route_argv(request_path, mode="shadow")))
        stored = (
            self.state_root / self.task["task_id"] / "route-decision.json"
        ).read_text(encoding="utf-8")

        output = StringIO()
        with redirect_stdout(output):
            exit_code = workflow.main(self._route_argv(request_path, mode="enforced"))

        self.assertEqual(2, exit_code)
        self.assertIn("ROUTE_ALREADY_FROZEN", output.getvalue())
        self.assertEqual(
            stored,
            (self.state_root / self.task["task_id"] / "route-decision.json").read_text(
                encoding="utf-8"
            ),
        )
        run_codex.assert_not_called()


class ForgedGateAndControlledEntryTest(unittest.TestCase):
    def test_public_advice_entry_rejects_caller_config_metrics_and_requires_state_root(self):
        for function in (
            workflow.evaluate_and_apply_route_advice,
            workflow.apply_route_advice,
            routing_mod.evaluate_and_apply_route_advice,
            routing_mod.apply_route_advice,
        ):
            parameters = inspect.signature(function).parameters
            self.assertNotIn("config", parameters)
            self.assertNotIn("metrics", parameters)
        self.assertNotIn(
            "_controller_owned", inspect.signature(workflow.record_metrics).parameters
        )
        with self.assertRaises(TypeError):
            workflow.record_metrics(
                "AWF-20260803-001",
                {"role": "luna"},
                _controller_owned=True,
            )
        task = valid_task()
        request = route_request("BOUNDED", "WRITE")
        decision = workflow.decide_route(task, request, "enforced")
        with self.assertRaises(TypeError):
            workflow.evaluate_and_apply_route_advice(
                decision,
                recommended_route="direct",
                config=ENFORCED_CONFIG,
                metrics=supporting_metrics(),
                task=task,
                request=request,
            )
        advice = evaluate_test_advice(
            decision,
            recommended_route="direct",
            task=task,
            request=request,
        )
        self.assertIn(advice.gate_result, {"KEEP_SHADOW", "FALLBACK_FIXED"})
        self.assertIs(False, advice.applied)
        self.assertEqual(decision.effective_roles, advice.roles)

    def test_controller_state_loader_can_open_enforced_gate(self):
        task = valid_task()
        request = route_request("BOUNDED", "WRITE")
        decision = workflow.decide_route(task, request, "enforced")
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            routing_mod,
            "_load_optimization_inputs",
            return_value=(ENFORCED_CONFIG, supporting_metrics()),
        ):
            advice = workflow.evaluate_and_apply_route_advice(
                decision,
                recommended_route="direct",
                state_root=Path(temp),
                task=task,
                request=request,
            )
        self.assertEqual("ALLOW_ENFORCED", advice.gate_result)
        self.assertIs(True, advice.applied)

    def test_public_facade_has_no_forgeable_gate_result(self):
        self.assertTrue(hasattr(workflow, "evaluate_and_apply_route_advice"))
        self.assertNotIn(
            "gate_result",
            inspect.signature(workflow.apply_route_advice).parameters,
        )
        self.assertNotIn(
            "gate_result",
            inspect.signature(workflow.evaluate_and_apply_route_advice).parameters,
        )
        for module in (workflow, routing_mod):
            for name, obj in inspect.getmembers(module, inspect.isfunction):
                try:
                    parameters = inspect.signature(obj).parameters
                except (TypeError, ValueError):
                    continue
                self.assertNotIn(
                    "gate_result",
                    parameters,
                    f"{module.__name__}.{name} still accepts gate_result",
                )
        self.assertFalse(hasattr(routing_mod, "_apply_computed_route_advice"))
        with self.assertRaises(TypeError):
            workflow.evaluate_and_apply_route_advice(  # type: ignore[call-arg]
                workflow.decide_route(
                    valid_task(), route_request("SIMPLE", "READ_ONLY"), "shadow"
                ),
                gate_result="ALLOW_ENFORCED",
            )

    def test_caller_supplied_allow_enforced_does_not_apply(self):
        task = valid_task()
        request = route_request("BOUNDED", "WRITE")
        decision = workflow.decide_route(task, request, "enforced")
        self.assertEqual("delegated", decision.route)
        advice = evaluate_test_advice(
            decision,
            recommended_route="direct",
            metrics=supporting_metrics(),
            config=SHADOW_CONFIG,
            task=task,
            request=request,
        )
        self.assertEqual("KEEP_SHADOW", advice.gate_result)
        self.assertIs(False, advice.applied)
        self.assertEqual(decision.effective_roles, advice.roles)

    def test_evaluate_and_apply_enforced_uses_internal_gate_not_routing_mode(self):
        task = valid_task()
        request = route_request("BOUNDED", "WRITE")
        decision = workflow.decide_route(task, request, "enforced")
        denied = evaluate_test_advice(
            decision,
            recommended_route="direct",
            metrics=supporting_metrics(paired=7),
            config=ENFORCED_CONFIG,
            task=task,
            request=request,
        )
        self.assertEqual("FALLBACK_FIXED", denied.gate_result)
        self.assertIs(False, denied.applied)
        allowed = evaluate_test_advice(
            decision,
            recommended_route="direct",
            metrics=supporting_metrics(),
            config=ENFORCED_CONFIG,
            task=task,
            request=request,
        )
        self.assertEqual("ALLOW_ENFORCED", allowed.gate_result)
        self.assertIs(True, allowed.applied)
        self.assertEqual((), allowed.roles)


class BoundInputAndDowngradeTest(unittest.TestCase):
    def test_hash_mismatch_between_decision_and_inputs_is_rejected(self):
        risky_task = valid_task(risk_flags=["SECURITY"])
        risky_request = route_request("SIMPLE", "WRITE", ["SECURITY"])
        decision = workflow.decide_route(risky_task, risky_request, "enforced")
        clean_task = valid_task()
        clean_request = route_request("SIMPLE", "READ_ONLY")
        with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_ADVICE_HASH_MISMATCH"):
            evaluate_test_advice(
                decision,
                recommended_route="direct",
                metrics=supporting_metrics(),
                config=ENFORCED_CONFIG,
                task=clean_task,
                request=clean_request,
            )

    def test_route_escalation_is_not_applied(self):
        task = valid_task()
        request = route_request("SIMPLE", "WRITE")
        decision = workflow.decide_route(task, request, "enforced")
        self.assertEqual("direct", decision.route)
        advice = evaluate_test_advice(
            decision,
            recommended_route="delegated",
            metrics=supporting_metrics(),
            config=ENFORCED_CONFIG,
            task=task,
            request=request,
        )
        self.assertEqual("FALLBACK_FIXED", advice.gate_result)
        self.assertIs(False, advice.applied)
        self.assertEqual(decision.effective_roles, advice.roles)

        sol_request = route_request("PLANNING_ONLY", "READ_ONLY")
        sol_decision = workflow.decide_route(task, sol_request, "enforced")
        self.assertEqual("sol_only", sol_decision.route)
        upgraded = evaluate_test_advice(
            sol_decision,
            recommended_route="delegated",
            metrics=supporting_metrics(),
            config=ENFORCED_CONFIG,
            task=task,
            request=sol_request,
        )
        self.assertEqual("FALLBACK_FIXED", upgraded.gate_result)
        self.assertIs(False, upgraded.applied)


class GateEvidenceFailClosedTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _create(self, task_id):
        task = valid_task()
        task["task_id"] = task_id
        self.store.create_task(task)
        return task_id

    def _record(self, task_id, run):
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            workflow.record_metrics(task_id, run)

    def test_missing_p0_or_p1_on_a_covered_run_is_none(self):
        task_id = self._create("AWF-20260803-011")
        self._record(
            task_id,
            {
                "role": "terra",
                "period": "experiment",
                "data_origin": "runtime",
                "status": "IMPLEMENTED_CANDIDATE",
                "p1_miss_count": 0,
            },
        )
        metrics = workflow.aggregate_metrics(self.state_root)
        self.assertIsNone(metrics["p0_miss_count"])
        self.assertEqual(0, metrics["p1_miss_count"])
        self.assertEqual("FALLBACK_FIXED", workflow.evaluate_optimization_gate(metrics))

    def test_no_records_leave_p0_and_p1_none(self):
        self._create("AWF-20260803-012")
        metrics = workflow.aggregate_metrics(self.state_root)
        self.assertIsNone(metrics["p0_miss_count"])
        self.assertIsNone(metrics["p1_miss_count"])
        self.assertEqual("FALLBACK_FIXED", workflow.evaluate_optimization_gate(metrics))

    def test_defaulted_period_or_origin_cannot_open_the_gate(self):
        task_id = self._create("AWF-20260803-013")
        self._record(
            task_id,
            {
                "role": "terra",
                "status": "IMPLEMENTED_CANDIDATE",
                "p0_miss_count": 0,
                "p1_miss_count": 0,
            },
        )
        metrics = workflow.aggregate_metrics(self.state_root)
        self.assertIsNone(metrics["p0_miss_count"])
        self.assertIsNone(metrics["p1_miss_count"])
        self.assertIsNone(metrics["calibration_first_delivery_pass_rate"])
        self.assertIsNone(metrics["experiment_first_delivery_pass_rate"])
        self.assertEqual("FALLBACK_FIXED", workflow.evaluate_optimization_gate(metrics))


class AdviceCombinationAndRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)
        self.task = valid_task()
        self.store.create_task(self.task)
        self.request = route_request("SIMPLE", "READ_ONLY")
        self.decision = workflow.decide_route(self.task, self.request, "shadow")
        workflow.record_route_decision(self.store, self.task["task_id"], self.decision)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_illegal_mode_gate_applied_combinations_are_rejected(self):
        cases = (
            {"optimization_mode": "shadow", "gate_result": "ALLOW_ENFORCED", "applied": True},
            {"optimization_mode": "shadow", "gate_result": "KEEP_SHADOW", "applied": True},
            {"optimization_mode": "shadow", "gate_result": "FALLBACK_FIXED", "applied": False},
            {"optimization_mode": "enforced", "gate_result": "KEEP_SHADOW", "applied": False},
            {"optimization_mode": "enforced", "gate_result": "ALLOW_ENFORCED", "applied": False},
            {"optimization_mode": "enforced", "gate_result": "FALLBACK_FIXED", "applied": True},
        )
        for overrides in cases:
            document = valid_advice_document(
                task_sha256=self.decision.task_sha256,
                request_sha256=self.decision.request_sha256,
                **overrides,
            )
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_ADVICE_INVALID"):
                    workflow.validate_route_advice(document)
                with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_ADVICE_INVALID"):
                    workflow.record_route_advice(self.store, self.task["task_id"], document)

    def test_record_rejects_actual_route_not_bound_to_stored_effective_route(self):
        document = valid_advice_document(
            actual_route="direct",
            task_sha256=self.decision.task_sha256,
            request_sha256=self.decision.request_sha256,
        )
        workflow.validate_route_advice(document)
        with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_ADVICE"):
            workflow.record_route_advice(self.store, self.task["task_id"], document)

    def test_sidecar_retries_restore_missing_event_without_duplicating(self):
        advice = evaluate_test_advice(
            self.decision,
            recommended_route="direct",
            config=SHADOW_CONFIG,
            task=self.task,
            request=self.request,
        )
        path = workflow.record_route_advice(
            self.store,
            self.task["task_id"],
            advice,
            request_sha256=workflow.artifact_sha256(self.request),
        )
        events_path = self.state_root / self.task["task_id"] / "events.jsonl"
        kept = [
            line
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event_type") != "ROUTE_ADVICE_RECORDED"
        ]
        events_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        self.assertTrue(path.is_file())
        workflow.record_route_advice(
            self.store,
            self.task["task_id"],
            advice,
            request_sha256=workflow.artifact_sha256(self.request),
        )
        events = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        ]
        advice_events = [event for event in events if event["event_type"] == "ROUTE_ADVICE_RECORDED"]
        self.assertEqual(1, len(advice_events))
        self.assertEqual(advice.to_advice_dict()["recommended_route"], advice_events[0]["recommended_route"])


class GateCoveredCostSummaryTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _create(self, task_id):
        task = valid_task()
        task["task_id"] = task_id
        self.store.create_task(task)
        return task

    def _record(self, task_id, run, *, controller=False):
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            recorder = (
                workflow._record_controller_metrics
                if controller
                else workflow.record_metrics
            )
            recorder(task_id, run, state_root=self.state_root)

    def test_undeclared_cost_pairs_cannot_open_the_gate_even_with_valid_misses(self):
        calibration = self._create("AWF-20260803-021")
        experiment = self._create("AWF-20260803-022")
        cost_task = self._create("AWF-20260803-023")
        self._record(
            calibration["task_id"],
            {
                "role": "terra",
                "period": "calibration",
                "data_origin": "runtime",
                "status": "IMPLEMENTED_CANDIDATE",
                "p0_miss_count": 0,
                "p1_miss_count": 0,
            },
        )
        self._record(
            experiment["task_id"],
            {
                "role": "terra",
                "period": "experiment",
                "data_origin": "runtime",
                "status": "IMPLEMENTED_CANDIDATE",
                "p0_miss_count": 0,
                "p1_miss_count": 0,
            },
        )
        for index in range(1, 9):
            self._record(
                cost_task["task_id"],
                {
                    "role": "luna",
                    "cost_evidence": {
                        "schema_version": "cost-evidence-1",
                        "route": "delegated",
                        "role": "luna",
                        "execution_surface": "NATIVE_SUBAGENT",
                        "duration_seconds": 1.0,
                        "prompt_bytes": 8,
                        "input_tokens": 10,
                        "cached_input_tokens": 0,
                        "output_tokens": 2,
                        "retry_kind": "none",
                        "verification_seconds": 0.0,
                        "quality_outcome": "SUPPORTED",
                        "paired_case_id": f"case-{index:02d}",
                        "evidence_class": "measured",
                        "rate_snapshot_id": None,
                        "net_measured_cost_delta": -1.0,
                        "quality_delta_points": 0.0,
                    },
                },
                controller=True,
            )
        metrics = workflow.aggregate_metrics(self.state_root)
        self.assertEqual(0, metrics["p0_miss_count"])
        self.assertEqual(0, metrics["p1_miss_count"])
        self.assertEqual(1.0, metrics["calibration_first_delivery_pass_rate"])
        self.assertEqual(1.0, metrics["experiment_first_delivery_pass_rate"])
        self.assertEqual({}, metrics["cost_summary"])
        self.assertEqual("FALLBACK_FIXED", workflow.evaluate_optimization_gate(metrics))


class ConfiguredRolePolicyAdviceTest(unittest.TestCase):
    def test_evaluate_and_apply_uses_routing_role_policy_from_config(self):
        task = valid_task()
        request = route_request("BOUNDED", "WRITE")
        decision = workflow.decide_route(task, request, "enforced")
        config = {
            "routing": {"mode": "enforced", "role_policy": "legacy"},
            "optimization": {
                "mode": "enforced",
                "minimum_paired_cases": 8,
                "compact_prompts": False,
            },
        }
        advice = evaluate_test_advice(
            decision,
            recommended_route="sol_only",
            metrics=supporting_metrics(),
            config=config,
            task=task,
            request=request,
        )
        self.assertEqual("ALLOW_ENFORCED", advice.gate_result)
        self.assertIs(True, advice.applied)
        self.assertEqual(("sol_planner",), advice.roles)

    def test_missing_routing_role_policy_fails_closed(self):
        task = valid_task()
        request = route_request("BOUNDED", "WRITE")
        decision = workflow.decide_route(task, request, "enforced")
        with self.assertRaisesRegex(workflow.WorkflowError, "ROLE_POLICY_INVALID"):
            evaluate_test_advice(
                decision,
                recommended_route="direct",
                metrics=supporting_metrics(),
                config={
                    "optimization": {
                        "mode": "enforced",
                        "minimum_paired_cases": 8,
                        "compact_prompts": False,
                    }
                },
                task=task,
                request=request,
            )


class CompactPromptDecisionTest(unittest.TestCase):
    def test_builder_rejects_caller_asserted_compact_or_gate(self):
        task = valid_task()
        contract = {"acceptance_commands": task["acceptance_commands"]}
        for function in (
            workflow.build_role_prompt,
            workflow.build_role_prompt_result,
            workflow.resolve_compact_prompt_decision,
        ):
            parameters = inspect.signature(function).parameters
            self.assertNotIn("compact", parameters)
            self.assertNotIn("compact_prompts", parameters)
            self.assertNotIn("gate_result", parameters)
            self.assertNotIn("applied", parameters)
            with self.assertRaises(TypeError):
                if function is workflow.resolve_compact_prompt_decision:
                    function(compact=True)
                else:
                    function("luna", task, contract, (), compact=True)

    def test_dual_key_requires_flag_enforced_mode_and_allow_enforced_gate(self):
        armed, reason = workflow.resolve_compact_prompt_decision(
            config=ARMED_COMPACT_CONFIG,
            metrics=supporting_metrics(),
        )
        self.assertIs(True, armed)
        self.assertEqual("armed", reason)

        negatives = {
            "flag_false": (ENFORCED_CONFIG, supporting_metrics(), "compact_flag_false"),
            "shadow": (SHADOW_COMPACT_CONFIG, supporting_metrics(), "mode_not_enforced"),
            "under_eight_pairs": (ARMED_COMPACT_CONFIG, supporting_metrics(paired=7), "gate_not_armed"),
            "p0_miss": (ARMED_COMPACT_CONFIG, supporting_metrics(p0=1), "gate_not_armed"),
            "p1_miss": (ARMED_COMPACT_CONFIG, supporting_metrics(p1=1), "gate_not_armed"),
            "first_delivery_drop": (
                ARMED_COMPACT_CONFIG,
                supporting_metrics(calibration=0.9, experiment=0.8),
                "gate_not_armed",
            ),
            "missing_metrics": (ARMED_COMPACT_CONFIG, None, "metrics_missing"),
            "illegal_metrics": (ARMED_COMPACT_CONFIG, ["not-a-mapping"], "metrics_missing"),
            "synthetic": (
                ARMED_COMPACT_CONFIG,
                supporting_metrics(synthetic=True),
                "synthetic",
            ),
        }
        for name, (config, metrics, expected_reason) in negatives.items():
            with self.subTest(name=name):
                armed, reason = workflow.resolve_compact_prompt_decision(
                    config=config,
                    metrics=metrics,
                )
                self.assertIs(False, armed)
                self.assertEqual(expected_reason, reason)


class CompactPromptProjectionTest(unittest.TestCase):
    def _evidence(self, directory):
        path = Path(directory) / "evidence.txt"
        path.write_text("verified fact", encoding="utf-8")
        return path

    def _luna_contract(self, task):
        return {
            "acceptance_commands": task["acceptance_commands"],
            "verification_level": task["verification_level"],
        }

    def test_default_and_unarmed_paths_keep_full_prompt(self):
        task = valid_task()
        contract = self._luna_contract(task)
        with tempfile.TemporaryDirectory() as temp:
            evidence = self._evidence(temp)
            baseline = workflow.build_role_prompt("luna", task, contract, [evidence])
            cases = (
                {"config": ENFORCED_CONFIG, "metrics": supporting_metrics()},
                {"config": SHADOW_COMPACT_CONFIG, "metrics": supporting_metrics()},
                {"config": ARMED_COMPACT_CONFIG, "metrics": supporting_metrics(paired=7)},
                {"config": ARMED_COMPACT_CONFIG, "metrics": supporting_metrics(p0=1)},
                {"config": ARMED_COMPACT_CONFIG, "metrics": supporting_metrics(p1=1)},
                {
                    "config": ARMED_COMPACT_CONFIG,
                    "metrics": supporting_metrics(calibration=0.9, experiment=0.8),
                },
                {"config": ARMED_COMPACT_CONFIG, "metrics": None},
                {"config": ARMED_COMPACT_CONFIG, "metrics": supporting_metrics(synthetic=True)},
            )
            for kwargs in cases:
                with self.subTest(kwargs=kwargs):
                    result = compact_role_prompt("luna", task, contract, [evidence], **kwargs)
                    self.assertEqual("full", result.mode)
                    self.assertEqual(baseline, result.prompt)
                    self.assertEqual(len(baseline.encode("utf-8")), result.prompt_bytes)

    def test_armed_compact_is_a_projection_not_a_summary(self):
        task = valid_task()
        task["paired_case_id"] = "case-compact-01"
        contract = self._luna_contract(task)
        with tempfile.TemporaryDirectory() as temp:
            evidence = self._evidence(temp)
            full = compact_role_prompt(
                "luna",
                task,
                contract,
                [evidence],
                config=ENFORCED_CONFIG,
                metrics=supporting_metrics(),
            )
            compact = compact_role_prompt(
                "luna",
                task,
                contract,
                [evidence],
                config=ARMED_COMPACT_CONFIG,
                metrics=supporting_metrics(),
            )
        self.assertEqual("compact", compact.mode)
        self.assertEqual("armed", compact.reason)
        self.assertEqual(len(compact.prompt.encode("utf-8")), compact.prompt_bytes)
        self.assertLess(compact.prompt_bytes, full.prompt_bytes)
        self.assertIn("Handle only bounded tasks.", compact.prompt)
        self.assertIn('Output "role" exactly as "luna".', compact.prompt)
        self.assertIn("only output ai-result-1 JSON", compact.prompt)
        self.assertIn("For Luna L1, output 1 to 5 claims", compact.prompt)
        self.assertIn("Do not write, modify, delete, stage, commit, merge, or push", compact.prompt)
        self.assertNotIn("Task envelope:", compact.prompt)
        self.assertNotIn("Task contract:", compact.prompt)
        self.assertNotIn("summar", compact.prompt.lower())
        for field in TASK_FIDELITY_FIELDS:
            _assert_value_preserved(self, compact.prompt, task[field])
        self.assertIn("case-compact-01", compact.prompt)
        self.assertIn(str(evidence), compact.prompt)

    def test_unknown_contract_field_is_kept_or_disables_compact(self):
        task = valid_task()
        secret = "OWNER-TICKET-UNLISTED-9f3c"
        contract = {
            **self._luna_contract(task),
            "mystery_authorization_ticket": secret,
        }
        result = compact_role_prompt(
            "luna",
            task,
            contract,
            (),
            config=ARMED_COMPACT_CONFIG,
            metrics=supporting_metrics(),
        )
        self.assertIn(secret, result.prompt)
        if result.mode == "compact":
            self.assertIn("mystery_authorization_ticket", result.prompt)

    def test_role_identity_and_overdesign_ban_survive_compact(self):
        task = valid_task(task_type="ACCEPTANCE")
        planner_task = valid_task()
        contracts = {
            "luna": self._luna_contract(planner_task),
            "sol_planner": {
                "acceptance_criteria": "plan stays inside the frozen objective",
                "required_output_schema": "ai-plan-1",
            },
            "sol_reviewer": {
                "acceptance_criteria": "candidate matches the frozen contract",
                "required_output_schema": "ai-result-1",
            },
            "sol_medium_reviewer": {
                "acceptance_criteria": "final acceptance stays read-only",
                "required_output_schema": "ai-result-1",
            },
            "sol_xhigh": {
                "authorization_ticket": "XHIGH-PLAN-1",
                "required_output_schema": "ai-result-1",
            },
            "sol_xhigh_planner": {
                "authorization_ticket": "XHIGH-PLAN-2",
                "required_output_schema": "ai-plan-1",
            },
            "terra_xhigh_planner": {
                "acceptance_criteria": "produce only the selected bounded plan",
                "required_output_schema": "ai-plan-1",
            },
            "terra_xhigh_reviewer": {
                "acceptance_criteria": "review only the pinned candidate",
                "required_output_schema": "ai-result-1",
            },
        }
        for role, contract in contracts.items():
            selected = task if "reviewer" in role or role == "sol_xhigh" else planner_task
            with self.subTest(role=role):
                result = compact_role_prompt(
                    role,
                    selected,
                    contract,
                    (),
                    config=ARMED_COMPACT_CONFIG,
                    metrics=supporting_metrics(),
                )
                self.assertEqual("compact", result.mode)
                instructions = workflow._load_role_config(role)["instructions"]
                self.assertIn(instructions, result.prompt)
                self.assertIn(f'Output "role" exactly as "{role}".', result.prompt)
                if role.startswith("sol_"):
                    self.assertIn("Do not over-design", result.prompt)
                    self.assertIn("smallest change", result.prompt)
                for field in TASK_FIDELITY_FIELDS:
                    _assert_value_preserved(self, result.prompt, selected[field])
                for field in CONTRACT_FIDELITY_FIELDS:
                    if field in contract:
                        _assert_value_preserved(self, result.prompt, contract[field])


class CompactConstructionPromptTest(unittest.TestCase):
    def _context(self):
        from tests.test_ai_workflow_construction_execution import (
            construction_plan,
            remediation_task,
        )

        task = remediation_task()
        frozen = workflow.validate_plan(construction_plan(task=task), task)
        context = workflow.ConstructionExecutionContext(
            plan=frozen,
            step=frozen.tasks[0],
            dispatch_id="d" * 64,
            task_sha256=frozen.task_sha256,
            request_sha256="e" * 64,
            role="luna_construction",
        )
        return task, context

    def test_construction_and_terra_prompts_use_the_same_renderer(self):
        task, context = self._context()
        with mock.patch.object(
            workflow, "_load_workflow_config", return_value=pinned_config_with(ARMED_COMPACT_CONFIG["optimization"])
        ), mock.patch.object(workflow, "aggregate_metrics", return_value=supporting_metrics()):
            compact = workflow.build_construction_role_prompt_result(
                task,
                context,
                state_root=Path("."),
            )
        via_role = compact_role_prompt(
            context.role,
            task,
            context.contract(),
            (),
            config=ARMED_COMPACT_CONFIG,
            metrics=supporting_metrics(),
        )
        self.assertEqual("compact", compact.mode)
        self.assertEqual(via_role.prompt, compact.prompt)
        contract = context.contract()
        for field in (
            "dispatch_id",
            "plan_sha256",
            "task_sha256",
            "request_sha256",
            "subtask_id",
            "write_scope",
            "first_artifact",
        ):
            _assert_value_preserved(self, compact.prompt, contract[field])
        self.assertIn("luna_construction", compact.prompt)
        self.assertIn(task["objective"], compact.prompt)
        self.assertIn(
            workflow._load_role_config("luna_construction")["instructions"],
            compact.prompt,
        )
        with mock.patch.object(
            workflow, "_load_workflow_config", return_value=pinned_config_with(ENFORCED_CONFIG["optimization"])
        ), mock.patch.object(workflow, "aggregate_metrics", return_value=supporting_metrics()):
            full = workflow.build_construction_role_prompt_result(
                task,
                context,
                state_root=Path("."),
            )
        self.assertEqual("full", full.mode)
        self.assertLess(compact.prompt_bytes, full.prompt_bytes)

    def _paths(self, name):
        return workflow.RunPaths(
            repo=ROOT,
            output_path=ROOT / f".{name}-result.json",
            schema_path=ROOT / "config" / "ai_workflow_result.schema.json",
            logs_dir=ROOT / f".{name}-logs",
        )

    def _self_consistent(self, prompt, mode, reason):
        return workflow.PromptBuildResult(
            prompt=prompt,
            mode=mode,
            reason=reason,
            prompt_bytes=len(prompt.encode("utf-8")),
        )

    def _run_luna_construction(self, task, context, prompt, prompt_result=None):
        from tests.test_ai_workflow_construction_execution import construction_plan

        return workflow.run_codex(
            "luna_construction",
            task,
            prompt,
            self._paths("contract-reconcile"),
            construction_plan=construction_plan(task=task),
            construction_step_id="construction-601",
            construction_context=context,
            prompt_result=prompt_result,
        )

    def _assert_not_mismatch(self, task, context, prompt, prompt_result=None):
        with self.assertRaises(workflow.WorkflowError) as raised:
            self._run_luna_construction(task, context, prompt, prompt_result)
        self.assertNotEqual("CONSTRUCTION_PROMPT_MISMATCH", raised.exception.code)
        return raised.exception

    def test_compact_requires_controller_armed_builder_result(self):
        task, context = self._context()
        full = workflow.build_construction_role_prompt_result(task, context)
        compact_prompt = workflow._render_compact_role_prompt(
            context.role,
            workflow._load_role_config(context.role),
            task,
            context.contract(),
            (),
        )
        self.assertIsNotNone(compact_prompt)
        self.assertLess(len(compact_prompt.encode("utf-8")), full.prompt_bytes)
        self._assert_not_mismatch(task, context, full.prompt, full)
        forged = self._self_consistent(compact_prompt, "compact", "armed")
        with self.assertRaisesRegex(
            workflow.WorkflowError, "CONSTRUCTION_PROMPT_MISMATCH|INVALID_PROMPT"
        ):
            self._run_luna_construction(task, context, forged.prompt, forged)
        with self.assertRaisesRegex(
            workflow.WorkflowError, "CONSTRUCTION_PROMPT_MISMATCH"
        ):
            self._run_luna_construction(task, context, compact_prompt, None)

        with mock.patch.object(
            workflow,
            "_load_workflow_config",
            return_value=pinned_config_with(ARMED_COMPACT_CONFIG["optimization"]),
        ), mock.patch.object(
            workflow, "aggregate_metrics", return_value=supporting_metrics()
        ):
            controlled = workflow.build_construction_role_prompt_result(
                task,
                context,
                state_root=Path("."),
            )
        self.assertEqual("compact", controlled.mode)
        self._assert_not_mismatch(
            task, context, controlled.prompt, controlled
        )

    def test_unrelated_luna_full_or_missing_plan_hash_mismatches_even_with_result(self):
        task, context = self._context()
        luna_full = workflow.build_role_prompt(
            "luna",
            task,
            {"acceptance_commands": task["acceptance_commands"]},
            (),
        )
        luna_result = self._self_consistent(luna_full, "full", "compact_flag_false")
        with self.assertRaisesRegex(workflow.WorkflowError, "CONSTRUCTION_PROMPT_MISMATCH"):
            self._run_luna_construction(task, context, luna_full, luna_result)

        legal = workflow.build_construction_role_prompt(task, context)
        missing_hash = legal.replace(context.plan.plan_sha256, "0" * 64)
        self.assertNotEqual(legal, missing_hash)
        forged = self._self_consistent(missing_hash, "full", "compact_flag_false")
        with self.assertRaisesRegex(workflow.WorkflowError, "CONSTRUCTION_PROMPT_MISMATCH"):
            self._run_luna_construction(task, context, missing_hash, forged)

    def test_illegal_construction_prompt_with_result_mismatches(self):
        task, context = self._context()
        illegal = "Task envelope: {\"task_id\":\"AWF-20260808-601\"}\nnot the frozen contract"
        forged = self._self_consistent(illegal, "full", "compact_flag_false")
        with self.assertRaisesRegex(workflow.WorkflowError, "CONSTRUCTION_PROMPT_MISMATCH"):
            self._run_luna_construction(task, context, illegal, forged)

    def test_frozen_legal_prompt_ignores_later_aggregate_metrics(self):
        task, context = self._context()
        with mock.patch.object(
            workflow,
            "_load_workflow_config",
            return_value=pinned_config_with(ARMED_COMPACT_CONFIG["optimization"]),
        ), mock.patch.object(
            workflow, "aggregate_metrics", return_value=supporting_metrics()
        ):
            frozen = workflow.build_construction_role_prompt_result(
                task,
                context,
                state_root=Path("."),
            )
        self.assertEqual("compact", frozen.mode)

        def _forbidden_aggregate(_root):
            raise AssertionError("run_codex must not reaggregate metrics")

        with mock.patch.object(workflow, "aggregate_metrics", side_effect=_forbidden_aggregate):
            self._assert_not_mismatch(task, context, frozen.prompt, frozen)


class CompactPromptPluginMirrorTest(unittest.TestCase):
    def test_plugin_runtime_mirrors_compact_prompt_entry(self):
        root = (ROOT / "scripts" / "ai_workflow.py").read_bytes()
        plugin = (ROOT / "plugins" / "ai-workflow" / "runtime" / "ai_workflow.py").read_bytes()
        self.assertEqual(root, plugin)
        self.assertIn(b"resolve_compact_prompt_decision", root)
        self.assertIn(b"PromptBuildResult", root)


EVIDENCE_AUTHORIZATION_SENTENCES = (
    "Read the named evidence files at the listed paths before evaluating the task.",
    "Use only the task contract and named evidence above; no additional source material is authorized.",
)


def _parse_compact_context(prompt):
    for line in prompt.splitlines():
        if line.startswith("Context: "):
            return json.loads(line.removeprefix("Context: "))
    raise AssertionError("compact prompt has no Context projection")


class CompactPromptReviewFixTest(unittest.TestCase):
    def _luna_contract(self, task):
        return {
            "acceptance_commands": task["acceptance_commands"],
            "verification_level": task["verification_level"],
        }

    def test_compact_keeps_full_evidence_authorization_sentences(self):
        task = valid_task()
        result = compact_role_prompt(
            "luna",
            task,
            self._luna_contract(task),
            (),
            config=ARMED_COMPACT_CONFIG,
            metrics=supporting_metrics(),
        )
        self.assertEqual("compact", result.mode)
        full = workflow.build_role_prompt("luna", task, self._luna_contract(task), ())
        self.assertLess(result.prompt_bytes, len(full.encode("utf-8")))
        for sentence in EVIDENCE_AUTHORIZATION_SENTENCES:
            with self.subTest(sentence=sentence):
                self.assertIn(sentence, result.prompt)

    def test_fidelity_is_structural_and_rejects_substring_soup(self):
        task = valid_task()
        task["objective"] = "null"
        contract = {
            "acceptance_criteria": "",
            "dependencies": [],
            "permission_profile": False,
        }
        result = compact_role_prompt(
            "luna",
            task,
            contract,
            (),
            config=ARMED_COMPACT_CONFIG,
            metrics=supporting_metrics(),
        )
        self.assertEqual("compact", result.mode)
        context = _parse_compact_context(result.prompt)
        self.assertIsNone(context["source_worktree"])
        self.assertEqual("null", context["objective"])
        self.assertEqual([], context["risk_flags"])
        self.assertEqual("", context["acceptance_criteria"])
        self.assertEqual([], context["dependencies"])
        self.assertIs(False, context["permission_profile"])
        soup = "\n".join(
            (
                f"Role instructions: {workflow._load_role_config('luna')['instructions']}",
                "null",
                "[]",
                "false",
                'Output "role" exactly as "luna".',
                *(_canonical(task[field]) for field in TASK_FIDELITY_FIELDS),
                *(_canonical(value) for value in contract.values()),
            )
        )
        self.assertFalse(
            workflow._compact_projection_is_faithful(
                soup,
                "luna",
                workflow._load_role_config("luna"),
                task,
                contract,
            )
        )

    def test_production_builders_reject_config_and_metrics_kwargs(self):
        task = valid_task()
        contract = self._luna_contract(task)
        from tests.test_ai_workflow_construction_execution import (
            construction_plan,
            remediation_task,
        )

        construction_task = remediation_task()
        frozen = workflow.validate_plan(construction_plan(task=construction_task), construction_task)
        context = workflow.ConstructionExecutionContext(
            plan=frozen,
            step=frozen.tasks[0],
            dispatch_id="d" * 64,
            task_sha256=frozen.task_sha256,
            request_sha256="e" * 64,
            role="luna_construction",
        )
        for function, args in (
            (workflow.build_role_prompt, ("luna", task, contract, ())),
            (workflow.build_role_prompt_result, ("luna", task, contract, ())),
            (workflow.build_construction_role_prompt, (construction_task, context)),
            (workflow.build_construction_role_prompt_result, (construction_task, context)),
        ):
            with self.subTest(function=function.__name__):
                parameters = inspect.signature(function).parameters
                self.assertNotIn("config", parameters)
                self.assertNotIn("metrics", parameters)
                with self.assertRaises(TypeError):
                    function(*args, config=ARMED_COMPACT_CONFIG, metrics=supporting_metrics())

    def test_oversized_compact_falls_back_to_full(self):
        task = valid_task()
        with mock.patch.object(
            workflow,
            "_render_compact_role_prompt",
            return_value="x" * 20000,
        ):
            result = compact_role_prompt(
                "luna",
                task,
                self._luna_contract(task),
                (),
                config=ARMED_COMPACT_CONFIG,
                metrics=supporting_metrics(),
            )
        self.assertEqual("full", result.mode)
        self.assertEqual("compact_not_smaller", result.reason)
        self.assertLess(len(result.prompt.encode("utf-8")), 20000)

    def test_forged_compact_result_is_not_recorded(self):
        from tests.test_ai_workflow import write_codex_result

        task = valid_task()
        task["verification_level"] = "L0"
        forged = workflow.PromptBuildResult(
            prompt="task contract",
            mode="compact",
            reason="armed",
            prompt_bytes=1,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_root = root / "state"
            workflow.WorkflowStore(state_root).create_task(task)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
                state_root=state_root,
            )
            result = {
                "schema_version": "ai-result-1",
                "role": "luna",
                "status": "SUPPORTED",
                "summary": "ok",
                "claims": [],
                "evidence": [],
                "counter_checks": [],
                "changed_files": [],
                "blind_spots": [],
                "unresolved_questions": [],
                "recommended_next_state": "EVIDENCE_READY",
            }

            def write_result(command, *args, **kwargs):
                write_codex_result(command, result)
                return subprocess.CompletedProcess(command, 0, stdout='{"event":"done"}\n', stderr="")

            with mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h", ())), \
                mock.patch.object(workflow, "working_tree_paths", return_value=set()), \
                mock.patch.object(workflow.subprocess, "run", side_effect=write_result):
                try:
                    workflow.run_codex(
                        "luna",
                        task,
                        "task contract",
                        paths,
                        prompt_result=forged,
                    )
                except workflow.WorkflowError as exc:
                    self.assertEqual("INVALID_PROMPT", exc.code)
                    metrics = workflow.aggregate_metrics(state_root)
                    self.assertEqual(0, metrics.get("compact_applied_count", 0))
                    return
            metrics = workflow.aggregate_metrics(state_root)
            self.assertEqual(0, metrics.get("compact_applied_count", 0))

    def test_construction_does_not_reaggregate_a_frozen_prompt_result(self):
        from tests.test_ai_workflow_construction_execution import (
            construction_plan,
            remediation_task,
        )

        task = remediation_task()
        frozen = workflow.validate_plan(construction_plan(task=task), task)
        context = workflow.ConstructionExecutionContext(
            plan=frozen,
            step=frozen.tasks[0],
            dispatch_id="d" * 64,
            task_sha256=frozen.task_sha256,
            request_sha256="e" * 64,
            role="luna_construction",
        )
        with mock.patch.object(
            workflow, "_load_workflow_config", return_value=pinned_config_with(ARMED_COMPACT_CONFIG["optimization"])
        ), mock.patch.object(workflow, "aggregate_metrics", return_value=supporting_metrics()):
            built = workflow.build_construction_role_prompt_result(
                task,
                context,
                state_root=Path("."),
            )
        self.assertEqual("compact", built.mode)
        paths = workflow.RunPaths(
            repo=ROOT,
            output_path=ROOT / ".toctou-luna-result.json",
            schema_path=ROOT / "config" / "ai_workflow_result.schema.json",
            logs_dir=ROOT / ".toctou-luna-logs",
            state_root=ROOT / ".toctou-state",
        )
        with self.assertRaises(workflow.WorkflowError) as raised:
            workflow.run_codex(
                "luna_construction",
                task,
                built.prompt,
                paths,
                construction_plan=construction_plan(task=task),
                construction_step_id="construction-601",
                construction_context=context,
                prompt_result=built,
            )
        self.assertNotEqual("CONSTRUCTION_PROMPT_MISMATCH", raised.exception.code)

    def test_repair_assignment_prompt_stays_full_and_is_not_a_compact_surface(self):
        from scripts import ai_workflow_repairs as repairs

        self.assertEqual("full_only", repairs.REPAIR_PROMPT_COMPACT_POLICY)
        source = inspect.getsource(repairs._v2_assignment_prompt)
        self.assertIn("Task:", source)
        self.assertIn("Assignment:", source)
        self.assertNotIn("build_role_prompt", source)
        self.assertNotIn("build_role_prompt_result", source)
        self.assertNotIn("Context:", source)


class CompactPromptMetricsTest(unittest.TestCase):
    def test_compact_applied_metric_does_not_change_the_gate(self):
        metrics = supporting_metrics()
        metrics["compact_applied_count"] = 12
        metrics["average_prompt_bytes"] = 10
        self.assertEqual("ALLOW_ENFORCED", workflow.evaluate_optimization_gate(metrics))
        self.assertEqual(
            inspect.signature(workflow._controller_cost_attempt).parameters["compact_applied"].default,
            False,
        )

    def test_aggregate_metrics_counts_compact_applied_without_touching_cost_summary(self):
        from tests.test_ai_workflow import MetricsReportTest

        helper = MetricsReportTest()
        helper.setUp()
        try:
            task_id = helper._create_task("AWF-20260803-041", paired_case_id="case-compact")
            task = workflow.load_task(helper.state_root / task_id / "task.json")
            workflow._controller_cost_attempt(
                task_id,
                task,
                "luna",
                workflow.NATIVE_SUBAGENT,
                1.0,
                80,
                None,
                "none",
                "SUPPORTED",
                helper.state_root,
                compact_applied=True,
            )
            workflow._controller_cost_attempt(
                task_id,
                task,
                "luna",
                workflow.NATIVE_SUBAGENT,
                1.0,
                120,
                None,
                "none",
                "SUPPORTED",
                helper.state_root,
                compact_applied=False,
            )
            metrics = workflow.aggregate_metrics(helper.state_root)
            self.assertEqual(1, metrics["compact_applied_count"])
            self.assertEqual(100.0, metrics["average_prompt_bytes"])
            self.assertNotIn("compact_applied_count", metrics.get("cost_summary", {}))
            self.assertEqual("FALLBACK_FIXED", workflow.evaluate_optimization_gate(metrics))
        finally:
            helper.tearDown()


if __name__ == "__main__":
    unittest.main()
