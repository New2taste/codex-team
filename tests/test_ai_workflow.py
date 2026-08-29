import ast
import functools
import inspect
import json
import os
import subprocess
import tempfile
import tomllib
import argparse
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_declarations as declarations
from scripts import ai_workflow_dispatch_policy as policy
from scripts import ai_workflow_ownership as ownership
from scripts import ai_workflow_preflight as preflight


ROOT = Path(__file__).resolve().parents[1]


def write_codex_result(command, result):
    """Model the Codex ``-o`` contract by writing the requested fresh output."""

    output_path = Path(command[command.index("-o") + 1])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result), encoding="utf-8")
    return subprocess.CompletedProcess(command, 0, stdout='{"event":"done"}\n', stderr="")


HUB_SELF_LOCK_WRAPPERS = (
    "require_dispatch_permit",
    "precheck_dispatch_permit",
    "release_permit_before_start",
    "consume_owner_authorization",
    "has_unresolved_ownership_violation",
    "run_role_preflight",
    "is_role_preflighted",
    "require_role_preflighted",
    "load_route_declaration",
    "require_verdict_fresh",
)


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _with_lock_blocks(function) -> list[ast.With]:
    tree = ast.parse(inspect.getsource(function))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    blocks: list[ast.With] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            expr = item.context_expr
            if (
                isinstance(expr, ast.Call)
                and isinstance(expr.func, ast.Attribute)
                and expr.func.attr == "lock"
            ):
                blocks.append(node)
                break
    return blocks


def _route_request_for(task: dict[str, object]) -> dict[str, object]:
    task_type = str(task["task_type"])
    if task_type == "PLAN":
        work_class, need = "PLANNING_ONLY", "READ_ONLY"
    elif task_type == "ACCEPTANCE":
        work_class, need = "BOUNDED", "READ_ONLY"
    else:
        work_class, need = "BOUNDED", "WRITE"
    return {
        "schema_version": "ai-route-request-1",
        "task_id": task["task_id"],
        "work_class": work_class,
        "execution_need": need,
        "decomposable": True,
        "risk_flags": list(task.get("risk_flags") or []),
        "reason_codes": ["PLAN_IS_DELIVERABLE"],
    }


def _seed_sessions(task: dict[str, object]) -> None:
    raw = task.get("source_worktree") or task["repository_root"]
    (Path(str(raw)) / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)


def _install_declaration(
    store: workflow.WorkflowStore,
    task: dict[str, object],
    *,
    allowed_roles: tuple[str, ...] = ("luna", "sol_planner"),
    active_roles: tuple[str, ...] | None = None,
    max_dispatches: int = 8,
    mode: str = "legacy",
) -> declarations.RouteDeclaration:
    _seed_sessions(task)
    task_id = str(task["task_id"])
    existing = declarations.load_route_declaration(store, task_id)
    if existing is not None:
        return existing
    request = _route_request_for(task)
    computed = workflow.decide_route(task, request, mode)
    decision = workflow.persist_or_reuse_route_decision(store, task_id, computed)
    declaration = declarations.build_route_declaration(
        decision=decision,
        route_config_hash=declarations.compute_route_config_hash(workflow._load_workflow_config()),
        allowed_roles=allowed_roles,
        active_roles=active_roles if active_roles is not None else allowed_roles[:1],
        rule_ids=(decision.rule_id,),
        reason_codes=("PLAN_IS_DELIVERABLE",),
        max_dispatches=max_dispatches,
        allowed_transitions=(),
    )
    with store.lock(task_id):
        recorded = declarations.ensure_route_declaration(store, task_id, declaration)
        for role in recorded.active_roles:
            preflight.run_role_preflight_locked(store, task_id, role)
    return recorded


def _compat_popen(handler, *, raise_on_communicate=None):
    class Popen(_RecordingPopen):
        def __init__(self, command, *args, **kwargs):
            super().__init__(command, *args, **kwargs)
            self._handler = handler
            self._handler_args = (command, args, kwargs)
            self._ran_handler = False
            self._communicate_error = raise_on_communicate

        def communicate(self, input=None, timeout=None):
            if self._delegate is not None:
                return super().communicate(input=input, timeout=timeout)
            self.input = input
            self.timeout = timeout
            if self._communicate_error is not None:
                raise self._communicate_error
            if not self._ran_handler:
                self._ran_handler = True
                command, args, kwargs = self._handler_args
                completed = self._handler(command, *args, **kwargs)
                if isinstance(completed, subprocess.CompletedProcess):
                    self.returncode = completed.returncode
                    self._stdout = completed.stdout or ""
                    self._stderr = completed.stderr or ""
                elif completed is not None:
                    self.returncode = getattr(completed, "returncode", 0) or 0
                    self._stdout = getattr(completed, "stdout", "") or ""
                    self._stderr = getattr(completed, "stderr", "") or ""
            return self._stdout, self._stderr

    return Popen


def _declared_codex_env(task: dict[str, object], *, role: str = "luna"):
    root = Path(tempfile.mkdtemp())
    state_root = root / "state"
    store = workflow.WorkflowStore(state_root)
    store.create_task(task)
    allowed = tuple(dict.fromkeys((role, "luna", "sol_planner", "sol_reviewer", "terra", "terra_xhigh")))
    _install_declaration(store, task, allowed_roles=allowed, active_roles=(role,))
    paths = workflow.RunPaths(
        repo=Path(str(task.get("source_worktree") or task["repository_root"])),
        output_path=root / f"{role}-result.json",
        schema_path=ROOT / "config/ai_workflow_result.schema.json",
        logs_dir=root / "logs",
        state_root=state_root,
    )
    return root, store, paths


class _RecordingPopen:
    calls: list[tuple[object, dict]] = []
    instances: list[object] = []
    _real = subprocess.Popen

    def __init__(self, command, *args, **kwargs):
        self.args = command
        self.killed = False
        self.returncode = 0
        self._stdout = '{"event":"done"}\n'
        self._stderr = ""
        self._delegate = None
        if not command or command[0] == "git" or "-o" not in list(command):
            self._delegate = type(self)._real(command, *args, **kwargs)
            return
        type(self).calls.append((command, kwargs))
        type(self).instances.append(self)
        result = getattr(self, "_result", None)
        if result is not None:
            write_codex_result(command, result)

    def communicate(self, input=None, timeout=None):
        self.input = input
        self.timeout = timeout
        if self._delegate is not None:
            return self._delegate.communicate(input=input, timeout=timeout)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        if self._delegate is not None:
            self._delegate.kill()

    def __enter__(self):
        if self._delegate is not None:
            return self._delegate.__enter__()
        return self

    def __exit__(self, *args):
        if self._delegate is not None:
            return self._delegate.__exit__(*args)
        return None

    def wait(self, timeout=None):
        if self._delegate is not None:
            return self._delegate.wait(timeout=timeout)
        return self.returncode

    def poll(self):
        if self._delegate is not None:
            return self._delegate.poll()
        return self.returncode

    @classmethod
    def reset(cls) -> None:
        cls.calls = []
        cls.instances = []


class WriteJsonOnceAtomicityTest(unittest.TestCase):
    def test_content_write_failure_leaves_no_frozen_target_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "frozen.json"
            real_named_temporary_file = tempfile.NamedTemporaryFile

            class FailingTemporary:
                def __init__(self, *args, **kwargs):
                    self.handle = real_named_temporary_file(*args, **kwargs)
                    self.name = self.handle.name

                def __enter__(self):
                    self.handle.__enter__()
                    return self

                def write(self, _value):
                    raise OSError("injected content write failure")

                def __exit__(self, exc_type, exc, traceback):
                    return self.handle.__exit__(exc_type, exc, traceback)

            with mock.patch.object(
                artifacts.tempfile,
                "NamedTemporaryFile",
                side_effect=FailingTemporary,
            ):
                with self.assertRaisesRegex(
                    workflow.WorkflowError, "ATOMIC_WRITE_FAILED"
                ):
                    workflow.write_json_once(
                        target,
                        {"value": 1},
                        conflict_code="FROZEN_CONFLICT",
                    )
            self.assertFalse(target.exists())

            workflow.write_json_once(
                target,
                {"value": 1},
                conflict_code="FROZEN_CONFLICT",
            )
            self.assertEqual({"value": 1}, json.loads(target.read_text(encoding="utf-8")))
            with self.assertRaisesRegex(workflow.WorkflowError, "FROZEN_CONFLICT"):
                workflow.write_json_once(
                    target,
                    {"value": 2},
                    conflict_code="FROZEN_CONFLICT",
                )


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
        self.assertEqual(
            {"type": "string", "minLength": 1},
            task_schema["properties"]["paired_case_id"],
        )
        self.assertNotIn("paired_case_id", task_schema["required"])
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

    def test_optional_paired_case_id_is_stored_and_legacy_tasks_remain_valid(self):
        legacy = self.valid_task()
        workflow.validate_task(legacy)
        task = self.valid_task()
        task["paired_case_id"] = "case-01"
        workflow.validate_task(task)
        with tempfile.TemporaryDirectory() as temporary:
            stored = workflow.WorkflowStore(Path(temporary)).create_task(task)
            self.assertEqual("case-01", json.loads(stored.read_text())["paired_case_id"])

    def test_optional_paired_case_id_rejects_blank_or_non_string_values(self):
        for value in ("", "   ", 1, None):
            task = self.valid_task()
            task["paired_case_id"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                workflow.WorkflowError, "(?:INVALID_TYPE|EMPTY_FIELD)"
            ):
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

    def _create_task(self, task_id, *, paired_case_id=None):
        task = TaskValidationTest().valid_task()
        task["task_id"] = task_id
        if paired_case_id is not None:
            task["paired_case_id"] = paired_case_id
        self.store.create_task(task)
        _install_declaration(
            self.store,
            task,
            allowed_roles=("luna", "sol_planner"),
            active_roles=("luna",),
        )
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

    def test_controller_appends_native_cost_attempts_for_failures_and_retries(self):
        task_id = self._create_task("AWF-20260803-003")
        task = workflow.load_task(self.state_root / task_id / "task.json")

        class RetryRunner:
            is_live_model = False

            def __init__(self):
                self.calls = 0

            def run(self, role, task):
                self.calls += 1
                if self.calls == 1:
                    raise workflow.WorkflowError("CODEX_EXIT_NONZERO", "synthetic failure")
                return workflow.FakeRunner().run(role, task)

        runner = RetryRunner()
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            result, state = workflow._run_role_with_technical_retry(
                self.store,
                task_id,
                task,
                "EVIDENCE_RUNNING",
                "luna",
                runner,
                workflow.RetryBudget(),
            )

        self.assertEqual("luna", result["role"])
        self.assertEqual("EVIDENCE_RUNNING", state)
        document = json.loads(
            (self.state_root / task_id / "metrics.json").read_text(encoding="utf-8")
        )
        attempts = [run["cost_evidence"] for run in document["runs"]]
        self.assertEqual(2, len(attempts))
        self.assertEqual(
            ["none", "technical"],
            [attempt["retry_kind"] for attempt in attempts],
        )
        self.assertEqual(
            ["NATIVE_SUBAGENT", "NATIVE_SUBAGENT"],
            [attempt["execution_surface"] for attempt in attempts],
        )
        self.assertEqual([None, None], [attempt["input_tokens"] for attempt in attempts])
        self.assertEqual(["unavailable", "unavailable"], [attempt["evidence_class"] for attempt in attempts])
        self.assertEqual([None, None], [attempt["paired_case_id"] for attempt in attempts])

    def test_unpaired_cost_attempt_is_reported_unavailable_and_not_aggregated(self):
        task_id = self._create_task("AWF-20260803-004")
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            workflow._record_controller_metrics(
                task_id,
                {
                    "role": "luna",
                    "cost_evidence": {
                        "schema_version": "cost-evidence-1",
                        "route": "blocked",
                        "role": "luna",
                        "execution_surface": "NATIVE_SUBAGENT",
                        "duration_seconds": 1.0,
                        "prompt_bytes": 0,
                        "input_tokens": None,
                        "cached_input_tokens": None,
                        "output_tokens": None,
                        "retry_kind": "none",
                        "verification_seconds": 0.0,
                        "quality_outcome": "FAILED",
                        "paired_case_id": None,
                        "evidence_class": "unavailable",
                        "rate_snapshot_id": None,
                    },
                },
            )
        metrics = workflow.aggregate_metrics(self.state_root)
        self.assertEqual({}, metrics["cost_summary"])
        self.assertEqual(1, metrics["cost_unavailable_attempt_count"])
        report = workflow.render_report(metrics)
        self.assertIn("- unavailable attempts: 1", report)

    def test_record_metrics_strips_model_supplied_cost_evidence(self):
        task_id = self._create_task("AWF-20260803-005")
        self._record(
            task_id,
            {
                "role": "luna",
                "cost_evidence": {
                    "schema_version": "cost-evidence-1",
                    "route": "direct",
                    "role": "luna",
                    "execution_surface": "NATIVE_SUBAGENT",
                    "duration_seconds": 1.0,
                    "prompt_bytes": 5,
                    "input_tokens": 999,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "retry_kind": "none",
                    "verification_seconds": 0.0,
                    "quality_outcome": "SUPPORTED",
                    "paired_case_id": "case-forged",
                    "evidence_class": "measured",
                    "rate_snapshot_id": None,
                },
            },
        )
        document = json.loads(
            (self.state_root / task_id / "metrics.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("cost_evidence", document["runs"][0])
        self.assertEqual({}, workflow.aggregate_metrics(self.state_root)["cost_summary"])

    def test_native_runner_stale_runtime_usage_is_not_cost_evidence(self):
        task_id = self._create_task("AWF-20260803-006")
        task = workflow.load_task(self.state_root / task_id / "task.json")

        class StaleRunner:
            is_live_model = False
            runtime_usage = {
                "input_tokens": 999,
                "cached_input_tokens": 1,
                "output_tokens": 2,
            }

            def run(self, role, task):
                return workflow.FakeRunner().run(role, task)

        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            workflow._run_role_with_technical_retry(
                self.store,
                task_id,
                task,
                "EVIDENCE_RUNNING",
                "luna",
                StaleRunner(),
                workflow.RetryBudget(),
            )
        document = json.loads(
            (self.state_root / task_id / "metrics.json").read_text(encoding="utf-8")
        )
        evidence = document["runs"][0]["cost_evidence"]
        self.assertEqual("unavailable", evidence["evidence_class"])
        self.assertIsNone(evidence["input_tokens"])

    def _live_codex_adapter(self, task, outcomes):
        """Return a minimal live runner that delegates each attempt to run_codex."""

        state_root = self.state_root
        remaining_outcomes = list(outcomes)

        class LiveCodexAdapter:
            is_live_model = True
            owns_cost_attempt_accounting = True

            def run(self, role, task, *, attempt_context=None):
                paths = workflow.RunPaths(
                    repo=ROOT,
                    output_path=state_root / task["task_id"] / f"{role}-result.json",
                    schema_path=ROOT / "config" / "ai_workflow_result.schema.json",
                    logs_dir=state_root / task["task_id"] / "logs",
                    state_root=state_root,
                )
                kwargs = (
                    {"attempt_context": attempt_context}
                    if attempt_context is not None
                    else {}
                )
                return workflow.run_codex(role, task, "live role prompt", paths, **kwargs)

        adapter = LiveCodexAdapter()

        def launch_codex(command, *args, **kwargs):
            outcome = remaining_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            if outcome == "failure":
                return subprocess.CompletedProcess(command, 23, stdout="", stderr="failed")
            return write_codex_result(command, workflow.FakeRunner().run("luna", task))

        return adapter, launch_codex

    def _run_live_codex_attempts(self, task_id, outcomes, budget):
        task = workflow.load_task(self.state_root / task_id / "task.json")
        adapter, launch_codex = self._live_codex_adapter(task, outcomes)
        with (
            mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root),
            mock.patch.object(
                workflow,
                "capture_repo",
                return_value=workflow.RepoSnapshot("pinned-head", ()),
            ),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(
                workflow.subprocess,
                "Popen",
                _compat_popen(launch_codex),
            ),
        ):
            result, state = workflow._run_role_with_technical_retry(
                self.store,
                task_id,
                task,
                "EVIDENCE_RUNNING",
                "luna",
                adapter,
                budget,
            )
        document = json.loads(
            (self.state_root / task_id / "metrics.json").read_text(encoding="utf-8")
        )
        return result, state, document["runs"]

    def test_live_codex_successful_attempt_is_recorded_once(self):
        task_id = self._create_task("AWF-20260803-010")

        result, state, runs = self._run_live_codex_attempts(
            task_id, ["success"], workflow.RetryBudget()
        )

        self.assertEqual("SUPPORTED", result["status"])
        self.assertEqual("EVIDENCE_RUNNING", state)
        self.assertEqual(1, len(runs))
        self.assertEqual("none", runs[0]["cost_evidence"]["retry_kind"])
        self.assertTrue(runs[0]["cost_evidence"]["attempt_id"])

    def test_live_codex_unrecovered_failure_is_recorded_once(self):
        task_id = self._create_task("AWF-20260803-011")

        result, state, runs = self._run_live_codex_attempts(
            task_id, ["failure"], workflow.RetryBudget(technical_retries=1)
        )

        self.assertIsNone(result)
        self.assertEqual("BLOCKED", state)
        self.assertEqual(1, len(runs))
        evidence = runs[0]["cost_evidence"]
        self.assertEqual("FAILED", evidence["quality_outcome"])
        self.assertEqual("none", evidence["retry_kind"])

    def test_live_codex_retry_records_inner_attempts_with_outer_retry_kinds(self):
        task_id = self._create_task("AWF-20260803-012")

        result, state, runs = self._run_live_codex_attempts(
            task_id, ["failure", "success"], workflow.RetryBudget()
        )

        self.assertEqual("SUPPORTED", result["status"])
        self.assertEqual("EVIDENCE_RUNNING", state)
        self.assertEqual(2, len(runs))
        self.assertEqual(
            ["none", "technical"],
            [run["cost_evidence"]["retry_kind"] for run in runs],
        )
        self.assertEqual(
            2,
            len({run["cost_evidence"]["attempt_id"] for run in runs}),
        )

    def test_live_codex_dispatch_passes_derived_strict_schema_to_codex(self):
        task_id = self._create_task("AWF-20260826-901")
        task = workflow.load_task(self.state_root / task_id / "task.json")
        adapter, launch_codex = self._live_codex_adapter(task, ["success"])
        captured_commands = []

        def recording_launch(command, *args, **kwargs):
            captured_commands.append(list(command))
            return launch_codex(command, *args, **kwargs)

        with (
            mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root),
            mock.patch.object(
                workflow,
                "capture_repo",
                return_value=workflow.RepoSnapshot("pinned-head", ()),
            ),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(
                workflow.subprocess,
                "Popen",
                _compat_popen(recording_launch),
            ),
        ):
            result, _state = workflow._run_role_with_technical_retry(
                self.store,
                task_id,
                task,
                "EVIDENCE_RUNNING",
                "luna",
                adapter,
                workflow.RetryBudget(),
            )
        self.assertEqual("SUPPORTED", result["status"])
        command = captured_commands[0]
        schema_arg = Path(command[command.index("--output-schema") + 1])
        output_arg = Path(command[command.index("-o") + 1])
        canonical = ROOT / "config" / "ai_workflow_result.schema.json"
        self.assertNotEqual(canonical.resolve(), schema_arg.resolve())
        self.assertEqual(output_arg.parent, schema_arg.parent)
        self.assertEqual(
            output_arg.name.removesuffix(".json") + ".schema.json", schema_arg.name
        )
        derived = json.loads(schema_arg.read_text(encoding="utf-8"))
        self.assertEqual(set(derived["required"]), set(derived["properties"]))
        self.assertEqual(["string", "null"], derived["properties"]["dispatch_id"]["type"])

    def test_invalid_role_result_is_recorded_as_failed_cost_attempt(self):
        task_id = self._create_task("AWF-20260803-008")
        task = workflow.load_task(self.state_root / task_id / "task.json")

        class InvalidRunner:
            is_live_model = False

            def run(self, role, task):
                return {"role": role, "status": "SUPPORTED"}

        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            result, state = workflow._run_role_with_technical_retry(
                self.store,
                task_id,
                task,
                "EVIDENCE_RUNNING",
                "luna",
                InvalidRunner(),
                workflow.RetryBudget(),
            )
        self.assertIsNone(result)
        self.assertEqual("BLOCKED", state)
        document = json.loads(
            (self.state_root / task_id / "metrics.json").read_text(encoding="utf-8")
        )
        self.assertEqual(2, len(document["runs"]))
        self.assertEqual(
            ["FAILED", "FAILED"],
            [run["cost_evidence"]["quality_outcome"] for run in document["runs"]],
        )

    def test_live_guard_failure_is_recorded_as_failed_cost_attempt(self):
        task_id = self._create_task("AWF-20260803-009")
        task = workflow.load_task(self.state_root / task_id / "task.json")

        class LiveRunner:
            is_live_model = True

            def run(self, role, task):
                return workflow.FakeRunner().run(role, task)

        with (
            mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root),
            mock.patch.object(workflow, "_reject_dirty_input"),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(
                workflow,
                "capture_repo",
                side_effect=[
                    workflow.RepoSnapshot("before", ()),
                    workflow.RepoSnapshot("after", ()),
                ],
            ),
        ):
            result, state = workflow._run_role_with_technical_retry(
                self.store,
                task_id,
                task,
                "EVIDENCE_RUNNING",
                "luna",
                LiveRunner(),
                workflow.RetryBudget(),
            )
        self.assertIsNone(result)
        self.assertEqual("BLOCKED", state)
        document = json.loads(
            (self.state_root / task_id / "metrics.json").read_text(encoding="utf-8")
        )
        self.assertEqual("FAILED", document["runs"][0]["cost_evidence"]["quality_outcome"])

    def test_aggregate_metrics_excludes_synthetic_fixture_records_but_keeps_production(self):
        task_id = self._create_task("AWF-20260803-007", paired_case_id="case-production")
        task = workflow.load_task(self.state_root / task_id / "task.json")
        workflow._controller_cost_attempt(
            task_id,
            task,
            "luna",
            workflow.NATIVE_SUBAGENT,
            1.0,
            10,
            None,
            "none",
            "SUPPORTED",
            self.state_root,
            base_metric_run={"period": "experiment", "data_origin": "runtime"},
        )
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "paired-cases.json").read_text(
                encoding="utf-8"
            )
        )
        synthetic_attempt = dict(fixture["cases"][0]["attempts"][0])
        workflow._record_controller_metrics(
            task_id,
            {
                "role": "terra",
                "data_origin": "synthetic_fixture",
                "cost_evidence": synthetic_attempt,
            },
            state_root=self.state_root,
        )
        metrics = workflow.aggregate_metrics(self.state_root)
        self.assertEqual({"case-production"}, set(metrics["cost_summary"]))
        self.assertEqual(1, metrics["synthetic_cost_attempt_count"])
        self.assertEqual({"luna": 1}, metrics["role_calls"])
        self.assertIn(
            "synthetic fixture records: 1 (not publishable)",
            workflow.render_report(metrics),
        )

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

    def test_aggregate_metrics_reports_calls_gates_prompt_and_cache_efficiency(self):
        task_id = self._create_task(
            "AWF-20260803-003", paired_case_id="case-efficiency"
        )
        task = workflow.load_task(self.state_root / task_id / "task.json")
        for prompt_bytes, input_tokens, cached_tokens in (
            (100, 100, 10),
            (300, 300, 60),
        ):
            workflow._controller_cost_attempt(
                task_id,
                task,
                "luna",
                workflow.NATIVE_SUBAGENT,
                1.0,
                prompt_bytes,
                {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_tokens,
                    "output_tokens": 5,
                },
                "none",
                "SUPPORTED",
                self.state_root,
            )
        self.store.append_event(
            task_id,
            {"event_type": "OWNER_GATE_REACHED", "new_state": "AWAITING_OWNER_DECISION"},
        )
        self.store.append_event(
            task_id,
            {
                "event_type": "CONSTRUCTION_OWNER_GATE_REACHED",
                "new_state": "AWAITING_OWNER_DECISION",
            },
        )
        self.store.append_event(
            task_id,
            {"event_type": "OWNER_DECISION", "new_state": "CLOSED"},
        )

        metrics = workflow.aggregate_metrics(self.state_root)

        self.assertEqual(2, metrics["model_call_count"])
        self.assertEqual(1, metrics["closed_task_count"])
        self.assertEqual(2.0, metrics["model_calls_per_closed_task"])
        self.assertEqual(2, metrics["owner_gate_count"])
        self.assertEqual(200.0, metrics["average_prompt_bytes"])
        self.assertEqual(0.175, metrics["cache_hit_ratio"])
        report = workflow.render_report(metrics)
        self.assertIn("Model calls per closed task: 2.000", report)
        self.assertIn("Owner gates reached: 2", report)

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
        report = output_path.read_text(encoding="utf-8")
        self.assertIn("# AI Workflow Experiment Report", report)
        self.assertIn(
            "This calibration report proves only that the Luna read-only path can run",
            report,
        )

    def test_report_command_pins_claim_threshold_from_optimization_policy(self):
        task_id = self._create_task("AWF-20260803-001")
        self._record(task_id, {"role": "luna", "duration_seconds": 1.0})
        output_path = Path(self.temporary_directory.name) / "pinned-report.md"

        with (
            mock.patch.object(
                workflow, "render_report", wraps=workflow.render_report
            ) as spy,
            redirect_stdout(StringIO()),
        ):
            exit_code = workflow.main(
                ["report", "--root", str(self.state_root), "--output", str(output_path)]
            )

        self.assertEqual(0, exit_code)
        pinned = workflow.resolve_optimization_policy(
            workflow._load_workflow_config()
        ).minimum_paired_cases
        self.assertEqual(
            pinned, spy.call_args.kwargs["claim_minimum_cases"]
        )

    def test_report_command_fails_closed_on_invalid_optimization_policy(self):
        task_id = self._create_task("AWF-20260803-001")
        self._record(task_id, {"role": "luna", "duration_seconds": 1.0})
        output_path = Path(self.temporary_directory.name) / "never-written.md"
        errors = StringIO()
        broken_config = json.loads(json.dumps(workflow._load_workflow_config()))
        broken_config["optimization"]["minimum_paired_cases"] = -1

        with (
            mock.patch.object(
                workflow,
                "_load_workflow_config",
                return_value=broken_config,
            ),
            redirect_stdout(StringIO()),
            redirect_stderr(errors),
        ):
            exit_code = workflow.main(
                ["report", "--root", str(self.state_root), "--output", str(output_path)]
            )

        self.assertEqual(2, exit_code)
        self.assertFalse(output_path.exists())
        self.assertIn("OPTIMIZATION_POLICY_INVALID", errors.getvalue())


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
        legacy_config = workflow._load_workflow_config()
        legacy_config["routing"] = {"mode": "legacy", "role_policy": "legacy"}
        self.legacy_policy = mock.patch.object(
            workflow,
            "_load_workflow_config",
            return_value=legacy_config,
        )
        self.legacy_policy.start()

    def tearDown(self):
        self.legacy_policy.stop()
        self.temporary_directory.cleanup()

    def _task(self, task_type="PLAN", risk_flags=None):
        task = TaskValidationTest().valid_task()
        task["task_type"] = task_type
        task["risk_flags"] = [] if risk_flags is None else risk_flags
        if task_type == "REMEDIATION":
            task["allowed_write_paths"] = ["scripts/"]
            task["source_worktree"] = str(ROOT)
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
        if task.get("allowed_write_paths"):
            registry = ownership.OwnershipRegistry(
                schema_version=ownership.OWNERSHIP_REGISTRY_SCHEMA_VERSION,
                task_id=str(task["task_id"]),
                envelope_hash=artifacts.artifact_sha256(task),
                path_owners={
                    str(path).rstrip("/") or path: "terra"
                    for path in task["allowed_write_paths"]
                },
                registered_at_utc="2026-08-28T00:00:00Z",
            )
            with self.store.lock(str(task["task_id"])):
                ownership.record_ownership_registry(self.store, str(task["task_id"]), registry)
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
                    "BLOCKED",
                )

        create_worktree.assert_called_once_with(task, owner_authorized=True, store=mock.ANY)

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

    def test_resume_command_continues_an_authorized_task_idempotently(self):
        task_id = self._create_task(self._task("REMEDIATION"))
        workflow.run_until_gate(
            task_id,
            runner=ScriptedRunner([]),
            allow_live_model=False,
            state_root=self.state_root,
        )
        workflow._apply_owner_decision(
            self.store, task_id, "approve_execution", "owner"
        )

        first = StringIO()
        with redirect_stdout(first):
            first_exit = workflow.main(
                ["resume", task_id, "--runner", "fake", "--root", str(self.state_root)]
            )
        events_after_first = (
            self.state_root / task_id / "events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        second = StringIO()
        with redirect_stdout(second):
            second_exit = workflow.main(
                ["resume", task_id, "--runner", "fake", "--root", str(self.state_root)]
            )

        self.assertEqual(0, first_exit)
        self.assertEqual(0, second_exit)
        self.assertEqual("AWAITING_OWNER_DECISION\n", first.getvalue())
        self.assertEqual("AWAITING_OWNER_DECISION\n", second.getvalue())
        events_after_second = (
            self.state_root / task_id / "events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            events_after_first,
            events_after_second,
        )

    def test_abort_command_preserves_task_evidence(self):
        task_id = self._create_task(self._task("REMEDIATION"))
        workflow.run_until_gate(
            task_id,
            runner=ScriptedRunner([]),
            allow_live_model=False,
            state_root=self.state_root,
        )

        output = StringIO()
        with redirect_stdout(output):
            exit_code = workflow.main(
                ["abort", task_id, "--by", "owner", "--root", str(self.state_root)]
            )

        self.assertEqual(0, exit_code)
        self.assertEqual("ABORTED\n", output.getvalue())
        self.assertTrue((self.state_root / task_id / "task.json").is_file())
        self.assertEqual("ABORTED", workflow._current_state(self.store, task_id))

    def test_abort_is_available_from_every_nonterminal_runtime_state(self):
        for state, targets in workflow.TRANSITIONS.items():
            with self.subTest(state=state):
                self.assertIn("ABORTED", targets)

    def test_decide_resume_is_one_explicit_fake_command(self):
        task_id = self._create_task(self._task("REMEDIATION"))
        workflow.run_until_gate(
            task_id,
            runner=ScriptedRunner([]),
            allow_live_model=False,
            state_root=self.state_root,
        )

        output = StringIO()
        with redirect_stdout(output):
            exit_code = workflow.main(
                [
                    "decide",
                    task_id,
                    "approve_execution",
                    "--resume",
                    "--runner",
                    "fake",
                    "--root",
                    str(self.state_root),
                ]
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(
            "DECISION_RECORDED\nAWAITING_OWNER_DECISION\n", output.getvalue()
        )

    def test_decide_resume_rejects_live_before_recording_the_decision(self):
        task_id = self._create_task(self._task("REMEDIATION"))
        workflow.run_until_gate(
            task_id,
            runner=ScriptedRunner([]),
            allow_live_model=False,
            state_root=self.state_root,
        )

        errors = StringIO()
        with redirect_stderr(errors):
            exit_code = workflow.main(
                [
                    "decide",
                    task_id,
                    "approve_execution",
                    "--resume",
                    "--runner",
                    "live",
                    "--root",
                    str(self.state_root),
                ]
            )

        self.assertEqual(2, exit_code)
        self.assertIn("LIVE_MODEL_NOT_AUTHORIZED", errors.getvalue())
        self.assertFalse(
            (self.state_root / task_id / "human-decisions.jsonl").exists()
        )

    def test_decide_resume_disabled_does_not_write_owner_decision(self):
        task_id = self._create_task(self._task("REMEDIATION"))
        workflow.run_until_gate(
            task_id,
            runner=ScriptedRunner([]),
            allow_live_model=False,
            state_root=self.state_root,
        )
        state_before = workflow._current_state(self.store, task_id)
        events_before = (
            self.state_root / task_id / "events.jsonl"
        ).read_text(encoding="utf-8")
        disabled = dict(workflow._load_workflow_config())
        automation = dict(disabled.get("automation") or {})
        automation["allow_decide_resume"] = False
        disabled["automation"] = automation

        errors = StringIO()
        with (
            mock.patch.object(workflow, "_load_workflow_config", return_value=disabled),
            redirect_stderr(errors),
        ):
            exit_code = workflow.main(
                [
                    "decide",
                    task_id,
                    "approve_execution",
                    "--resume",
                    "--runner",
                    "fake",
                    "--root",
                    str(self.state_root),
                ]
            )

        self.assertEqual(2, exit_code)
        self.assertIn("DECIDE_RESUME_DISABLED", errors.getvalue())
        self.assertFalse(
            (self.state_root / task_id / "human-decisions.jsonl").exists()
        )
        self.assertEqual(state_before, workflow._current_state(self.store, task_id))
        self.assertEqual(
            events_before,
            (self.state_root / task_id / "events.jsonl").read_text(encoding="utf-8"),
        )

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

    def test_retry_budget_consumes_explicit_configured_limits(self):
        limits = workflow._retry_limits_from_config(
            {
                "policy": {
                    "max_technical_retries": 2,
                    "max_implementation_reworks": 1,
                    "max_cross_model_escalations": 1,
                }
            }
        )
        budget = workflow.RetryBudget(limits=limits)

        budget.consume_technical()
        budget.consume_technical()
        with self.assertRaisesRegex(workflow.WorkflowError, "RETRY_BUDGET_EXHAUSTED"):
            budget.consume_technical()

        budget.consume_rework()
        with self.assertRaisesRegex(workflow.WorkflowError, "RETRY_BUDGET_EXHAUSTED"):
            budget.consume_rework()


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


class DispatchSchemaDerivationTest(unittest.TestCase):
    CANONICAL = ROOT / "config" / "ai_workflow_result.schema.json"
    IDENTITY_TYPES = {
        "dispatch_id": "string",
        "task_id": "string",
        "step_id": "string",
        "attempt": "integer",
    }

    def test_derived_variant_is_provider_strict_and_otherwise_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            derived_path = workflow.materialize_dispatch_result_schema(
                self.CANONICAL, Path(temp), "attempt-1"
            )
            self.assertEqual(Path(temp) / "attempt-1.schema.json", derived_path)
            derived = json.loads(derived_path.read_text(encoding="utf-8"))
        canonical = json.loads(self.CANONICAL.read_text(encoding="utf-8"))
        self.assertEqual(set(derived["required"]), set(derived["properties"]))
        for field, base_type in self.IDENTITY_TYPES.items():
            self.assertEqual([base_type, "null"], derived["properties"][field]["type"])

        def assert_nested_objects_are_strict(node):
            if isinstance(node, dict):
                if isinstance(node.get("properties"), dict):
                    self.assertEqual(
                        set(node.get("required", [])), set(node["properties"])
                    )
                for value in node.values():
                    assert_nested_objects_are_strict(value)
            elif isinstance(node, list):
                for item in node:
                    assert_nested_objects_are_strict(item)

        assert_nested_objects_are_strict(derived)
        reverted = json.loads(json.dumps(derived))
        for field, base_type in self.IDENTITY_TYPES.items():
            reverted["properties"][field]["type"] = base_type
        reverted["required"] = canonical["required"]
        self.assertEqual(canonical, reverted)

    def test_derivation_rejects_unknown_canonical_property(self):
        drifted = json.loads(self.CANONICAL.read_text(encoding="utf-8"))
        drifted["properties"]["surprise_field"] = {"type": "string"}
        with tempfile.TemporaryDirectory() as temp:
            drifted_path = Path(temp) / "drifted.schema.json"
            drifted_path.write_text(json.dumps(drifted), encoding="utf-8")
            with self.assertRaisesRegex(
                workflow.WorkflowError, "RESULT_SCHEMA_DERIVATION_INVALID"
            ):
                workflow.materialize_dispatch_result_schema(
                    drifted_path, Path(temp), "attempt-1"
                )

    def test_derivation_rejects_identity_field_shape_drift(self):
        drifted = json.loads(self.CANONICAL.read_text(encoding="utf-8"))
        drifted["properties"]["attempt"]["type"] = "number"
        with tempfile.TemporaryDirectory() as temp:
            drifted_path = Path(temp) / "drifted.schema.json"
            drifted_path.write_text(json.dumps(drifted), encoding="utf-8")
            with self.assertRaisesRegex(
                workflow.WorkflowError, "RESULT_SCHEMA_DERIVATION_INVALID"
            ):
                workflow.materialize_dispatch_result_schema(
                    drifted_path, Path(temp), "attempt-1"
                )

    def test_derivation_is_write_once_per_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            workflow.materialize_dispatch_result_schema(
                self.CANONICAL, Path(temp), "attempt-1"
            )
            with self.assertRaisesRegex(
                workflow.WorkflowError, "RESULT_SCHEMA_DERIVATION_INVALID"
            ):
                workflow.materialize_dispatch_result_schema(
                    self.CANONICAL, Path(temp), "attempt-1"
                )
            workflow.materialize_dispatch_result_schema(
                self.CANONICAL, Path(temp), "attempt-2"
            )


def _with_run_popen_bridge(fn):
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        run = mock.Mock(name="codex_run")
        self._codex_run = run
        with mock.patch.object(
            workflow.subprocess,
            "Popen",
            _compat_popen(lambda command, *a, **k: run(command, *a, **k)),
        ):
            return fn(self, *args, **kwargs)

    return wrapped


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

    def _declare_codex_task(self, task, state_root, *, role="luna"):
        store = workflow.WorkflowStore(state_root)
        task_dir = Path(state_root) / str(task["task_id"])
        if not task_dir.exists():
            store.create_task(task)
        allowed = tuple(
            dict.fromkeys((role, "luna", "sol_planner", "sol_reviewer", "terra", "terra_xhigh"))
        )
        _install_declaration(store, task, allowed_roles=allowed, active_roles=(role,))
        return store

    def _codex_paths(self, root, task, *, role="luna", **overrides):
        state_root = overrides.pop("state_root", root / "state")
        self._declare_codex_task(task, state_root, role=role)
        values = {
            "repo": ROOT,
            "output_path": root / f"{role}-result.json",
            "schema_path": ROOT / "config/ai_workflow_result.schema.json",
            "logs_dir": root / "logs",
            "state_root": state_root,
        }
        values.update(overrides)
        return workflow.RunPaths(**values)

    def _popen(self, handler):
        return mock.patch.object(workflow.subprocess, "Popen", _compat_popen(handler))

    def _bridge_run_to_popen(self, run):
        return mock.patch.object(
            workflow.subprocess,
            "Popen",
            _compat_popen(lambda command, *args, **kwargs: run(command, *args, **kwargs)),
        )

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

    def test_result_all_null_identity_quartet_normalizes_to_absent(self):
        result = self.valid_result()
        result.update(
            {"dispatch_id": None, "task_id": None, "step_id": None, "attempt": None}
        )
        caller_view = dict(result)
        workflow.validate_role_result("luna", result, set())
        self.assertEqual(result, caller_view)

    def test_result_exact_bound_task_id_echo_normalizes_to_absent(self):
        result = self.valid_result()
        result.update(
            {
                "dispatch_id": None,
                "task_id": "AWF-20260826-001",
                "step_id": None,
                "attempt": None,
            }
        )
        caller_view = dict(result)
        workflow.validate_role_result(
            "luna",
            result,
            set(),
            expected_task_id="AWF-20260826-001",
        )
        self.assertEqual(result, caller_view)

    def test_result_task_id_echo_requires_exact_controller_binding(self):
        echo = {
            "dispatch_id": None,
            "task_id": "AWF-20260826-001",
            "step_id": None,
            "attempt": None,
        }
        for expected_task_id in (
            None,
            "",
            "AWF-20260826-999",
        ):
            with self.subTest(expected_task_id=expected_task_id):
                result = self.valid_result()
                result.update(echo)
                with self.assertRaisesRegex(
                    workflow.WorkflowError, "INVALID_ROLE_RESULT"
                ):
                    workflow.validate_role_result(
                        "luna",
                        result,
                        set(),
                        expected_task_id=expected_task_id,
                    )

    def test_result_task_id_echo_rejects_non_null_reserved_identity(self):
        for field, value in (
            ("dispatch_id", "a" * 64),
            ("step_id", "step-1"),
            ("attempt", 1),
        ):
            with self.subTest(field=field):
                result = self.valid_result()
                result.update(
                    {
                        "dispatch_id": None,
                        "task_id": "AWF-20260826-001",
                        "step_id": None,
                        "attempt": None,
                        field: value,
                    }
                )
                with self.assertRaisesRegex(
                    workflow.WorkflowError, "INVALID_ROLE_RESULT"
                ):
                    workflow.validate_role_result(
                        "luna",
                        result,
                        set(),
                        expected_task_id="AWF-20260826-001",
                    )

    def test_result_partially_null_identity_is_rejected(self):
        full_identity = {
            "dispatch_id": "a" * 64,
            "task_id": "AWF-20260826-001",
            "step_id": "step-1",
            "attempt": 1,
        }
        partial_shapes = (
            {**full_identity, "attempt": None},
            {**full_identity, "dispatch_id": None, "task_id": None},
            {"dispatch_id": None, "task_id": None},
            {"attempt": None},
        )
        for shape in partial_shapes:
            with self.subTest(shape=sorted(shape)):
                result = self.valid_result()
                result.update(shape)
                with self.assertRaisesRegex(
                    workflow.WorkflowError, "INVALID_ROLE_RESULT"
                ):
                    workflow.validate_role_result("luna", result, set())

    def test_result_forged_identity_values_are_still_rejected(self):
        base_identity = {
            "dispatch_id": "a" * 64,
            "task_id": "AWF-20260826-001",
            "step_id": "step-1",
            "attempt": 1,
        }
        forged_shapes = (
            {"dispatch_id": "not-a-digest"},
            {"attempt": 0},
            {"attempt": True},
            {"task_id": ""},
        )
        for forged in forged_shapes:
            with self.subTest(forged=sorted(forged)):
                result = self.valid_result()
                result.update({**base_identity, **forged})
                with self.assertRaisesRegex(
                    workflow.WorkflowError, "INVALID_ROLE_RESULT"
                ):
                    workflow.validate_role_result("luna", result, set())

    def test_result_fully_bound_identity_is_still_accepted(self):
        result = self.valid_result()
        result.update(
            {
                "dispatch_id": "a" * 64,
                "task_id": "AWF-20260826-001",
                "step_id": "step-1",
                "attempt": 1,
            }
        )
        workflow.validate_role_result("luna", result, set())

    def test_live_bound_validation_rejects_model_supplied_scheduler_identity(self):
        result = self.valid_result()
        result.update(
            {
                "dispatch_id": "a" * 64,
                "task_id": "AWF-20260826-001",
                "step_id": "step-1",
                "attempt": 1,
            }
        )
        with self.assertRaisesRegex(
            workflow.WorkflowError, "INVALID_ROLE_RESULT"
        ):
            workflow.validate_role_result(
                "luna",
                result,
                set(),
                expected_task_id="AWF-20260826-001",
            )

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
            "Set dispatch_id, task_id, step_id, and attempt to null; controller identity is reserved.",
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
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    def test_run_codex_enforces_luna_l1_evidence_package(self, _working_tree_paths, _capture_repo):
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
        reviewer = self.valid_result("sol_reviewer", "ACCEPTANCE_RECOMMENDED")
        cases.append(("non-luna role", reviewer, None))

        for name, result, error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                paths = self._codex_paths(root, task, role=result["role"])
                handler = lambda command, *args, **kwargs: write_codex_result(command, result)
                with self._popen(handler):
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
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    def test_run_codex_passes_sanitized_stdin_and_accepts_valid_output(self, _working_tree_paths, _capture_repo):
        result = self.valid_result()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self._codex_paths(root, self.valid_task())
            _RecordingPopen.reset()
            with self._popen(lambda command, *args, **kwargs: write_codex_result(command, result)), mock.patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "secret", "TUSHARE_TOKEN": "secret", "PATH": "/usr/bin"},
                clear=True,
            ):
                actual = workflow.run_codex("luna", self.valid_task(), "task contract", paths)

            self.assertEqual(actual, result)
            self.assertEqual(
                next((root / "logs").glob("luna-*.jsonl")).read_text(),
                '{"event":"done"}\n',
            )

        self.assertEqual(1, len(_RecordingPopen.calls))
        command, kwargs = _RecordingPopen.calls[0]
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["text"])
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("TUSHARE_TOKEN", kwargs["env"])
        self.assertEqual(_RecordingPopen.instances[0].input, "task contract")

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    @mock.patch("scripts.ai_workflow._record_controller_metrics")
    def test_codex_non_runtime_usage_is_unavailable_without_verified_runtime_identity(
        self, record_metrics, _working_tree_paths, _capture_repo
    ):
        task = self.valid_task()
        result = self.valid_result()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_root = root / "state"
            self._declare_codex_task(task, state_root)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
                state_root=state_root,
            )

            def write_usage_result(command, *args, **kwargs):
                write_codex_result(command, result)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        '{"type":"turn.completed","usage":'
                        '{"input_tokens":11,"cached_input_tokens":2,"output_tokens":3}}\n'
                    ),
                    stderr="",
                )

            self._codex_run.side_effect = write_usage_result
            workflow.run_codex("luna", task, "task contract", paths)

        record_metrics.assert_called_once()
        metric_run = record_metrics.call_args.args[1]
        evidence = metric_run["cost_evidence"]
        self.assertEqual("CODEX_EXEC_ROLE_CONTRACT", evidence["execution_surface"])
        self.assertIsNone(evidence["input_tokens"])
        self.assertIsNone(evidence["cached_input_tokens"])
        self.assertIsNone(evidence["output_tokens"])
        self.assertEqual(len("task contract".encode("utf-8")), evidence["prompt_bytes"])
        self.assertEqual("unavailable", evidence["evidence_class"])
        self.assertIsNone(evidence["paired_case_id"])

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    def test_codex_usage_is_unavailable_when_runtime_identity_validation_fails(
        self, _working_tree_paths, _capture_repo
    ):
        task = self.valid_task()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            self._declare_codex_task(task, state_root)
            sessions = root / "sessions"
            sessions.mkdir()
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
                state_root=state_root,
                runtime_evidence_required=True,
                runtime_sessions_dir=sessions,
            )

            def write_usage_result(command, *args, **kwargs):
                write_codex_result(command, workflow.FakeRunner().run("luna", task))
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        '{"type":"turn.completed","usage":'
                        '{"input_tokens":11,"cached_input_tokens":2,"output_tokens":3}}\n'
                    ),
                    stderr="",
                )

            self._codex_run.side_effect = write_usage_result
            with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_EVIDENCE"):
                workflow.run_codex("luna", task, "task contract", paths)
            document = json.loads(
                (state_root / task["task_id"] / "metrics.json").read_text(
                    encoding="utf-8"
                )
            )

        evidence = document["runs"][0]["cost_evidence"]
        self.assertEqual("FAILED", evidence["quality_outcome"])
        self.assertEqual("unavailable", evidence["evidence_class"])
        self.assertIsNone(evidence["input_tokens"])

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    def test_non_runtime_invalid_result_has_exactly_one_failed_attempt(
        self, _working_tree_paths, _capture_repo
    ):
        task = self.valid_task()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            self._declare_codex_task(task, state_root)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
                state_root=state_root,
            )

            def write_invalid_result(command, *args, **kwargs):
                write_codex_result(command, {"role": "luna", "status": "SUPPORTED"})
                return subprocess.CompletedProcess(command, 0, stdout="{}\n", stderr="")

            self._codex_run.side_effect = write_invalid_result
            with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_ROLE_RESULT"):
                workflow.run_codex("luna", task, "task contract", paths)
            document = json.loads(
                (state_root / task["task_id"] / "metrics.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(1, len(document["runs"]))
        evidence = document["runs"][0]["cost_evidence"]
        self.assertEqual("FAILED", evidence["quality_outcome"])
        self.assertEqual("unavailable", evidence["evidence_class"])

    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    def test_non_runtime_repo_guard_failure_has_exactly_one_failed_attempt(
        self, _working_tree_paths
    ):
        task = self.valid_task()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            self._declare_codex_task(task, state_root)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
                state_root=state_root,
            )

            def write_valid_result(command, *args, **kwargs):
                write_codex_result(command, self.valid_result())
                return subprocess.CompletedProcess(command, 0, stdout="{}\n", stderr="")

            self._codex_run.side_effect = write_valid_result
            with mock.patch(
                "scripts.ai_workflow.capture_repo",
                side_effect=[
                    workflow.RepoSnapshot("before", ()),
                    workflow.RepoSnapshot("after", ()),
                ],
            ):
                with self.assertRaisesRegex(workflow.WorkflowError, "HEAD_DRIFT"):
                    workflow.run_codex("luna", task, "task contract", paths)
            document = json.loads(
                (state_root / task["task_id"] / "metrics.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(1, len(document["runs"]))
        evidence = document["runs"][0]["cost_evidence"]
        self.assertEqual("FAILED", evidence["quality_outcome"])
        self.assertEqual("unavailable", evidence["evidence_class"])

    @staticmethod
    def _bound_attempt_context(task, attempt_id, *, role="luna", retry_kind="none"):
        return workflow.AttemptAccountingContext(
            task_id=task["task_id"],
            role=role,
            retry_kind=retry_kind,
            attempt_id=attempt_id,
        )

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    def test_run_codex_keeps_task_id_echo_in_raw_attempt_but_returns_normalized_result(
        self, _working_tree_paths, _capture_repo
    ):
        task = self.valid_task()
        result = self.valid_result()
        result.update(
            {
                "dispatch_id": None,
                "task_id": task["task_id"],
                "step_id": None,
                "attempt": None,
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            self._declare_codex_task(task, state_root)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
                state_root=state_root,
            )
            context = self._bound_attempt_context(task, "luna-task-id-echo")
            self._codex_run.side_effect = lambda command, *args, **kwargs: write_codex_result(
                command, result
            )

            returned = workflow.run_codex(
                "luna",
                task,
                "task contract",
                paths,
                attempt_context=context,
            )
            raw = json.loads(
                (
                    root
                    / "attempts"
                    / "luna-task-id-echo.json"
                ).read_text(encoding="utf-8")
            )

        self.assertNotIn("task_id", returned)
        self.assertEqual(task["task_id"], raw["task_id"])
        self.assertIsNone(raw["dispatch_id"])
        self.assertIsNone(raw["step_id"])
        self.assertIsNone(raw["attempt"])

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    def test_reused_failed_attempt_context_is_rejected_before_a_second_launch(
        self, _working_tree_paths, _capture_repo
    ):
        task = self.valid_task()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            self._declare_codex_task(task, state_root)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
                state_root=state_root,
            )
            context = self._bound_attempt_context(task, "luna-reused-failure")
            self._codex_run.return_value = subprocess.CompletedProcess(
                [], 23, stdout="first failed attempt\n", stderr=""
            )

            with self.assertRaisesRegex(workflow.WorkflowError, "CODEX_EXIT_NONZERO"):
                workflow.run_codex("luna", task, "task contract", paths, attempt_context=context)
            log_path = paths.logs_dir / "luna-reused-failure.jsonl"
            first_log = log_path.read_text(encoding="utf-8")

            with self.assertRaisesRegex(
                workflow.WorkflowError, "ATTEMPT_CONTEXT_REUSED|DISPATCH_PERMIT_ALREADY_STARTED"
            ):
                workflow.run_codex("luna", task, "task contract", paths, attempt_context=context)

            document = json.loads(
                (state_root / task["task_id"] / "metrics.json").read_text(encoding="utf-8")
            )
            second_log = log_path.read_text(encoding="utf-8")

        self.assertEqual(1, self._codex_run.call_count)
        self.assertGreaterEqual(len(document["runs"]), 1)
        self.assertEqual(first_log, second_log)

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    def test_codex_nonzero_exit_surfaces_bounded_redacted_stderr_tail(
        self, _working_tree_paths, _capture_repo
    ):
        task = self.valid_task()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            self._declare_codex_task(task, state_root)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
                state_root=state_root,
            )
            context = self._bound_attempt_context(task, "luna-stderr-tail")
            child_stderr = (
                "x" * 3000
                + "\nERROR: unsupported schema: invalid_json_schema details\n"
                + "OPENAI_API_KEY=sk-super-secret-value\n"
            )
            self._codex_run.return_value = subprocess.CompletedProcess(
                [], 1, stdout="", stderr=child_stderr
            )

            with self.assertRaises(workflow.WorkflowError) as caught:
                workflow.run_codex(
                    "luna", task, "task contract", paths, attempt_context=context
                )

        self.assertEqual("CODEX_EXIT_NONZERO", caught.exception.code)
        message = caught.exception.message
        self.assertIn("luna exited with code 1", message)
        self.assertIn("stderr tail:", message)
        self.assertIn("invalid_json_schema", message)
        self.assertIn("OPENAI_API_KEY=[REDACTED]", message)
        self.assertNotIn("sk-super-secret-value", message)
        self.assertLessEqual(len(message), 2100)

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    def test_codex_stderr_tail_redacts_secret_straddling_the_truncation_cut(
        self, _working_tree_paths, _capture_repo
    ):
        task = self.valid_task()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            self._declare_codex_task(task, state_root)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
                state_root=state_root,
            )
            context = self._bound_attempt_context(task, "luna-stderr-cut")
            secret = "sk-" + "a1b2c3d4" * 8
            # Position the secret so the 2000-character tail cut lands in the
            # middle of its raw value; the fragment must still be redacted.
            child_stderr = f"OPENAI_API_KEY={secret}\n" + "y" * 1990
            self._codex_run.return_value = subprocess.CompletedProcess(
                [], 1, stdout="", stderr=child_stderr
            )

            with self.assertRaises(workflow.WorkflowError) as caught:
                workflow.run_codex(
                    "luna", task, "task contract", paths, attempt_context=context
                )

        message = caught.exception.message
        self.assertNotIn(secret[-9:], message)
        self.assertNotIn(secret, message)

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    def test_reused_successful_attempt_context_is_rejected_before_a_second_launch(
        self, _working_tree_paths, _capture_repo
    ):
        task = self.valid_task()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            self._declare_codex_task(task, state_root)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
                state_root=state_root,
            )
            context = self._bound_attempt_context(task, "luna-reused-success")
            self._codex_run.side_effect = lambda command, *args, **kwargs: write_codex_result(
                command, self.valid_result()
            )

            workflow.run_codex("luna", task, "task contract", paths, attempt_context=context)
            with self.assertRaisesRegex(
                workflow.WorkflowError, "ATTEMPT_CONTEXT_REUSED|DISPATCH_PERMIT_ALREADY_STARTED"
            ):
                workflow.run_codex("luna", task, "task contract", paths, attempt_context=context)

            document = json.loads(
                (state_root / task["task_id"] / "metrics.json").read_text(encoding="utf-8")
            )

        self.assertEqual(1, self._codex_run.call_count)
        self.assertGreaterEqual(len(document["runs"]), 1)

    @_with_run_popen_bridge
    def test_attempt_context_rejects_task_and_role_mismatches_before_launch(self):
        task = self.valid_task()
        other_task = dict(task)
        other_task["task_id"] = "AWF-20260803-099"
        context = self._bound_attempt_context(task, "luna-bound-context")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
            )

            with self.assertRaisesRegex(workflow.WorkflowError, "ATTEMPT_CONTEXT_MISMATCH"):
                workflow.run_codex("sol_reviewer", task, "task contract", paths, attempt_context=context)
            with self.assertRaisesRegex(workflow.WorkflowError, "ATTEMPT_CONTEXT_MISMATCH"):
                workflow.run_codex("luna", other_task, "task contract", paths, attempt_context=context)

        self._codex_run.assert_not_called()

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    def test_distinct_attempt_contexts_can_launch_independent_failures(
        self, _working_tree_paths, _capture_repo
    ):
        task = self.valid_task()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "state"
            self._declare_codex_task(task, state_root)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
                state_root=state_root,
            )
            self._codex_run.return_value = subprocess.CompletedProcess([], 23, stdout="failed\n", stderr="")
            for attempt_id in ("luna-first-failure", "luna-second-failure"):
                with self.assertRaisesRegex(workflow.WorkflowError, "CODEX_EXIT_NONZERO"):
                    workflow.run_codex(
                        "luna",
                        task,
                        "task contract",
                        paths,
                        attempt_context=self._bound_attempt_context(task, attempt_id),
                    )
            document = json.loads(
                (state_root / task["task_id"] / "metrics.json").read_text(encoding="utf-8")
            )
            log_names = {path.name for path in paths.logs_dir.glob("*.jsonl")}

        self.assertEqual(2, self._codex_run.call_count)
        self.assertEqual(2, len(document["runs"]))
        self.assertEqual(
            {"luna-first-failure.jsonl", "luna-second-failure.jsonl"},
            log_names,
        )

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    def test_run_codex_rejects_timeout_exit_and_invalid_json(self, _working_tree_paths, _capture_repo):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = self.valid_task()
            paths = self._codex_paths(root, task)
            self._codex_run.side_effect = __import__("subprocess").TimeoutExpired("codex", 30)
            with self.assertRaisesRegex(workflow.WorkflowError, "CODEX_TIMEOUT"):
                workflow.run_codex("luna", task, "task contract", paths)

            self._codex_run.side_effect = None
            self._codex_run.return_value = mock.Mock(returncode=23, stdout="", stderr="failed")
            with self.assertRaisesRegex(workflow.WorkflowError, "CODEX_EXIT_NONZERO"):
                workflow.run_codex("luna", task, "task contract", paths)

            def write_invalid_output(command, *args, **kwargs):
                output_path = Path(command[command.index("-o") + 1])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text("not json", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            self._codex_run.side_effect = write_invalid_output
            with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_ROLE_RESULT"):
                workflow.run_codex("luna", task, "task contract", paths)

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set())
    @_with_run_popen_bridge
    def test_run_codex_redacts_secret_assignments_and_long_tokens_from_events(self, _working_tree_paths, _capture_repo):
        result = self.valid_result()
        long_token = "Ab3d" * 32
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = self.valid_task()
            paths = self._codex_paths(root, task)
            def write_redacted_result(command, *args, **kwargs):
                write_codex_result(command, result)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=f"TUSHARE_TOKEN=abc123 OPENAI_API_KEY=sk-test-value {long_token}",
                    stderr="",
                )

            self._codex_run.side_effect = write_redacted_result
            workflow.run_codex("luna", task, "task contract", paths)
            events = next((root / "logs").glob("luna-*.jsonl")).read_text(encoding="utf-8")

        self.assertIn("[REDACTED]", events)
        self.assertIn("TUSHARE_TOKEN=[REDACTED]", events)
        self.assertIn("OPENAI_API_KEY=[REDACTED]", events)
        self.assertNotIn("abc123", events)
        self.assertNotIn("sk-test-value", events)
        self.assertNotIn(long_token, events)


class TeamCallCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.repo = Path(self.temporary_directory.name) / "repository"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "team-call-cli@example.test")
        self._git("config", "user.name", "Team Call CLI Test")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial fixture")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _git(self, *argv):
        return subprocess.run(
            ("git", *argv),
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_team_call_fake_cli_emits_a_canonical_l0_receipt(self):
        output = StringIO()

        with redirect_stdout(output):
            exit_code = workflow.main(
                [
                    "team-call",
                    "team call 检查当前工作区状态",
                    "--root",
                    str(self.state_root),
                    "--repository-root",
                    str(self.repo),
                    "--runner",
                    "fake",
                ]
            )

        self.assertEqual(0, exit_code)
        payload = json.loads(output.getvalue())
        self.assertEqual("DIRECT_L0", payload["disposition"])
        self.assertIsNone(payload["task_id"])
        self.assertRegex(payload["result_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(workflow._canonical_json(payload) + "\n", output.getvalue())

    def test_team_call_cli_replays_a_failed_call_as_blocked_with_exit_two(self):
        argv = (
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        failed = subprocess.CompletedProcess(argv, 1, stdout="", stderr="failed")
        first_output = StringIO()
        first_errors = StringIO()
        replay_output = StringIO()

        with mock.patch.object(workflow.TeamCallFakeController, "run_l0", return_value=failed):
            with redirect_stdout(first_output), redirect_stderr(first_errors):
                first_exit = workflow.main(
                    [
                        "team-call",
                        "team call 检查当前工作区状态",
                        "--root",
                        str(self.state_root),
                        "--repository-root",
                        str(self.repo),
                        "--runner",
                        "fake",
                    ]
                )
            with redirect_stdout(replay_output):
                replay_exit = workflow.main(
                    [
                        "team-call",
                        "team call 检查当前工作区状态",
                        "--root",
                        str(self.state_root),
                        "--repository-root",
                        str(self.repo),
                        "--runner",
                        "fake",
                    ]
                )

        self.assertEqual(2, first_exit)
        self.assertIn("TEAM_CALL_L0_FAILED", first_errors.getvalue())
        self.assertEqual("", first_output.getvalue())
        self.assertEqual(2, replay_exit)
        self.assertEqual("BLOCKED", json.loads(replay_output.getvalue())["disposition"])

    def test_team_call_default_state_root_stays_outside_a_fresh_repository(self):
        state_home = Path(self.temporary_directory.name) / "xdg-state"
        output = StringIO()
        original_cwd = Path.cwd()
        try:
            os.chdir(self.repo)
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state_home)}):
                with redirect_stdout(output):
                    exit_code = workflow.main(
                        [
                            "team-call",
                            "team call 核对文件 README.md",
                            "--repository-root",
                            str(self.repo),
                            "--runner",
                            "fake",
                        ]
                    )
        finally:
            os.chdir(original_cwd)

        self.assertEqual(0, exit_code)
        self.assertEqual("DIRECT_L1", json.loads(output.getvalue())["disposition"])
        self.assertEqual("", self._git("status", "--porcelain=v1", "--untracked-files=all").stdout)
        state_roots = list((state_home / "ai-workflow" / "team-call").iterdir())
        self.assertEqual(1, len(state_roots))
        self.assertTrue((state_roots[0] / "team-calls.jsonl").is_file())

    def test_team_call_cli_reports_missing_repository_with_exit_two(self):
        output = StringIO()
        errors = StringIO()

        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = workflow.main(
                [
                    "team-call",
                    "team call 检查当前工作区状态",
                    "--root",
                    str(self.state_root),
                    "--repository-root",
                    str(self.repo / "missing"),
                    "--runner",
                    "fake",
                ]
            )

        self.assertEqual(2, exit_code)
        self.assertEqual(
            "REPOSITORY_NOT_FOUND: repository_root does not exist\n", errors.getvalue()
        )
        self.assertEqual("", output.getvalue())

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_team_call_live_runner_requires_explicit_model_authorization(self, run_codex):
        output = StringIO()
        errors = StringIO()

        with redirect_stdout(output), redirect_stderr(errors):
            exit_code = workflow.main(
                [
                    "team-call",
                    "team call 核对文件 README.md",
                    "--root",
                    str(self.state_root),
                    "--repository-root",
                    str(self.repo),
                    "--runner",
                    "live",
                ]
            )

        self.assertEqual(2, exit_code)
        self.assertEqual(
            "LIVE_MODEL_NOT_AUTHORIZED: --allow-live-model is required for the live runner\n",
            errors.getvalue(),
        )
        self.assertEqual("", output.getvalue())
        run_codex.assert_not_called()

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_team_call_live_l1_runs_when_live_is_explicitly_authorized(self, run_codex):
        run_codex.return_value = {
            "schema_version": "ai-result-1",
            "role": "luna",
            "status": "SUPPORTED",
            "summary": "The named file was read without modification.",
            "claims": [
                {
                    "id": "claim-1",
                    "kind": "FACT",
                    "text": "The fixture exists.",
                    "evidence_ids": ["evidence-1"],
                }
            ],
            "evidence": [
                {
                    "id": "evidence-1",
                    "type": "FILE",
                    "locator": "README.md",
                    "observation": "The fixture was read.",
                }
            ],
            "counter_checks": [
                {
                    "target_claim_id": "claim-1",
                    "method": "Read it again.",
                    "result": "No contradiction found.",
                }
            ],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "EVIDENCE_READY",
        }
        sessions = Path(self.temporary_directory.name) / "sessions"
        sessions.mkdir()
        output = StringIO()

        with redirect_stdout(output):
            exit_code = workflow.main(
                [
                    "team-call",
                    "team call 核对文件 README.md",
                    "--root",
                    str(self.state_root),
                    "--repository-root",
                    str(self.repo),
                    "--runner",
                    "live",
                    "--allow-live-model",
                    "--runtime-sessions-dir",
                    str(sessions),
                ]
            )

        self.assertEqual(0, exit_code)
        self.assertEqual("DIRECT_L1", json.loads(output.getvalue())["disposition"])
        run_codex.assert_called_once()


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
        errors = StringIO()

        with redirect_stderr(errors):
            exit_code = workflow.main(
                ["run", str(self.task_path), "--runner", "live", "--root", str(self.state_root)]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("LIVE_MODEL_NOT_AUTHORIZED", errors.getvalue())
        run_codex.assert_not_called()

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_live_runner_rejects_non_luna_roles(self, run_codex):
        errors = StringIO()

        with redirect_stderr(errors):
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
        self.assertIn("LIVE_ROLE_NOT_ALLOWED", errors.getvalue())
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
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)

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
            "source_worktree": str(
                self.repo / ".codex-worktrees" / "awf-20260803-001"
            ),
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
                task = self._task()
                task["task_id"] = f"AWF-20260803-00{1 if role == 'luna' else 2}"
                task["source_worktree"] = str(self.repo)
                (self.repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
                self.store.create_task(task)
                _install_declaration(
                    self.store,
                    task,
                    allowed_roles=tuple(dict.fromkeys((role, "luna", "sol_reviewer"))),
                    active_roles=(role,),
                )
                output_path = Path(self.temporary_directory.name) / "outputs" / f"{role}-result.json"
                paths = workflow.RunPaths(
                    repo=self.repo,
                    output_path=output_path,
                    schema_path=ROOT / "config/ai_workflow_result.schema.json",
                    logs_dir=Path(self.temporary_directory.name) / "logs",
                    state_root=self.state_root,
                )

                def mutate(command, *args, **kwargs):
                    (self.repo / f"{role}-mutation.txt").write_text("changed\n", encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0, stdout="{\"event\": \"done\"}\n", stderr="")

                with mock.patch.object(workflow.subprocess, "Popen", _compat_popen(mutate)):
                    with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_ROLE_MODIFIED_REPO"):
                        workflow.run_codex(role, task, "bounded task", paths)
                (self.repo / f"{role}-mutation.txt").unlink()

    def test_create_worktree_rejects_an_unauthorized_owner_before_running_git(self):
        task = self._task()
        with mock.patch("scripts.ai_workflow.subprocess.run") as run:
            with self.assertRaisesRegex(workflow.WorkflowError, "OWNER_AUTHORIZATION_REQUIRED"):
                workflow.create_worktree(task, owner_authorized=False)

        run.assert_not_called()

    def test_create_worktree_requires_an_execution_approval_record(self):
        task = self._task()
        self.store.create_task(task)
        with self.assertRaisesRegex(workflow.WorkflowError, "APPROVED_FOR_EXECUTION_REQUIRED"):
            workflow.create_worktree(task, owner_authorized=True, store=self.store)

    def test_create_worktree_uses_the_approved_branch_and_directory(self):
        task = self._task()
        self.store.create_task(task)
        self.store.append_event(
            task["task_id"],
            {
                "event_type": "STATE_TRANSITION",
                "new_state": "AWAITING_OWNER_DECISION",
                "task_sha256": workflow._task_sha256(self.store, task["task_id"]),
            },
        )
        workflow._apply_owner_decision(self.store, task["task_id"], "approve_execution", "owner")
        (self.repo / "tracked.txt").write_text("new root head\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "unrelated root head")

        worktree = workflow.create_worktree(task, owner_authorized=True, store=self.store)

        self.assertEqual(
            worktree,
            (self.repo / ".codex-worktrees" / "awf-20260803-001").resolve(),
        )
        self.assertEqual(self._git("-C", str(worktree), "branch", "--show-current"), "aiwf/awf-20260803-001")
        self.assertEqual(self._git("-C", str(worktree), "rev-parse", "HEAD"), task["base_commit"])


class FinalSafetyRegressionTest(unittest.TestCase):
    """Regressions reproduced during the final runtime safety review."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        legacy_config = workflow._load_workflow_config()
        legacy_config["routing"] = {"mode": "legacy", "role_policy": "legacy"}
        self.legacy_policy = mock.patch.object(
            workflow,
            "_load_workflow_config",
            return_value=legacy_config,
        )
        self.legacy_policy.start()
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)
        self.repo = Path(self.temporary_directory.name) / "repository"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Workflow Test")
        (self.repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "initial")
        self.base_commit = self._git("rev-parse", "HEAD")

    def tearDown(self):
        self.legacy_policy.stop()
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

    def _task(self, task_type="PLAN"):
        task = TaskValidationTest().valid_task()
        task.update(
            {
                "task_type": task_type,
                "repository_root": str(self.repo),
                "base_commit": self.base_commit,
                "candidate_commit": None,
                "authoritative_files": ["tracked.txt"],
            }
        )
        if task_type == "REMEDIATION":
            task["allowed_write_paths"] = ["allowed/"]
        return task

    @staticmethod
    def _result(role, status):
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

    def _luna_result(self, status="SUPPORTED"):
        result = self._result("luna", status)
        if status != "BLOCKED":
            result["claims"] = [
                {
                    "id": "claim-1",
                    "kind": "FACT",
                    "text": "The authorized fixture was examined.",
                    "evidence_ids": ["evidence-1"],
                }
            ]
            result["evidence"] = [
                {
                    "id": "evidence-1",
                    "type": "FILE",
                    "locator": "tracked.txt",
                    "observation": "The fixture is present.",
                }
            ]
            result["counter_checks"] = [
                {
                    "target_claim_id": "claim-1",
                    "method": "Check the fixture once more.",
                    "result": "No contradiction found.",
                }
            ]
        return result

    def _paths(self, output_path):
        return workflow.RunPaths(
            repo=self.repo,
            output_path=output_path,
            schema_path=ROOT / "config" / "ai_workflow_result.schema.json",
            logs_dir=Path(self.temporary_directory.name) / "logs",
            state_root=self.state_root,
        )

    def _prepare(self, task, *, role="luna"):
        task_dir = self.state_root / task["task_id"]
        if not task_dir.exists():
            self.store.create_task(task)
        allowed = tuple(
            dict.fromkeys((role, "luna", "sol_planner", "sol_reviewer", "terra", "terra_xhigh"))
        )
        _install_declaration(self.store, task, allowed_roles=allowed, active_roles=(role,))
        if task.get("allowed_write_paths"):
            registry = ownership.OwnershipRegistry(
                schema_version=ownership.OWNERSHIP_REGISTRY_SCHEMA_VERSION,
                task_id=str(task["task_id"]),
                envelope_hash=artifacts.artifact_sha256(task),
                path_owners={
                    str(path).rstrip("/") or path: "terra"
                    for path in task["allowed_write_paths"]
                },
                registered_at_utc="2026-08-28T00:00:00Z",
            )
            with self.store.lock(str(task["task_id"])):
                ownership.record_ownership_registry(self.store, str(task["task_id"]), registry)
        return task

    def _patch_codex(self, handler):
        return mock.patch.object(
            workflow.subprocess, "Popen", _compat_popen(handler)
        )

    def test_acceptance_rejects_a_head_other_than_the_resolved_candidate_before_model_run(self):
        (self.repo / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "candidate")
        candidate = self._git("rev-parse", "HEAD")
        self._git("checkout", self.base_commit)
        task = self._task("ACCEPTANCE")
        task["candidate_commit"] = candidate
        task["base_commit"] = self.base_commit

        real_run = subprocess.run

        def run_git_only(command, *args, **kwargs):
            if command[0] == "git":
                return real_run(command, *args, **kwargs)
            self.fail("the candidate mismatch must stop before the Codex process starts")

        with self._patch_codex(run_git_only):
            with self.assertRaisesRegex(workflow.WorkflowError, "ACCEPTANCE_CANDIDATE_HEAD_MISMATCH"):
                workflow.run_codex("luna", task, "bounded", self._paths(Path(self.temporary_directory.name) / "result.json"))

    def test_read_only_role_rejects_a_preexisting_dirty_repository_before_model_run(self):
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        real_run = subprocess.run

        def run_git_only(command, *args, **kwargs):
            if command[0] == "git":
                return real_run(command, *args, **kwargs)
            self.fail("a dirty read-only input must stop before the Codex process starts")

        with self._patch_codex(run_git_only):
            with self.assertRaisesRegex(workflow.WorkflowError, "DIRTY_READ_ONLY_REPOSITORY"):
                workflow.run_codex(
                    "luna",
                    self._task(),
                    "bounded",
                    self._paths(Path(self.temporary_directory.name) / "result.json"),
                )

    def test_acceptance_rechecks_the_candidate_after_the_model_run(self):
        (self.repo / "tracked.txt").write_text("candidate\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "candidate")
        candidate = self._git("rev-parse", "HEAD")
        task = self._task("ACCEPTANCE")
        task["candidate_commit"] = candidate
        task["base_commit"] = self.base_commit
        output_path = Path(self.temporary_directory.name) / "result.json"
        real_run = subprocess.run

        def write_then_move_head(command, *args, **kwargs):
            if command[0] == "git":
                return real_run(command, *args, **kwargs)
            completed = write_codex_result(command, self._luna_result())
            self._git("checkout", self.base_commit)
            return completed

        with self._patch_codex(write_then_move_head):
            with self.assertRaisesRegex(workflow.WorkflowError, "HEAD_DRIFT"):
                workflow.run_codex("luna", self._prepare(task), "bounded", self._paths(output_path))

    def test_stale_canonical_output_is_not_accepted_when_this_attempt_creates_no_output(self):
        output_path = Path(self.temporary_directory.name) / "luna-result.json"
        output_path.write_text(json.dumps(self._luna_result()), encoding="utf-8")
        real_run = subprocess.run

        def run_without_output(command, *args, **kwargs):
            if command[0] == "git":
                return real_run(command, *args, **kwargs)
            return subprocess.CompletedProcess(command, 0, stdout='{"event":"done"}\n', stderr="")

        with self._patch_codex(run_without_output):
            with self.assertRaisesRegex(workflow.WorkflowError, "MISSING_FRESH_ROLE_OUTPUT"):
                workflow.run_codex("luna", self._prepare(self._task()), "bounded", self._paths(output_path))

    def test_each_role_attempt_uses_a_new_output_and_log_path(self):
        output_path = Path(self.temporary_directory.name) / "luna-result.json"
        paths = self._paths(output_path)
        real_run = subprocess.run

        def write_fresh_output(command, *args, **kwargs):
            if command[0] == "git":
                return real_run(command, *args, **kwargs)
            return write_codex_result(command, self._luna_result())

        with self._patch_codex(write_fresh_output):
            task = self._prepare(self._task())
            workflow.run_codex("luna", task, "bounded", paths)
            workflow.run_codex("luna", task, "bounded", paths)

        attempts_dir = output_path.parent / "attempts"
        result_outputs = [
            path
            for path in attempts_dir.glob("luna-*.json")
            if not path.name.endswith(".schema.json")
        ]
        self.assertEqual(len(result_outputs), 2)
        self.assertEqual(len(list(attempts_dir.glob("luna-*.schema.json"))), 2)
        self.assertEqual(len(list(paths.logs_dir.glob("luna-*.jsonl"))), 2)
        self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), self._luna_result())

    def test_role_runner_creates_the_attempt_output_directory_before_codex(self):
        output_path = Path(self.temporary_directory.name) / "luna-result.json"
        real_run = subprocess.run

        def require_parent_then_write(command, *args, **kwargs):
            if command[0] == "git":
                return real_run(command, *args, **kwargs)
            attempt_output = Path(command[command.index("-o") + 1])
            self.assertTrue(attempt_output.parent.is_dir())
            attempt_output.write_text(json.dumps(self._luna_result()), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout='{"event":"done"}\n', stderr="")

        with self._patch_codex(require_parent_then_write):
            workflow.run_codex("luna", self._prepare(self._task()), "bounded", self._paths(output_path))

    def test_luna_blocked_stops_the_pipeline_without_running_the_next_role(self):
        state_root = Path(self.temporary_directory.name) / "state"
        store = workflow.WorkflowStore(state_root)
        task = self._task()
        store.create_task(task)
        _install_declaration(
            store,
            task,
            allowed_roles=("luna", "sol_planner"),
            active_roles=("luna",),
        )
        runner = ScriptedRunner([self._luna_result("BLOCKED")])

        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", state_root):
            state = workflow.run_until_gate(task["task_id"], runner=runner, allow_live_model=False)

        self.assertEqual(state, "BLOCKED")
        self.assertEqual(runner.calls, ["luna"])

    def test_terra_actual_forbidden_file_is_blocked_even_when_the_model_declares_no_changes(self):
        task = self._task("REMEDIATION")
        source_worktree = self.repo / ".codex-worktrees" / task["task_id"].lower()
        source_worktree.parent.mkdir()
        self._git(
            "worktree",
            "add",
            "-b",
            f"aiwf/{task['task_id'].lower()}",
            str(source_worktree),
            self.base_commit,
        )
        task["source_worktree"] = str(source_worktree)
        state_root = Path(self.temporary_directory.name) / "state"
        store = workflow.WorkflowStore(state_root)
        store.create_task(task)
        _install_declaration(
            store,
            task,
            allowed_roles=("terra", "luna", "sol_reviewer"),
            active_roles=("terra",),
        )
        registry = ownership.OwnershipRegistry(
            schema_version=ownership.OWNERSHIP_REGISTRY_SCHEMA_VERSION,
            task_id=str(task["task_id"]),
            envelope_hash=artifacts.artifact_sha256(task),
            path_owners={"allowed": "terra"},
            registered_at_utc="2026-08-28T00:00:00Z",
        )
        with store.lock(str(task["task_id"])):
            ownership.record_ownership_registry(store, str(task["task_id"]), registry)
        store.append_event(
            task["task_id"],
            {
                "event_type": "OWNER_DECISION",
                "new_state": "WORKTREE_READY",
                "task_sha256": workflow._task_sha256(store, task["task_id"]),
            },
        )
        store.record_decision(
            task["task_id"],
            {
                "decision": "approve_execution",
                "actor": "owner",
                "new_state": "APPROVED_FOR_EXECUTION",
                "task_sha256": workflow._task_sha256(store, task["task_id"]),
            },
        )

        class ForbiddenWriterRunner:
            is_live_model = True

            def __init__(self):
                self.calls = []

            def run(self, role, unused_task):
                self.calls.append(role)
                (source_worktree / "forbidden.txt").write_text("forbidden\n", encoding="utf-8")
                return FinalSafetyRegressionTest._result("terra", "BLOCKED")

        runner = ForbiddenWriterRunner()
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", state_root):
            state = workflow.run_until_gate(task["task_id"], runner=runner, allow_live_model=True)

        self.assertEqual(state, "BLOCKED")
        self.assertEqual(runner.calls, ["terra"])


class CodexSideEffectObservationTest(unittest.TestCase):
    def valid_task(self):
        return CodexRunnerTest().valid_task()

    def valid_result(self, role="luna", status="SUPPORTED"):
        return CodexRunnerTest().valid_result(role=role, status=status)

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_directory.name) / "repository"
        self.repo.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.email", "observe@example.test")
        self._git("config", "user.name", "Observe Test")
        self._git("config", "commit.gpgsign", "false")
        (self.repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "initial")
        self.state_root = Path(self.temporary_directory.name) / "state"
        self.store = workflow.WorkflowStore(self.state_root)

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
        task = self.valid_task()
        task["repository_root"] = str(self.repo)
        return task

    def _prepare(self, task, *, roles: tuple[str, ...] = ("luna",)):
        self.store.create_task(task)
        _install_declaration(self.store, task, allowed_roles=roles, active_roles=roles)
        return task

    def _paths(self, task):
        return workflow.RunPaths(
            repo=self.repo,
            output_path=Path(self.temporary_directory.name) / "luna-result.json",
            schema_path=ROOT / "config/ai_workflow_result.schema.json",
            logs_dir=Path(self.temporary_directory.name) / "logs",
            state_root=self.state_root,
        )

    def _patch_codex(self, handler):
        return mock.patch.object(workflow.subprocess, "Popen", _compat_popen(handler))

    def test_live_runner_records_new_worktree_file_kind(self):
        from scripts import ai_workflow_ownership as ownership

        task = self._prepare(self._task())
        result = self.valid_result()

        def write_new_file(command, *args, **kwargs):
            (self.repo / "observed.txt").write_text("from runner\n", encoding="utf-8")
            return write_codex_result(command, result)

        with self._patch_codex(write_new_file):
            with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_ROLE_MODIFIED_REPO"):
                workflow.run_codex("luna", task, "task contract", self._paths(task))
        kinds = {
            row["effect_kind"] for row in ownership.load_side_effects(self.store, task["task_id"])
        }
        self.assertTrue(kinds & {"OWNED_WRITE", "UNTRACKED_WRITE"})
        paths = {
            row["path"] for row in ownership.load_side_effects(self.store, task["task_id"])
        }
        self.assertIn("observed.txt", paths)

    def test_read_only_role_with_no_tree_change_has_no_locking_ledger_rows(self):
        from scripts import ai_workflow_ownership as ownership

        task = self._prepare(self._task())
        result = self.valid_result()
        with self._patch_codex(lambda command, *args, **kwargs: write_codex_result(command, result)):
            workflow.run_codex("luna", task, "task contract", self._paths(task))
        rows = ownership.load_side_effects(self.store, task["task_id"])
        self.assertFalse(any(row["effect_kind"] in ownership.LOCKING_EFFECT_KINDS for row in rows))
        self.assertFalse(ownership.has_ownership_locking_side_effect(self.store, task["task_id"]))

    def test_timeout_and_crash_record_unobserved_locking_effect(self):
        from scripts import ai_workflow_ownership as ownership

        task = self._prepare(self._task())
        with self._patch_codex(
            lambda command, *args, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("codex", 30)
            )
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "CODEX_TIMEOUT"):
                workflow.run_codex("luna", task, "task contract", self._paths(task))
        self.assertTrue(ownership.has_ownership_locking_side_effect(self.store, task["task_id"]))
        kinds = {
            row["effect_kind"] for row in ownership.load_side_effects(self.store, task["task_id"])
        }
        self.assertIn("UNOBSERVED_ASSUMED_PRESENT", kinds)

        other_task = self._task()
        other_task["task_id"] = "AWF-20260803-099"
        self._prepare(other_task)
        with mock.patch.object(
            workflow.subprocess,
            "Popen",
            _compat_popen(
                lambda command, *args, **kwargs: subprocess.CompletedProcess(
                    command, 0, "", ""
                ),
                raise_on_communicate=OSError("codex crashed"),
            ),
        ):
            with self.assertRaises(OSError):
                workflow.run_codex("luna", other_task, "task contract", self._paths(other_task))
        self.assertTrue(
            ownership.has_ownership_locking_side_effect(self.store, other_task["task_id"])
        )

    def test_host_observation_does_not_call_record_side_effect_from_this_test(self):
        from scripts import ai_workflow_ownership as ownership

        task = self._prepare(self._task())
        result = self.valid_result()

        def write_new_file(command, *args, **kwargs):
            (self.repo / "host-observed.txt").write_text("host\n", encoding="utf-8")
            return write_codex_result(command, result)

        with self._patch_codex(write_new_file):
            with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_ROLE_MODIFIED_REPO"):
                workflow.run_codex("luna", task, "task contract", self._paths(task))
        rows = ownership.load_side_effects(self.store, task["task_id"])
        self.assertTrue(rows)
        self.assertIn("host-observed.txt", {row["path"] for row in rows})

    def test_construction_run_codex_records_frozen_step_producer(self):
        from tests.test_ai_workflow_construction_execution import (
            construction_plan,
            remediation_task,
        )
        from scripts import ai_workflow_ownership as ownership

        rollout_events_with_commands = (
            {
                "type": "thread.started",
                "thread_id": "019fc73c-4d40-7c20-a82a-c5a9ae078bcf",
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "python -m unittest tests.test_parser",
                    "cwd": "/work",
                    "exit_code": 0,
                    "aggregated_output": "ok",
                },
            },
            {"type": "turn.completed"},
        )

        task = remediation_task()
        task_id = task["task_id"]
        worktree = self.repo / ".codex-worktrees" / task_id.lower()
        self._git("worktree", "add", str(worktree), "HEAD")
        task["repository_root"] = str(self.repo)
        task["source_worktree"] = str(worktree)
        task["base_commit"] = self._git("rev-parse", "HEAD")
        frozen = workflow.validate_plan(construction_plan(task=task), task)
        context = workflow.ConstructionExecutionContext(
            plan=frozen,
            step=frozen.tasks[0],
            dispatch_id="d" * 64,
            task_sha256=frozen.task_sha256,
            request_sha256="e" * 64,
            role="luna_construction",
        )
        self.store.create_task(task)
        _install_declaration(
            self.store,
            task,
            allowed_roles=("luna_construction",),
            active_roles=("luna_construction",),
        )
        registry = ownership.build_ownership_registry(
            task_id=task_id,
            envelope_hash=artifacts.artifact_sha256(task),
            plan=frozen,
            registered_at_utc="2026-08-28T00:00:00Z",
        )
        with self.store.lock(task_id):
            ownership.record_ownership_registry(self.store, task_id, registry)
        prompt = workflow.build_construction_role_prompt(task, context)
        result = {
            "schema_version": "ai-result-1",
            "role": "luna_construction",
            "status": "BLOCKED",
            "summary": "bounded construction stopped.",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "BLOCKED",
        }
        stdout = "\n".join(json.dumps(event) for event in rollout_events_with_commands) + "\n"

        def write_construction(command, *args, **kwargs):
            written = write_codex_result(command, result)
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=written.stderr)

        paths = workflow.RunPaths(
            repo=worktree,
            output_path=Path(self.temporary_directory.name) / "luna-construction.json",
            schema_path=ROOT / "config/ai_workflow_result.schema.json",
            logs_dir=Path(self.temporary_directory.name) / "construction-logs",
            state_root=self.state_root,
        )
        with (
            mock.patch.object(workflow, "_assert_terra_worktree_authorized"),
            self._patch_codex(write_construction),
        ):
            try:
                workflow.run_codex(
                    "luna_construction",
                    task,
                    prompt,
                    paths,
                    construction_plan=construction_plan(task=task),
                    construction_step_id="construction-601",
                    construction_context=context,
                )
            except workflow.WorkflowError:
                pass
        generated = [
            row
            for row in ownership.load_side_effects(self.store, task_id)
            if row["effect_kind"] == "COMMAND_GENERATED"
        ]
        self.assertEqual(1, len(generated))
        self.assertEqual("CONSTRUCTION_FROZEN_STEP", generated[0]["producer"])
        self.assertEqual(
            f"{frozen.plan_sha256}:{frozen.tasks[0].id}",
            generated[0]["producer_ref"],
        )


class DispatchGateHubTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repo = root / "repository"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "hub@example.test"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Hub Test"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "README.md").write_text("repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
        self.state_root = root / "state"
        self.store = workflow.WorkflowStore(self.state_root)
        self.task = TaskValidationTest().valid_task()
        self.task["repository_root"] = str(self.repo)
        self.store.create_task(self.task)
        self.task_id = str(self.task["task_id"])
        _RecordingPopen.reset()
        legacy_config = workflow._load_workflow_config()
        legacy_config["routing"] = {"mode": "legacy", "role_policy": "legacy"}
        self._legacy_policy = mock.patch.object(
            workflow, "_load_workflow_config", return_value=legacy_config
        )
        self._legacy_policy.start()

    def tearDown(self) -> None:
        self._legacy_policy.stop()
        self.temporary.cleanup()

    def _paths(self) -> workflow.RunPaths:
        task_dir = self.state_root / self.task_id
        return workflow.RunPaths(
            repo=self.repo,
            output_path=task_dir / "luna-result.json",
            schema_path=ROOT / "config/ai_workflow_result.schema.json",
            logs_dir=task_dir / "logs",
            state_root=self.state_root,
        )

    def _popen(self, result: dict[str, object]):
        class Popen(_RecordingPopen):
            _result = result

        return Popen

    def _permit_records(self) -> list[dict[str, object]]:
        path = self.store._require_task(self.task_id) / policy.DISPATCH_PERMIT_LEDGER
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def _dispatch_records(self) -> list[dict[str, object]]:
        path = self.store._require_task(self.task_id) / "dispatches.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_missing_declaration_rejects_run_codex_without_spawn(self) -> None:
        popen = self._popen(CodexRunnerTest().valid_result())
        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_DECLARATION_MISSING"):
                workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertEqual([], popen.calls)
        self.assertEqual([], self._permit_records())
        self.assertEqual([], self._dispatch_records())

    def test_state_root_none_is_declaration_missing(self) -> None:
        paths = workflow.RunPaths(
            repo=self.repo,
            output_path=Path(self.temporary.name) / "out.json",
            schema_path=ROOT / "config/ai_workflow_result.schema.json",
            logs_dir=Path(self.temporary.name) / "logs",
        )
        popen = self._popen(CodexRunnerTest().valid_result())
        with mock.patch.object(workflow.subprocess, "Popen", popen):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_DECLARATION_MISSING"):
                workflow.run_codex("luna", self.task, "task contract", paths)
        self.assertEqual([], popen.calls)

    def test_legal_declaration_reserves_then_starts_same_permit(self) -> None:
        _install_declaration(self.store, self.task, allowed_roles=("luna",), active_roles=("luna",))
        result = CodexRunnerTest().valid_result()
        popen = self._popen(result)
        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            actual = workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertEqual(result, actual)
        self.assertEqual(1, len(popen.calls))
        records = self._permit_records()
        self.assertEqual(["RESERVED", "STARTED"], [row["state"] for row in records])
        self.assertEqual(records[0]["permit_id"], records[1]["permit_id"])
        self.assertEqual([1, 2], [row["seq"] for row in records])

    def test_hub_lock_bodies_forbid_self_lock_wrappers(self) -> None:
        for function in (workflow.run_codex, workflow.run_assignment):
            for block in _with_lock_blocks(function):
                names = [_call_name(node) for child in ast.walk(block) if isinstance(child, ast.Call) for node in [child]]
                for wrapper in HUB_SELF_LOCK_WRAPPERS:
                    self.assertNotIn(wrapper, names, function.__name__)

    def test_hubs_release_only_through_never_spawned_helper(self) -> None:
        for function in (workflow.run_codex, workflow.run_assignment):
            source = inspect.getsource(function)
            self.assertIn("require_dispatch_permit_locked(", source)
            self.assertIn("claim_permit_start_locked(", source)
            self.assertIn("release_permit_if_never_spawned(", source)
            tree = ast.parse(source)
            names = [
                _call_name(node)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
            ]
            self.assertIn("release_permit_if_never_spawned", names)
            self.assertNotIn("release_permit_before_start", names)

    def test_helper_is_unique_direct_caller_and_only_releases_when_unspawned(self) -> None:
        module_path = ROOT / "scripts" / "ai_workflow_dispatch_policy.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        callers: list[str] = []
        helper = None
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name == "release_permit_if_never_spawned":
                helper = node
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and _call_name(child) == "release_permit_before_start":
                    callers.append(node.name)
        self.assertEqual(["release_permit_if_never_spawned"], callers)
        assert helper is not None
        if_nodes = [node for node in helper.body if isinstance(node, ast.If)]
        self.assertTrue(if_nodes)
        first = if_nodes[0]
        self.assertIsInstance(first.test, ast.Name)
        self.assertEqual("spawned", first.test.id)
        self.assertTrue(first.body)
        self.assertIsInstance(first.body[0], ast.Return)
        release_names = [
            _call_name(node)
            for node in ast.walk(helper)
            if isinstance(node, ast.Call)
        ]
        self.assertIn("release_permit_before_start", release_names)

    def test_popen_is_immediately_followed_by_claim(self) -> None:
        tree = ast.parse(inspect.getsource(workflow.run_codex))
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        found = False
        for block in _with_lock_blocks(workflow.run_codex):
            statements = [
                node
                for node in block.body
                if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
            ]
            for index, statement in enumerate(statements):
                if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Call):
                    continue
                if _call_name(statement.value) != "Popen":
                    continue
                following = statements[index + 1]
                call = following.value if isinstance(following, ast.Expr) else (
                    following if isinstance(following, ast.Expr) else None
                )
                if isinstance(following, ast.Expr) and isinstance(following.value, ast.Call):
                    self.assertEqual("claim_permit_start_locked", _call_name(following.value))
                    found = True
                elif isinstance(following, ast.Assign) and isinstance(following.value, ast.Call):
                    self.assertEqual("claim_permit_start_locked", _call_name(following.value))
                    found = True
                else:
                    self.fail("claim_permit_start_locked must follow Popen immediately")
        self.assertTrue(found)

    def test_schema_materialize_failure_releases_and_retires_identity(self) -> None:
        _install_declaration(self.store, self.task, allowed_roles=("luna",), active_roles=("luna",))
        popen = self._popen(CodexRunnerTest().valid_result())
        helper = mock.Mock(wraps=policy.release_permit_if_never_spawned)
        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(
                workflow,
                "materialize_dispatch_result_schema",
                side_effect=workflow.WorkflowError("RESULT_SCHEMA_DERIVATION_INVALID", "boom"),
            ),
            mock.patch.object(workflow, "release_permit_if_never_spawned", helper),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "RESULT_SCHEMA_DERIVATION_INVALID"):
                workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertEqual([], popen.calls)
        helper.assert_called()
        self.assertFalse(helper.call_args.kwargs["spawned"])
        records = self._permit_records()
        self.assertEqual(["RESERVED", "RELEASED_BEFORE_START"], [row["state"] for row in records])
        identity = records[0]["permit_id"]
        context = workflow.AttemptAccountingContext(
            task_id=self.task_id,
            role="luna",
            retry_kind="none",
            attempt_id="replay-same",
        )
        with mock.patch.object(
            workflow,
            "_require_attempt_accounting_context",
            return_value=context,
        ):
            with (
                mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
                mock.patch.object(workflow, "working_tree_paths", return_value=set()),
                mock.patch.object(
                    workflow,
                    "derive_dispatch_identity",
                    return_value=identity,
                ),
                mock.patch.object(workflow.subprocess, "Popen", popen),
            ):
                with self.assertRaisesRegex(workflow.WorkflowError, "DISPATCH_IDENTITY_RETIRED"):
                    workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertEqual([], popen.calls)

    def test_timeout_after_spawn_does_not_release(self) -> None:
        _install_declaration(self.store, self.task, allowed_roles=("luna",), active_roles=("luna",))

        class TimeoutPopen(_RecordingPopen):
            def communicate(self, input=None, timeout=None):
                raise subprocess.TimeoutExpired(self.args, timeout)

        helper = mock.Mock(wraps=policy.release_permit_if_never_spawned)
        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow, "release_permit_if_never_spawned", helper),
            mock.patch.object(workflow.subprocess, "Popen", TimeoutPopen),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "CODEX_TIMEOUT"):
                workflow.run_codex("luna", self.task, "task contract", self._paths())
        helper.assert_called()
        self.assertTrue(helper.call_args.kwargs["spawned"])
        records = self._permit_records()
        self.assertEqual(["RESERVED", "STARTED"], [row["state"] for row in records])
        effects = ownership.load_side_effects(self.store, self.task_id)
        self.assertTrue(any(row.get("effect_kind") == "UNOBSERVED_ASSUMED_PRESENT" for row in effects))

    def test_claim_failure_kills_process_without_release(self) -> None:
        _install_declaration(self.store, self.task, allowed_roles=("luna",), active_roles=("luna",))
        popen = self._popen(CodexRunnerTest().valid_result())

        def boom(*_args, **_kwargs):
            raise workflow.WorkflowError("DISPATCH_PERMIT_STATE_ILLEGAL", "claim boom")

        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow, "claim_permit_start_locked", boom),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "DISPATCH_PERMIT_STATE_ILLEGAL"):
                workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertEqual(1, len(popen.instances))
        self.assertTrue(popen.instances[0].killed)
        records = self._permit_records()
        self.assertEqual(["RESERVED"], [row["state"] for row in records])

    def test_started_identity_cannot_respawn(self) -> None:
        _install_declaration(self.store, self.task, allowed_roles=("luna",), active_roles=("luna",))
        result = CodexRunnerTest().valid_result()
        popen = self._popen(result)
        context = workflow.AttemptAccountingContext(
            task_id=self.task_id,
            role="luna",
            retry_kind="none",
            attempt_id="fixed-attempt",
        )
        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow, "_require_attempt_accounting_context", return_value=context),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            workflow.run_codex("luna", self.task, "task contract", self._paths())
            with self.assertRaisesRegex(workflow.WorkflowError, "DISPATCH_PERMIT_ALREADY_STARTED"):
                workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertEqual(1, len(popen.calls))

    def test_technical_retry_gets_a_new_permit(self) -> None:
        _install_declaration(self.store, self.task, allowed_roles=("luna",), active_roles=("luna",), max_dispatches=4)
        first = workflow.AttemptAccountingContext(
            task_id=self.task_id, role="luna", retry_kind="none", attempt_id="attempt-a"
        )
        second = workflow.AttemptAccountingContext(
            task_id=self.task_id, role="luna", retry_kind="technical", attempt_id="attempt-b"
        )
        contexts = iter((first, second))
        popen = self._popen(CodexRunnerTest().valid_result())
        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(
                workflow,
                "_require_attempt_accounting_context",
                side_effect=lambda *args, **kwargs: next(contexts),
            ),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            workflow.run_codex("luna", self.task, "task contract", self._paths())
            workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertEqual(2, len(popen.calls))
        records = self._permit_records()
        self.assertEqual(["RESERVED", "STARTED", "RESERVED", "STARTED"], [row["state"] for row in records])
        self.assertNotEqual(records[0]["permit_id"], records[2]["permit_id"])

    def test_fake_runner_missing_declaration_is_rejected(self) -> None:
        events = self.store._require_task(self.task_id) / "events.jsonl"
        events.write_text(
            json.dumps(
                {
                    "event_type": "STATE_TRANSITION",
                    "previous_state": "DRAFT",
                    "new_state": "TASK_VALIDATED",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runner = ScriptedRunner([CodexRunnerTest().valid_result()])
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_DECLARATION_MISSING"):
                workflow.run_until_gate(
                    self.task_id,
                    runner=runner,
                    allow_live_model=False,
                    state_root=self.state_root,
                )
        self.assertEqual([], runner.calls)

    def test_fake_runner_legal_path_starts_permit_and_keeps_it_on_runner_error(self) -> None:
        _install_declaration(self.store, self.task, allowed_roles=("luna", "sol_planner"), active_roles=("luna", "sol_planner"))
        runner = ScriptedRunner([RuntimeError("runner boom")])
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            with self.assertRaises(RuntimeError):
                workflow.run_until_gate(self.task_id, runner=runner, allow_live_model=False, state_root=self.state_root)
        self.assertEqual(["luna"], runner.calls)
        records = self._permit_records()
        self.assertEqual(["RESERVED", "STARTED"], [row["state"] for row in records])

    def test_write_role_without_authorization_does_not_spawn(self) -> None:
        self.task["task_type"] = "REMEDIATION"
        self.task["allowed_write_paths"] = ["src"]
        self.task["source_worktree"] = str(self.repo)
        task_dir = self.store._require_task(self.task_id)
        (task_dir / "task.json").write_text(workflow._canonical_json(self.task) + "\n", encoding="utf-8")
        _install_declaration(
            self.store,
            self.task,
            allowed_roles=("terra",),
            active_roles=("terra",),
        )
        registry = ownership.OwnershipRegistry(
            schema_version=ownership.OWNERSHIP_REGISTRY_SCHEMA_VERSION,
            task_id=self.task_id,
            envelope_hash=artifacts.artifact_sha256(self.task),
            path_owners={"src": "luna"},
            registered_at_utc="2026-08-28T00:00:00Z",
        )
        with self.store.lock(self.task_id):
            ownership.record_ownership_registry(self.store, self.task_id, registry)
        popen = self._popen(CodexRunnerTest().valid_result("terra", "IMPLEMENTED_CANDIDATE"))
        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow, "_assert_terra_worktree_authorized"),
            mock.patch.object(workflow, "_reject_dirty_input"),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "OWNERSHIP_TRANSFER_BLOCKED"):
                workflow.run_codex("terra", self.task, "task contract", self._paths())
        self.assertEqual([], popen.calls)

    def test_historical_task_without_route_decision_is_fail_closed(self) -> None:
        events = self.store._require_task(self.task_id) / "events.jsonl"
        events.write_text(
            json.dumps({"event_type": "STATE_TRANSITION", "new_state": "DRAFT"}) + "\n",
            encoding="utf-8",
        )
        popen = self._popen(CodexRunnerTest().valid_result())
        with mock.patch.object(workflow.subprocess, "Popen", popen):
            with self.assertRaisesRegex(
                workflow.WorkflowError, "ROUTE_DECLARATION_MISSING|ROUTE_DECLARATION_LATE"
            ):
                workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertEqual([], popen.calls)
        self.assertFalse((self.store._require_task(self.task_id) / "route-declaration.json").is_file())

    def test_deleted_declaration_is_not_silently_rewritten(self) -> None:
        _install_declaration(self.store, self.task, allowed_roles=("luna",), active_roles=("luna",))
        path = self.store._require_task(self.task_id) / "route-declaration.json"
        path.unlink()
        popen = self._popen(CodexRunnerTest().valid_result())
        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            with self.assertRaisesRegex(
                workflow.WorkflowError, "ROUTE_DECLARATION_MISSING|ROUTE_DECLARATION_CORRUPT"
            ):
                workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertEqual([], popen.calls)
        self.assertFalse(path.is_file())

    def test_crash_window_recovers_event_without_rewriting_bytes(self) -> None:
        declaration = _install_declaration(
            self.store, self.task, allowed_roles=("luna",), active_roles=("luna",)
        )
        path = self.store._require_task(self.task_id) / "route-declaration.json"
        before = path.read_bytes()
        events = self.store._require_task(self.task_id) / "events.jsonl"
        kept = [
            line
            for line in events.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event_type") != "ROUTE_DECLARED"
        ]
        events.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        loaded = declarations.load_route_declaration(self.store, self.task_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(declaration.to_dict(), loaded.to_dict())
        self.assertEqual(before, path.read_bytes())
        restored = [
            json.loads(line)
            for line in events.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event_type") == "ROUTE_DECLARED"
        ]
        self.assertEqual(1, len(restored))

    def test_live_luna_missing_declaration_does_not_call_run_codex(self) -> None:
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


if __name__ == "__main__":
    unittest.main()

