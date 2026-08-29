"""Host-static role preflight with internally recaptured PreflightContext."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_declarations as declarations
from scripts import ai_workflow_dispatch_policy as policy
from scripts import ai_workflow_preflight as preflight
from scripts import sync_plugin
from scripts.ai_workflow_routing import RuntimeRouteDecision
from tests.test_ai_workflow import ScriptedRunner, _compat_popen
from tests.test_ai_workflow_runtime import THREAD_ID, blocked_luna_result


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE_PATH = SCRIPTS / "ai_workflow_preflight.py"
TASK_ID = "AWF-20260803-001"
OTHER_TASK_ID = "AWF-20260803-002"
PYTHON311 = Path("/Users/lee/.local/bin/python3.11")
PUBLIC_SAFETY_ENTRIES = (
    "run_role_preflight",
    "run_role_preflight_locked",
    "is_role_preflighted",
    "is_role_preflighted_locked",
    "require_role_preflighted",
    "require_role_preflighted_locked",
)
LOCKED_VARIANTS = (
    "compute_preflight_context",
    "run_role_preflight_locked",
    "is_role_preflighted_locked",
    "require_role_preflighted_locked",
)
INJECTOR_NAMES = frozenset(
    {"context", "route_config_hash", "root", "process_generation"}
)


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        shell=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace')}"
        )
    return result


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "-b", "main")
    _run_git(path, "config", "user.email", "preflight@example.test")
    _run_git(path, "config", "user.name", "Preflight Test")
    _run_git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("repo\n", encoding="utf-8")
    _run_git(path, "add", "README.md")
    _run_git(path, "commit", "-m", "init")
    return path


def _valid_task(*, task_id: str, repository_root: Path) -> dict[str, object]:
    return {
        "schema_version": "ai-task-1",
        "task_id": task_id,
        "task_type": "PLAN",
        "objective": "Review the approved workflow specification",
        "repository_root": str(repository_root),
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


def _runtime_decision(task: dict[str, object]) -> RuntimeRouteDecision:
    wire = artifacts.RouteDecision(
        task_id=str(task["task_id"]),
        route="direct",
        rule_id="PROMPT_SUFFICIENT_ROUTE",
        task_sha256=artifacts.artifact_sha256(task),
        request_sha256="a" * 64,
        decided_at_utc="2026-08-03T00:00:00Z",
        routing_mode="enforced",
        evidence_class="unavailable",
    )
    artifacts.validate_route_decision(wire.to_dict())
    return RuntimeRouteDecision(
        wire=wire,
        roles=("luna",),
        shadow_route=None,
        effective_roles=("luna",),
    )


def _build_declaration(
    decision: RuntimeRouteDecision,
    *,
    route_config_hash: str = "b" * 64,
    allowed_roles: tuple[str, ...] = ("luna", "sol_planner"),
    active_roles: tuple[str, ...] = ("luna",),
) -> declarations.RouteDeclaration:
    return declarations.build_route_declaration(
        decision=decision,
        route_config_hash=route_config_hash,
        allowed_roles=allowed_roles,
        active_roles=active_roles,
        rule_ids=(decision.rule_id,),
        reason_codes=("PROMPT_SUFFICIENT",),
        max_dispatches=2,
        allowed_transitions=({"from_role": "luna", "to_role": "sol_planner"},),
    )


def _first_call_name(function) -> str | None:
    tree = ast.parse(inspect.getsource(function))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    for node in func.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
        else:
            return None
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        if isinstance(call.func, ast.Name):
            return call.func.id
        return None
    return None


def _wrapper_statements(function) -> list[ast.stmt]:
    tree = ast.parse(inspect.getsource(function))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    return [
        node
        for node in func.body
        if not (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        )
    ]


def _calls_name(function, name: str) -> bool:
    tree = ast.parse(inspect.getsource(function))
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == name:
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr == name:
            return True
    return False


def _context(
    *,
    task_id: str = TASK_ID,
    route_config_hash: str = "b" * 64,
    runtime_profile_hash: str = "c" * 64,
    install_version: str = "d" * 64,
    launcher_version: str = "ai-workflow-launcher-1",
    cwd: str = "/work",
    worktree_id: str = "/repo",
    process_generation: str = "e" * 32,
) -> preflight.PreflightContext:
    return preflight.PreflightContext(
        task_id=task_id,
        route_config_hash=route_config_hash,
        runtime_profile_hash=runtime_profile_hash,
        install_version=install_version,
        launcher_version=launcher_version,
        cwd=cwd,
        worktree_id=worktree_id,
        process_generation=process_generation,
    )


class _PreflightStoreMixin:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repo = _init_repo(root / "repository")
        self.store = workflow.WorkflowStore(root / "state")
        self.task = _valid_task(task_id=TASK_ID, repository_root=self.repo)
        self.store.create_task(self.task)
        self.decision = _runtime_decision(self.task)
        self.store.write_task_artifact_once(
            TASK_ID,
            "route-decision.json",
            self.decision.to_dict(),
            conflict_code="ROUTE_ALREADY_FROZEN",
        )
        self.declaration = _build_declaration(self.decision)
        with self.store.lock(TASK_ID):
            declarations.record_route_declaration(
                self.store, TASK_ID, self.declaration
            )
        (self.repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed_other_task(self, *, task_id: str = OTHER_TASK_ID) -> str:
        task = _valid_task(task_id=task_id, repository_root=self.repo)
        self.store.create_task(task)
        decision = _runtime_decision(task)
        self.store.write_task_artifact_once(
            task_id,
            "route-decision.json",
            decision.to_dict(),
            conflict_code="ROUTE_ALREADY_FROZEN",
        )
        declaration = _build_declaration(decision)
        with self.store.lock(task_id):
            declarations.record_route_declaration(self.store, task_id, declaration)
        return task_id

    def _ledger_path(self, task_id: str = TASK_ID) -> Path:
        return self.store._require_task(task_id) / "preflight-records.jsonl"


class PreflightStaticChecksTest(unittest.TestCase):
    def test_unknown_role_is_rejected(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_ENUM"):
            preflight._run_preflight_checks("not_a_role", _context())

    def test_missing_pinned_fields_records_fail(self) -> None:
        self.assertEqual("", preflight.compute_runtime_profile_hash({"model": "x"}))
        result = preflight._run_preflight_checks(
            "luna", _context(runtime_profile_hash="")
        )
        self.assertEqual("FAIL", result["status"])
        self.assertIn("cache_key", result)

    def test_valid_role_records_pass_with_cache_key(self) -> None:
        context = _context()
        result = preflight._run_preflight_checks("luna", context)
        self.assertEqual("PASS", result["status"])
        self.assertEqual(context.cache_key(), result["cache_key"])

    def test_module_has_no_executor_or_model_call_surface(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        banned_params = {"executor", "model_client", "codex", "runner"}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = {arg.arg for arg in node.args.args}
            names.update(arg.arg for arg in node.args.kwonlyargs)
            if node.args.vararg is not None:
                names.add(node.args.vararg.arg)
            if node.args.kwarg is not None:
                names.add(node.args.kwarg.arg)
            overlap = names & banned_params
            self.assertFalse(overlap, f"{node.name} has banned params {sorted(overlap)}")
        self.assertNotIn("run_codex", source)
        self.assertNotRegex(source, r"\bopenai\b")
        self.assertNotRegex(source, r"\bexecutor\b")

    def test_private_helpers_have_no_store_parameter(self) -> None:
        for name in ("_run_preflight_checks", "_preflight_record_matches"):
            self.assertTrue(name.startswith("_"))
            parameters = inspect.signature(getattr(preflight, name)).parameters
            self.assertNotIn("store", parameters)


class PreflightCacheKeyTest(_PreflightStoreMixin, unittest.TestCase):
    def test_second_is_role_preflighted_hits_without_rerunning_checks(self) -> None:
        with mock.patch.object(
            preflight,
            "_run_preflight_checks",
            wraps=preflight._run_preflight_checks,
        ) as spy:
            result = preflight.run_role_preflight(self.store, TASK_ID, "luna")
            self.assertEqual("PASS", result["status"])
            self.assertEqual(1, spy.call_count)
            self.assertTrue(preflight.is_role_preflighted(self.store, TASK_ID, "luna"))
            self.assertEqual(1, spy.call_count)

    def test_any_context_factor_change_misses_cache(self) -> None:
        preflight.run_role_preflight(self.store, TASK_ID, "luna")
        self.assertTrue(preflight.is_role_preflighted(self.store, TASK_ID, "luna"))
        cases = (
            ("route_config_hash", "1" * 64),
            ("runtime_profile_hash", "2" * 64),
            ("install_version", "3" * 64),
            ("launcher_version", "ai-workflow-launcher-other"),
            ("cwd", "/changed-cwd"),
            ("worktree_id", "/changed-worktree"),
            ("process_generation", "f" * 32),
        )
        original = preflight.compute_preflight_context
        for field, value in cases:
            with self.subTest(field=field):
                def _shifted(store, task_id, *, role, _field=field, _value=value):
                    current = original(store, task_id, role=role)
                    return preflight.PreflightContext(
                        **{**current.__dict__, _field: _value}
                    )

                with mock.patch.object(
                    preflight, "compute_preflight_context", side_effect=_shifted
                ):
                    self.assertFalse(
                        preflight.is_role_preflighted(self.store, TASK_ID, "luna")
                    )

    def test_cache_does_not_hit_across_tasks(self) -> None:
        preflight.run_role_preflight(self.store, TASK_ID, "luna")
        other = self._seed_other_task()
        with self.store.lock(TASK_ID):
            left = preflight.compute_preflight_context(
                self.store, TASK_ID, role="luna"
            )
        with self.store.lock(other):
            right = preflight.compute_preflight_context(
                self.store, other, role="luna"
            )
        self.assertEqual(left.cache_key(), right.cache_key())
        self.assertFalse(preflight.is_role_preflighted(self.store, other, "luna"))

    def test_roles_on_same_task_have_independent_records(self) -> None:
        preflight.run_role_preflight(self.store, TASK_ID, "luna")
        self.assertTrue(preflight.is_role_preflighted(self.store, TASK_ID, "luna"))
        self.assertFalse(
            preflight.is_role_preflighted(self.store, TASK_ID, "sol_planner")
        )
        preflight.run_role_preflight(self.store, TASK_ID, "sol_planner")
        self.assertTrue(
            preflight.is_role_preflighted(self.store, TASK_ID, "sol_planner")
        )
        self.assertTrue(preflight.is_role_preflighted(self.store, TASK_ID, "luna"))
        lines = self._ledger_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(2, len(lines))
        roles = [json.loads(line)["role"] for line in lines]
        self.assertEqual(["luna", "sol_planner"], roles)

    def test_expired_recheck_appends_without_rewriting_old_bytes(self) -> None:
        preflight.run_role_preflight(self.store, TASK_ID, "luna")
        path = self._ledger_path()
        old_bytes = path.read_bytes()
        original = preflight.compute_preflight_context

        def _shifted(store, task_id, *, role):
            current = original(store, task_id, role=role)
            return preflight.PreflightContext(
                **{**current.__dict__, "cwd": "/changed-cwd"}
            )

        with mock.patch.object(
            preflight, "compute_preflight_context", side_effect=_shifted
        ):
            self.assertFalse(preflight.is_role_preflighted(self.store, TASK_ID, "luna"))
            result = preflight.run_role_preflight(self.store, TASK_ID, "luna")
        self.assertEqual("PASS", result["status"])
        new_bytes = path.read_bytes()
        self.assertTrue(new_bytes.startswith(old_bytes))
        self.assertGreater(len(new_bytes), len(old_bytes))
        self.assertEqual(old_bytes.splitlines()[0], new_bytes.splitlines()[0])


class PreflightContextAuthorityTest(_PreflightStoreMixin, unittest.TestCase):
    def test_context_hash_follows_stored_declaration_not_caller_belief(self) -> None:
        with self.store.lock(TASK_ID):
            context = preflight.compute_preflight_context(
                self.store, TASK_ID, role="luna"
            )
        self.assertEqual("b" * 64, context.route_config_hash)
        path = self.store._require_task(TASK_ID) / "route-declaration.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["route_config_hash"] = "c" * 64
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with self.store.lock(TASK_ID):
            shifted = preflight.compute_preflight_context(
                self.store, TASK_ID, role="luna"
            )
        self.assertEqual("c" * 64, shifted.route_config_hash)
        self.assertNotEqual(context.cache_key(), shifted.cache_key())

    def test_missing_declaration_is_route_declaration_missing(self) -> None:
        path = self.store._require_task(TASK_ID) / "route-declaration.json"
        path.unlink()
        events = self.store._require_task(TASK_ID) / "events.jsonl"
        if events.is_file():
            events.unlink()
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "ROUTE_DECLARATION_MISSING"
            ):
                preflight.compute_preflight_context(self.store, TASK_ID, role="luna")

    def test_compute_preflight_context_has_no_injector_parameters(self) -> None:
        parameters = inspect.signature(preflight.compute_preflight_context).parameters
        self.assertEqual(("store", "task_id", "role"), tuple(parameters))
        self.assertEqual(
            inspect.Parameter.KEYWORD_ONLY, parameters["role"].kind
        )
        for name in INJECTOR_NAMES:
            self.assertNotIn(name, parameters)

    def test_safety_entries_have_no_context_parameter(self) -> None:
        for name in PUBLIC_SAFETY_ENTRIES:
            parameters = inspect.signature(getattr(preflight, name)).parameters
            self.assertEqual(("store", "task_id", "role"), tuple(parameters))
            self.assertNotIn("context", parameters)
            for injector in INJECTOR_NAMES:
                self.assertNotIn(injector, parameters)

    def test_six_public_entries_recapture_context(self) -> None:
        for name in PUBLIC_SAFETY_ENTRIES:
            function = getattr(preflight, name)
            if name.endswith("_locked"):
                self.assertTrue(
                    _calls_name(function, "compute_preflight_context"),
                    f"{name} must call compute_preflight_context",
                )
            else:
                stmts = _wrapper_statements(function)
                self.assertEqual(1, len(stmts), name)
                self.assertIsInstance(stmts[0], ast.With)
                self.assertTrue(
                    _calls_name(function, f"{name}_locked"),
                    f"{name} must delegate to {name}_locked",
                )

    def test_locked_variants_assert_lock_first(self) -> None:
        for name in LOCKED_VARIANTS:
            self.assertEqual(
                "_assert_lock_held",
                _first_call_name(getattr(preflight, name)),
                name,
            )

    def test_locked_variants_require_held_lock(self) -> None:
        for name in LOCKED_VARIANTS:
            function = getattr(preflight, name)
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    artifacts.WorkflowError, "LOCK_REQUIRED"
                ):
                    if name == "compute_preflight_context":
                        function(self.store, TASK_ID, role="luna")
                    else:
                        function(self.store, TASK_ID, "luna")

    def test_require_unpreflighted_role_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "ROLE_NOT_PREFLIGHTED"
        ):
            preflight.require_role_preflighted(self.store, TASK_ID, "luna")
        preflight.run_role_preflight(self.store, TASK_ID, "luna")
        preflight.require_role_preflighted(self.store, TASK_ID, "luna")

    def test_missing_sessions_directory_does_not_pass_or_satisfy_require(self) -> None:
        sessions = self.repo / ".codex" / "sessions"
        if sessions.exists():
            if sessions.is_dir():
                shutil.rmtree(sessions)
            else:
                sessions.unlink()
        self.assertFalse(sessions.is_dir())
        result = preflight.run_role_preflight(self.store, TASK_ID, "luna")
        self.assertNotEqual("PASS", result["status"])
        self.assertFalse(preflight.is_role_preflighted(self.store, TASK_ID, "luna"))
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "ROLE_NOT_PREFLIGHTED"
        ):
            preflight.require_role_preflighted(self.store, TASK_ID, "luna")

    def test_missing_install_manifest_is_unavailable(self) -> None:
        path = ROOT / "config" / preflight.RUNTIME_MANIFEST_FILENAME
        original = path.read_bytes()
        try:
            path.unlink()
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "INSTALL_MANIFEST_UNAVAILABLE"
            ):
                preflight.compute_install_version()
        finally:
            path.write_bytes(original)

    def test_malformed_install_manifest_is_unavailable(self) -> None:
        path = ROOT / "config" / preflight.RUNTIME_MANIFEST_FILENAME
        original = path.read_bytes()
        try:
            path.write_text("[]\n", encoding="utf-8")
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "INSTALL_MANIFEST_UNAVAILABLE"
            ):
                preflight.compute_install_version()
        finally:
            path.write_bytes(original)

    def test_stub_role_config_records_fail_through_public_entry(self) -> None:
        with mock.patch.object(
            preflight,
            "_load_role_config",
            return_value={"model": "gpt-5.6-luna"},
        ):
            result = preflight.run_role_preflight(self.store, TASK_ID, "luna")
        self.assertEqual("FAIL", result["status"])
        self.assertIn("cache_key", result)
        self.assertFalse(preflight.is_role_preflighted(self.store, TASK_ID, "luna"))


class PreflightLedgerReplayTest(_PreflightStoreMixin, unittest.TestCase):
    def test_truncated_trailing_record_is_corrupt(self) -> None:
        preflight.run_role_preflight(self.store, TASK_ID, "luna")
        path = self._ledger_path()
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "PREFLIGHT_LEDGER_CORRUPT"
        ):
            preflight.is_role_preflighted(self.store, TASK_ID, "luna")

    def test_non_object_line_is_corrupt(self) -> None:
        preflight.run_role_preflight(self.store, TASK_ID, "luna")
        path = self._ledger_path()
        path.write_bytes(path.read_bytes() + b"[]\n")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "PREFLIGHT_LEDGER_CORRUPT"
        ):
            preflight.is_role_preflighted(self.store, TASK_ID, "luna")

    def test_cross_task_record_is_corrupt(self) -> None:
        preflight.run_role_preflight(self.store, TASK_ID, "luna")
        path = self._ledger_path()
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record["task_id"] = OTHER_TASK_ID
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "PREFLIGHT_LEDGER_CORRUPT"
        ):
            preflight.is_role_preflighted(self.store, TASK_ID, "luna")

    def test_ledger_record_has_no_seq_field(self) -> None:
        result = preflight.run_role_preflight(self.store, TASK_ID, "luna")
        self.assertNotIn("seq", result)
        record = json.loads(self._ledger_path().read_text(encoding="utf-8"))
        self.assertNotIn("seq", record)
        self.assertEqual("ai-preflight-record-1", record["schema_version"])

    def test_preflight_record_matches_uses_latest_row(self) -> None:
        first = {
            "role": "luna",
            "cache_key": "k",
            "status": "PASS",
            "task_id": TASK_ID,
        }
        second = {
            "role": "luna",
            "cache_key": "k",
            "status": "FAIL",
            "task_id": TASK_ID,
        }
        self.assertTrue(
            preflight._preflight_record_matches((first,), "luna", "k")
        )
        self.assertFalse(
            preflight._preflight_record_matches((first, second), "luna", "k")
        )


class PreflightDistributionContractTest(unittest.TestCase):
    def test_runtime_manifest_matches_runtime_files_bytes(self) -> None:
        path = ROOT / "config" / preflight.RUNTIME_MANIFEST_FILENAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("ai-runtime-files-1", manifest["schema_version"])
        listed = {item["name"]: item["sha256"] for item in manifest["files"]}
        self.assertEqual(list(sync_plugin.RUNTIME_FILES), [item["name"] for item in manifest["files"]])
        for name in sync_plugin.RUNTIME_FILES:
            digest = hashlib.sha256((SCRIPTS / name).read_bytes()).hexdigest()
            self.assertEqual(digest, listed[name], name)
        files_preimage = json.dumps(
            manifest["files"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(files_preimage).hexdigest(),
            manifest["aggregate_sha256"],
        )

    def test_check_fails_after_runtime_file_tamper(self) -> None:
        target = SCRIPTS / "ai_workflow_preflight.py"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b"\n# tampered runtime file\n")
            completed = subprocess.run(
                [str(PYTHON311), "scripts/sync_plugin.py", "--check"],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(0, completed.returncode)
            self.assertIn("PLUGIN_SYNC_FAILED", completed.stdout)
        finally:
            target.write_bytes(original)

    def test_config_keeps_rate_snapshot_and_adds_preflight_artifacts(self) -> None:
        self.assertIn(
            "ai_workflow_rate_snapshot.schema.json", sync_plugin.CONFIG_FILES
        )
        self.assertIn(
            "ai_workflow_preflight_record.schema.json", sync_plugin.CONFIG_FILES
        )
        self.assertIn(
            preflight.RUNTIME_MANIFEST_FILENAME, sync_plugin.CONFIG_FILES
        )
        self.assertIn("ai_workflow_preflight.py", sync_plugin.RUNTIME_FILES)


def _jsonl(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _pipeline_result(role: str, status: str, state: str) -> dict[str, object]:
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


class PreflightProductionWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repo = _init_repo(root / "repository")
        (self.repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
        self.state_root = root / "state"
        self.store = workflow.WorkflowStore(self.state_root)
        self.task = _valid_task(task_id=TASK_ID, repository_root=self.repo)
        self.store.create_task(self.task)
        legacy_config = workflow._load_workflow_config()
        legacy_config["routing"] = {"mode": "legacy", "role_policy": "legacy"}
        self._legacy = mock.patch.object(
            workflow, "_load_workflow_config", return_value=legacy_config
        )
        self._legacy.start()

    def tearDown(self) -> None:
        self._legacy.stop()
        self.temporary.cleanup()

    def _preflight_roles(self) -> list[str]:
        return [str(row["role"]) for row in _jsonl(self._ledger_path())]

    def _ledger_path(self, name: str = preflight.PREFLIGHT_LEDGER) -> Path:
        return self.store._require_task(TASK_ID) / name

    def _permit_records(self) -> list[dict[str, object]]:
        return _jsonl(self._ledger_path(policy.DISPATCH_PERMIT_LEDGER))

    def _events(self) -> list[dict[str, object]]:
        return _jsonl(self.store._require_task(TASK_ID) / "events.jsonl")

    def _run_until_gate(self, runner: ScriptedRunner) -> str:
        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            return workflow.run_until_gate(
                TASK_ID,
                runner=runner,
                allow_live_model=False,
                state_root=self.state_root,
            )

    def test_require_dispatch_permit_locked_does_not_run_preflight(self) -> None:
        source = inspect.getsource(policy.require_dispatch_permit_locked)
        self.assertNotIn("run_role_preflight", source)
        self.assertIn("require_role_preflighted_locked", source)

    def test_preflight_active_roles_is_self_locking_orchestration(self) -> None:
        self.assertTrue(hasattr(policy, "preflight_active_roles"))
        parameters = inspect.signature(policy.preflight_active_roles).parameters
        self.assertEqual(("store", "task_id", "roles"), tuple(parameters))
        for injector in INJECTOR_NAMES:
            self.assertNotIn(injector, parameters)
        source = inspect.getsource(policy.preflight_active_roles)
        self.assertIn("run_role_preflight(", source)
        self.assertNotIn("run_role_preflight_locked", source)

    def test_ensure_declaration_preflights_outside_lock_via_public_wrapper(self) -> None:
        source = inspect.getsource(workflow._ensure_task_declaration)
        self.assertIn("preflight_active_roles", source)
        self.assertNotIn("run_role_preflight_locked", source)

    def test_owner_escalation_preflights_new_role_outside_lock(self) -> None:
        source = inspect.getsource(workflow._apply_owner_decision)
        self.assertIn("run_role_preflight(", source)
        self.assertNotIn("run_role_preflight_locked", source)

    def test_until_gate_preflights_active_roles_before_first_reserved(self) -> None:
        state = self._run_until_gate(workflow.FakeRunner())
        self.assertEqual("AWAITING_OWNER_DECISION", state)
        declaration = declarations.load_route_declaration(self.store, TASK_ID)
        self.assertIsNotNone(declaration)
        assert declaration is not None
        preflighted = self._preflight_roles()
        self.assertEqual(list(declaration.active_roles), preflighted)
        inactive = set(declaration.allowed_roles) - set(declaration.active_roles)
        self.assertTrue(inactive)
        self.assertTrue(inactive.isdisjoint(set(preflighted)))
        permits = self._permit_records()
        self.assertTrue(permits)
        first_by_role: dict[str, dict[str, object]] = {}
        for row in permits:
            role = str(row["role"])
            if role not in first_by_role:
                first_by_role[role] = row
        for role in declaration.active_roles:
            self.assertIn(role, first_by_role)
            self.assertEqual("RESERVED", first_by_role[role]["state"])
            self.assertLess(
                preflighted.index(role),
                len(preflighted),
            )

    def test_until_gate_deleted_preflight_rejects_without_permit_growth(self) -> None:
        declaration = workflow._ensure_task_declaration(self.store, TASK_ID, self.task)
        self.assertEqual(list(declaration.active_roles), self._preflight_roles())
        ledger = self._ledger_path()
        self.assertTrue(ledger.is_file())
        ledger.unlink()
        before = self._permit_records()
        self.assertEqual([], before)
        runner = ScriptedRunner(
            [_pipeline_result("luna", "SUPPORTED", "EVIDENCE_READY")]
        )
        state = self._run_until_gate(runner)
        self.assertEqual([], runner.calls)
        self.assertEqual("BLOCKED", state)
        failures = [
            event for event in self._events() if event.get("event_type") == "ROLE_FAILURE"
        ]
        self.assertTrue(failures)
        self.assertEqual("ROLE_NOT_PREFLIGHTED", failures[-1]["error_code"])
        self.assertEqual([], self._permit_records())

    def test_illegal_activate_role_is_transition_blocked(self) -> None:
        workflow._ensure_task_declaration(self.store, TASK_ID, self.task)
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "ROUTE_TRANSITION_BLOCKED"
            ):
                policy.activate_role(
                    self.store, TASK_ID, from_role="luna", to_role="terra"
                )

    def test_preflight_cache_hit_still_verifies_runtime_identity(self) -> None:
        workflow._ensure_task_declaration(self.store, TASK_ID, self.task)
        self.assertIn("luna", self._preflight_roles())
        sessions = Path(self.temporary.name) / "runtime-sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        rollout = {
            "thread_id": THREAD_ID,
            "agent_type": None,
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "sandbox_policy": "read-only",
            "permission_profile": "read-only",
            "cwd": str(Path(self.repo).resolve()),
            "prompt": "PROMPT_SECRET",
            "environment": "ENV_SECRET",
            "token": "TOKEN_SECRET",
        }
        (sessions / f"rollout-{THREAD_ID}").write_text(
            json.dumps(rollout), encoding="utf-8"
        )
        paths = workflow.RunPaths(
            repo=Path(self.repo).resolve(),
            output_path=self.store._require_task(TASK_ID) / "luna-result.json",
            schema_path=ROOT / "config" / "ai_workflow_result.schema.json",
            logs_dir=self.store._require_task(TASK_ID) / "logs",
            state_root=self.state_root,
            runtime_evidence_required=True,
            runtime_sessions_dir=sessions,
        )

        def write_result(command, *args, **kwargs):
            attempt_output = Path(command[command.index("-o") + 1])
            attempt_output.write_text(
                json.dumps(blocked_luna_result()), encoding="utf-8"
            )
            events = "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": THREAD_ID}),
                    json.dumps({"type": "turn.completed"}),
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=events + "\n", stderr="")

        with (
            mock.patch.object(
                workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())
            ),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow.subprocess, "Popen", _compat_popen(write_result)),
            mock.patch.object(
                workflow,
                "verify_runtime_identity",
                wraps=workflow.verify_runtime_identity,
            ) as identity,
        ):
            workflow.run_codex("luna", self.task, "Read only.", paths)
        self.assertEqual(1, identity.call_count)
        ledger = self._ledger_path()
        ledger.unlink()
        with (
            mock.patch.object(
                workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())
            ),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(
                workflow.subprocess,
                "Popen",
                _compat_popen(
                    lambda *args, **kwargs: (_ for _ in ()).throw(
                        AssertionError("codex launched")
                    )
                ),
            ),
            mock.patch.object(
                workflow,
                "verify_runtime_identity",
                wraps=workflow.verify_runtime_identity,
            ) as identity_missing,
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROLE_NOT_PREFLIGHTED"):
                workflow.run_codex("luna", self.task, "Read only.", paths)
        self.assertEqual(0, identity_missing.call_count)


if __name__ == "__main__":
    unittest.main()
