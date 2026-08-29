"""Dispatch-permit state machine: single-transaction locked primitives."""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_declarations as declarations
from scripts import ai_workflow_dispatch_policy as policy
from scripts import ai_workflow_ownership as ownership
from scripts import ai_workflow_preflight as preflight
from scripts.ai_workflow_routing import RuntimeRouteDecision


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE_PATH = SCRIPTS / "ai_workflow_dispatch_policy.py"
TASK_ID = "AWF-20260803-001"
OTHER_TASK_ID = "AWF-20260803-002"
NEW_BUSINESS_MODULES = (
    "ai_workflow_dispatch_policy",
    "ai_workflow_ownership",
    "ai_workflow_authorizations",
    "ai_workflow_declarations",
    "ai_workflow_preflight",
    "ai_workflow_side_effects",
)
LOCKED_PRIMITIVES = (
    "require_dispatch_permit_locked",
    "precheck_dispatch_permit_locked",
    "release_permit_before_start_locked",
    "claim_permit_start_locked",
)
SELF_LOCK_WRAPPERS = (
    "require_dispatch_permit",
    "precheck_dispatch_permit",
    "release_permit_before_start",
)
READ_ONLY_CONFIG: dict[str, object] = {
    "roles": {"luna": {"sandbox": "read-only"}, "sol_planner": {"sandbox": "read-only"}}
}
EFFECTFUL_CONFIG: dict[str, object] = {
    "roles": {
        "luna": {"sandbox": "read-only"},
        "terra": {"sandbox": "workspace-write"},
    }
}


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
    _run_git(path, "config", "user.email", "dispatch-policy@example.test")
    _run_git(path, "config", "user.name", "Dispatch Policy Test")
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
    allowed_roles: tuple[str, ...] = ("luna", "sol_planner"),
    active_roles: tuple[str, ...] = ("luna",),
    max_dispatches: int = 2,
    allowed_transitions: tuple[dict[str, str], ...] | None = None,
) -> declarations.RouteDeclaration:
    transitions = allowed_transitions
    if transitions is None:
        transitions = ({"from_role": "luna", "to_role": "sol_planner"},)
    return declarations.build_route_declaration(
        decision=decision,
        route_config_hash="b" * 64,
        allowed_roles=allowed_roles,
        active_roles=active_roles,
        rule_ids=(decision.rule_id,),
        reason_codes=("PROMPT_SUFFICIENT",),
        max_dispatches=max_dispatches,
        allowed_transitions=transitions,
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
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _skip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        return body[1:]
    return body


def _is_lock_wrapper(func: ast.FunctionDef) -> bool:
    body = _skip_docstring(func.body)
    if len(body) != 1 or not isinstance(body[0], ast.With):
        return False
    item = body[0].items[0].context_expr if body[0].items else None
    if not isinstance(item, ast.Call):
        return False
    if not isinstance(item.func, ast.Attribute) or item.func.attr != "lock":
        return False
    return True


def _module_functions(path: Path) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _called_names(func: ast.FunctionDef) -> tuple[str, ...]:
    names: list[str] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name is not None:
                names.append(name)
    return tuple(names)


class _DispatchStoreMixin:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repo = _init_repo(root / "repository")
        (self.repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
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
        self.task_sha256 = artifacts.artifact_sha256(self.task)
        self.config = dict(READ_ONLY_CONFIG)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _identity(self, attempt_id: str = "attempt-1", *, role: str = "luna") -> str:
        return policy.derive_dispatch_identity(
            task_sha256=self.task_sha256, role=role, attempt_id=attempt_id
        )

    def _preflight(self, role: str = "luna") -> None:
        preflight.run_role_preflight(self.store, TASK_ID, role)

    def _require(
        self,
        *,
        role: str = "luna",
        attempt_id: str = "attempt-1",
        config: dict[str, object] | None = None,
    ) -> policy.DispatchPermit:
        with self.store.lock(TASK_ID):
            return policy.require_dispatch_permit_locked(
                self.store,
                TASK_ID,
                role,
                dispatch_identity=self._identity(attempt_id, role=role),
                config=config if config is not None else self.config,
            )

    def _ledger_path(self) -> Path:
        return self.store._require_task(TASK_ID) / policy.DISPATCH_PERMIT_LEDGER

    def _ledger_records(self) -> list[dict[str, object]]:
        path = self._ledger_path()
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def _task_file_bytes(self) -> dict[str, bytes]:
        task_dir = self.store._require_task(TASK_ID)
        return {
            path.name: path.read_bytes()
            for path in task_dir.iterdir()
            if path.is_file()
        }

    def _record_violation(self) -> None:
        event = {
            "event_type": ownership.OWNERSHIP_VIOLATION_EVENT_TYPE,
            "task_id": TASK_ID,
            "envelope_hash": self.task_sha256,
            "permit_id": "permit-violation",
            "role": "luna",
            "paths": ["src/a.py"],
            "timestamp_utc": "2026-08-28T12:00:00Z",
        }
        self.assertEqual(ownership.OWNERSHIP_VIOLATION_EVENT_FIELDS, set(event))
        self.store.append_event(TASK_ID, event)

    def _write_permits(self, records: list[dict[str, object]]) -> None:
        path = self._ledger_path()
        text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records)
        path.write_text(text, encoding="utf-8")

    def _permit_record(
        self,
        *,
        seq: int,
        permit_id: str,
        state: str,
        role: str = "luna",
        reason: str = "",
        task_id: str = TASK_ID,
    ) -> dict[str, object]:
        return {
            "schema_version": policy.DISPATCH_PERMIT_SCHEMA_VERSION,
            "seq": seq,
            "permit_id": permit_id,
            "task_id": task_id,
            "role": role,
            "state": state,
            "reason": reason,
            "recorded_at_utc": "2026-08-28T00:00:00Z",
        }


class DispatchPermitGateTest(_DispatchStoreMixin, unittest.TestCase):
    def test_missing_declaration_is_rejected(self) -> None:
        path = self.store._require_task(TASK_ID) / "route-declaration.json"
        path.unlink()
        events = self.store._require_task(TASK_ID) / "events.jsonl"
        if events.is_file():
            events.unlink()
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "ROUTE_DECLARATION_MISSING"
        ):
            self._require()

    def test_tampered_envelope_is_mismatch(self) -> None:
        self._preflight()
        task_path = self.store._require_task(TASK_ID) / "task.json"
        mutated = json.loads(task_path.read_text(encoding="utf-8"))
        mutated["objective"] = "tampered objective"
        task_path.write_text(json.dumps(mutated) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "ROUTE_DECLARATION_MISMATCH"
        ):
            self._require()

    def test_persistent_violation_blocks_all_later_permits(self) -> None:
        self._preflight()
        permit = self._require(attempt_id="first")
        with self.store.lock(TASK_ID):
            policy.claim_permit_start_locked(self.store, TASK_ID, permit)
        self._record_violation()
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_BLOCKED_OWNERSHIP_VIOLATION"
        ):
            self._require(attempt_id="second")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_BLOCKED_OWNERSHIP_VIOLATION"
        ):
            policy.require_dispatch_permit(
                self.store,
                TASK_ID,
                "luna",
                dispatch_identity=self._identity("third"),
                config=self.config,
            )

    def test_role_not_in_allowed_roles_is_rejected(self) -> None:
        self._preflight()
        with self.assertRaisesRegex(artifacts.WorkflowError, "ROLE_NOT_ALLOWED"):
            self._require(role="terra", attempt_id="terra-1")

    def test_inactive_role_is_transition_blocked(self) -> None:
        self._preflight("sol_planner")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "ROUTE_TRANSITION_BLOCKED"
        ):
            self._require(role="sol_planner", attempt_id="planner-1")

    def test_unpreflighted_role_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "ROLE_NOT_PREFLIGHTED"
        ):
            self._require()

    def test_budget_full_is_rejected(self) -> None:
        self.declaration = _build_declaration(self.decision, max_dispatches=1)
        with self.store.lock(TASK_ID):
            existing = self.store._require_task(TASK_ID) / "route-declaration.json"
            existing.unlink()
            events = self.store._require_task(TASK_ID) / "events.jsonl"
            if events.is_file():
                events.unlink()
            declarations.record_route_declaration(
                self.store, TASK_ID, self.declaration
            )
        self._preflight()
        self._require(attempt_id="only")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "ROUTE_BUDGET_EXCEEDED"
        ):
            self._require(attempt_id="overflow")

    def test_legal_path_appends_reserved_with_continuous_seq(self) -> None:
        self._preflight()
        first = self._require(attempt_id="one")
        self.assertEqual(self._identity("one"), first.permit_id)
        self.assertEqual(TASK_ID, first.task_id)
        self.assertEqual("luna", first.role)
        self.assertEqual(1, first.reservation_seq)
        second = self._require(attempt_id="two")
        self.assertEqual(2, second.reservation_seq)
        records = self._ledger_records()
        self.assertEqual(["RESERVED", "RESERVED"], [row["state"] for row in records])
        self.assertEqual([1, 2], [row["seq"] for row in records])
        self.assertEqual(policy.DISPATCH_PERMIT_FIELDS, set(records[0]))
        self.assertEqual("", records[0]["reason"])
        self.assertEqual(policy.DISPATCH_PERMIT_SCHEMA_VERSION, records[0]["schema_version"])


class DispatchPermitStateMachineTest(_DispatchStoreMixin, unittest.TestCase):
    def test_claimed_identity_cannot_reenter(self) -> None:
        self._preflight()
        permit = self._require(attempt_id="same")
        with self.store.lock(TASK_ID):
            policy.claim_permit_start_locked(self.store, TASK_ID, permit)
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "DISPATCH_PERMIT_ALREADY_STARTED"
            ):
                policy.require_dispatch_permit_locked(
                    self.store,
                    TASK_ID,
                    "luna",
                    dispatch_identity=permit.permit_id,
                    config=self.config,
                )

    def test_released_identity_is_retired_serially(self) -> None:
        self._preflight()
        permit = self._require(attempt_id="retire")
        with self.store.lock(TASK_ID):
            policy.release_permit_before_start_locked(
                self.store, TASK_ID, permit, reason="spawn-failed"
            )
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_IDENTITY_RETIRED"
        ):
            self._require(attempt_id="retire")

    def test_released_identity_is_retired_concurrently(self) -> None:
        self._preflight()
        permit = self._require(attempt_id="race")
        with self.store.lock(TASK_ID):
            policy.release_permit_before_start_locked(
                self.store, TASK_ID, permit, reason="spawn-failed"
            )
        codes: list[str] = []
        barrier = threading.Barrier(2)

        def _worker() -> None:
            barrier.wait()
            while True:
                try:
                    policy.require_dispatch_permit(
                        self.store,
                        TASK_ID,
                        "luna",
                        dispatch_identity=permit.permit_id,
                        config=self.config,
                    )
                    codes.append("ok")
                    return
                except artifacts.WorkflowError as exc:
                    if exc.code == "TASK_ALREADY_RUNNING":
                        continue
                    codes.append(exc.code)
                    return

        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(["DISPATCH_IDENTITY_RETIRED", "DISPATCH_IDENTITY_RETIRED"], sorted(codes))

    def test_unclaimed_reserved_identity_cannot_reenter(self) -> None:
        self._preflight()
        permit = self._require(attempt_id="orphan")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_PERMIT_UNCLAIMED"
        ):
            self._require(attempt_id="orphan")
        self.assertEqual("RESERVED", policy.permit_latest_states(
            policy.replay_permit_ledger(self.store, TASK_ID)
        )[permit.permit_id])

    def test_started_permit_is_never_released(self) -> None:
        self._preflight()
        permit = self._require(attempt_id="started")
        with self.store.lock(TASK_ID):
            policy.claim_permit_start_locked(self.store, TASK_ID, permit)
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "DISPATCH_PERMIT_STATE_ILLEGAL"
            ):
                policy.release_permit_before_start_locked(
                    self.store, TASK_ID, permit, reason="too-late"
                )

    def test_release_of_already_released_is_retired(self) -> None:
        self._preflight()
        permit = self._require(attempt_id="twice")
        with self.store.lock(TASK_ID):
            policy.release_permit_before_start_locked(
                self.store, TASK_ID, permit, reason="first"
            )
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "DISPATCH_IDENTITY_RETIRED"
            ):
                policy.release_permit_before_start_locked(
                    self.store, TASK_ID, permit, reason="second"
                )

    def test_claim_of_non_reserved_is_illegal(self) -> None:
        self._preflight()
        permit = self._require(attempt_id="claim-twice")
        with self.store.lock(TASK_ID):
            policy.claim_permit_start_locked(self.store, TASK_ID, permit)
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "DISPATCH_PERMIT_STATE_ILLEGAL"
            ):
                policy.claim_permit_start_locked(self.store, TASK_ID, permit)
            released = policy.require_dispatch_permit_locked(
                self.store,
                TASK_ID,
                "luna",
                dispatch_identity=self._identity("to-release"),
                config=self.config,
            )
            policy.release_permit_before_start_locked(
                self.store, TASK_ID, released, reason="before-claim"
            )
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "DISPATCH_PERMIT_STATE_ILLEGAL"
            ):
                policy.claim_permit_start_locked(self.store, TASK_ID, released)

    def test_new_attempt_after_release_is_authorized(self) -> None:
        self._preflight()
        first = self._require(attempt_id="old")
        with self.store.lock(TASK_ID):
            policy.release_permit_before_start_locked(
                self.store, TASK_ID, first, reason="retry"
            )
        second = self._require(attempt_id="new")
        self.assertNotEqual(first.permit_id, second.permit_id)
        self.assertEqual("RESERVED", self._ledger_records()[-1]["state"])
        released = [
            row for row in self._ledger_records() if row["state"] == "RELEASED_BEFORE_START"
        ]
        self.assertEqual("retry", released[0]["reason"])


class DispatchPermitLockDisciplineTest(_DispatchStoreMixin, unittest.TestCase):
    def test_locked_variants_require_held_lock(self) -> None:
        permit = policy.DispatchPermit(
            permit_id=self._identity("lock"),
            task_id=TASK_ID,
            role="luna",
            reservation_seq=1,
        )
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            policy.require_dispatch_permit_locked(
                self.store,
                TASK_ID,
                "luna",
                dispatch_identity=permit.permit_id,
                config=self.config,
            )
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            policy.precheck_dispatch_permit_locked(
                self.store, TASK_ID, "luna", config=self.config
            )
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            policy.release_permit_before_start_locked(
                self.store, TASK_ID, permit, reason="x"
            )
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            policy.claim_permit_start_locked(self.store, TASK_ID, permit)

    def test_self_lock_wrappers_fail_when_lock_already_held(self) -> None:
        self._preflight()
        permit = self._require(attempt_id="held")
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "TASK_ALREADY_RUNNING"
            ):
                policy.require_dispatch_permit(
                    self.store,
                    TASK_ID,
                    "luna",
                    dispatch_identity=self._identity("nested"),
                    config=self.config,
                )
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "TASK_ALREADY_RUNNING"
            ):
                policy.precheck_dispatch_permit(
                    self.store, TASK_ID, "luna", config=self.config
                )
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "TASK_ALREADY_RUNNING"
            ):
                policy.release_permit_before_start(
                    self.store, permit, reason="nested"
                )

    def test_wrappers_only_take_lock_and_delegate(self) -> None:
        for name in SELF_LOCK_WRAPPERS:
            function = getattr(policy, name)
            stmts = _wrapper_statements(function)
            self.assertEqual(1, len(stmts), name)
            self.assertIsInstance(stmts[0], ast.With)
            source = inspect.getsource(function)
            self.assertIn("store.lock(", source)
            self.assertIn(f"{name}_locked", source)

    def test_claim_has_no_unlocked_wrapper(self) -> None:
        self.assertFalse(hasattr(policy, "claim_permit_start"))

    def test_locked_require_calls_only_locked_violation_query(self) -> None:
        source = inspect.getsource(policy.require_dispatch_permit_locked)
        self.assertIn("has_unresolved_ownership_violation_locked(", source)
        self.assertNotIn("has_unresolved_ownership_violation(", source)

    def test_locked_primitives_assert_lock_first(self) -> None:
        for name in LOCKED_PRIMITIVES:
            self.assertEqual(
                "_assert_lock_held",
                _first_call_name(getattr(policy, name)),
                name,
            )

    def test_require_does_not_accept_constructed_preflight_context(self) -> None:
        parameters = inspect.signature(policy.require_dispatch_permit_locked).parameters
        self.assertNotIn("context", parameters)
        self.assertNotIn("PreflightContext", inspect.getsource(policy.require_dispatch_permit_locked))

    def test_release_guard_helper_three_states(self) -> None:
        permit = policy.DispatchPermit(
            permit_id=self._identity("guard"),
            task_id=TASK_ID,
            role="luna",
            reservation_seq=1,
        )
        with mock.patch.object(policy, "release_permit_before_start") as spy:
            policy.release_permit_if_never_spawned(
                self.store, permit, spawned=True, reason="spawned"
            )
            spy.assert_not_called()
            policy.release_permit_if_never_spawned(
                self.store, permit, spawned=False, reason="not-spawned"
            )
            spy.assert_called_once_with(self.store, permit, reason="not-spawned")
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "TASK_ALREADY_RUNNING"
            ):
                policy.release_permit_if_never_spawned(
                    self.store, permit, spawned=False, reason="held"
                )

    def test_release_wrapper_has_single_direct_call_site(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
        callers: list[str] = []

        def _enclosing(target: ast.AST) -> str | None:
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for child in ast.walk(node):
                    if child is target:
                        return node.name
            return None

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node) == "release_permit_before_start":
                owner = _enclosing(node)
                if owner is not None:
                    callers.append(owner)
        self.assertEqual(["release_permit_if_never_spawned"], callers)

    def test_transitive_call_graph_excludes_self_lock_wrappers(self) -> None:
        functions: dict[str, ast.FunctionDef] = {}
        wrappers: set[str] = set()
        for name in NEW_BUSINESS_MODULES:
            path = SCRIPTS / f"{name}.py"
            defs = _module_functions(path)
            for func_name, func in defs.items():
                functions[func_name] = func
                if _is_lock_wrapper(func):
                    wrappers.add(func_name)
        self.assertIn("require_dispatch_permit", wrappers)
        self.assertIn("has_unresolved_ownership_violation", wrappers)
        reachable: set[str] = set()
        stack = ["require_dispatch_permit_locked", "require_write_ownership_locked"]
        while stack:
            current = stack.pop()
            if current in reachable or current not in functions:
                continue
            reachable.add(current)
            for called in _called_names(functions[current]):
                if called in functions and called not in reachable:
                    stack.append(called)
        overlap = reachable & wrappers
        self.assertFalse(overlap, f"locked path reaches wrappers: {sorted(overlap)}")


class DispatchPermitConcurrencyAndExternalTest(_DispatchStoreMixin, unittest.TestCase):
    def test_concurrent_requires_respect_budget(self) -> None:
        self._preflight()
        results: list[str] = []
        barrier = threading.Barrier(5)

        def _worker(index: int) -> None:
            barrier.wait()
            while True:
                try:
                    policy.require_dispatch_permit(
                        self.store,
                        TASK_ID,
                        "luna",
                        dispatch_identity=self._identity(f"c{index}"),
                        config=self.config,
                    )
                    results.append("ok")
                    return
                except artifacts.WorkflowError as exc:
                    if exc.code == "TASK_ALREADY_RUNNING":
                        continue
                    results.append(exc.code)
                    return

        threads = [threading.Thread(target=_worker, args=(index,)) for index in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(2, results.count("ok"))
        self.assertEqual(3, results.count("ROUTE_BUDGET_EXCEEDED"))
        self.assertEqual(2, len(self._ledger_records()))

    def test_effectful_role_records_external_with_permit_id(self) -> None:
        self.declaration = _build_declaration(
            self.decision,
            allowed_roles=("luna", "terra"),
            active_roles=("luna", "terra"),
            allowed_transitions=(),
        )
        with self.store.lock(TASK_ID):
            (self.store._require_task(TASK_ID) / "route-declaration.json").unlink()
            events = self.store._require_task(TASK_ID) / "events.jsonl"
            if events.is_file():
                events.unlink()
            declarations.record_route_declaration(
                self.store, TASK_ID, self.declaration
            )
        self._preflight("terra")
        permit = self._require(
            role="terra", attempt_id="ext", config=EFFECTFUL_CONFIG
        )
        rows = [
            row
            for row in ownership.load_side_effects(self.store, TASK_ID)
            if row.get("effect_kind") == "EXTERNAL"
        ]
        self.assertEqual(1, len(rows))
        self.assertEqual(permit.permit_id, rows[0]["permit_id"])
        self.assertEqual("terra", rows[0]["role"])

    def test_read_only_role_does_not_record_external(self) -> None:
        self._preflight()
        self._require(attempt_id="ro")
        rows = ownership.load_side_effects(self.store, TASK_ID)
        self.assertFalse(any(row.get("effect_kind") == "EXTERNAL" for row in rows))


class DispatchPermitOrderActivationReplayTest(_DispatchStoreMixin, unittest.TestCase):
    def test_declaration_precedes_reserved_without_utc_comparison(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MODULE_PATH))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            compared = [node.left, *node.comparators]
            for item in compared:
                if isinstance(item, ast.Name) and item.id == "declared_at_utc":
                    self.fail("declared_at_utc must not be compared")
                if isinstance(item, ast.Attribute) and item.attr == "declared_at_utc":
                    self.fail("declared_at_utc must not be compared")
        self._preflight()
        self._require(attempt_id="order")
        events = [
            json.loads(line)
            for line in (self.store._require_task(TASK_ID) / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        declared_index = next(
            index
            for index, event in enumerate(events)
            if event.get("event_type") == "ROUTE_DECLARED"
        )
        self.assertEqual(0, declared_index)
        self.assertEqual("RESERVED", self._ledger_records()[0]["state"])

    def test_activate_role_validates_transition_graph(self) -> None:
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "ROUTE_TRANSITION_BLOCKED"
            ):
                policy.activate_role(
                    self.store, TASK_ID, from_role="luna", to_role="terra"
                )
            policy.activate_role(
                self.store, TASK_ID, from_role="luna", to_role="sol_planner"
            )
            active = policy.derive_active_roles(
                self.store, TASK_ID, self.declaration
            )
        self.assertIn("sol_planner", active)
        self.assertIn("luna", active)

    def test_previous_dispatch_role_is_not_activation_state(self) -> None:
        self._preflight()
        self._require(attempt_id="luna-first")
        self._preflight("sol_planner")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "ROUTE_TRANSITION_BLOCKED"
        ):
            self._require(role="sol_planner", attempt_id="planner")

    def test_replay_rejects_truncated_duplicate_gap_and_illegal_transitions(self) -> None:
        identity = self._identity("corrupt")
        valid = self._permit_record(seq=1, permit_id=identity, state="RESERVED")
        self._write_permits([valid])
        path = self._ledger_path()
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_PERMIT_LEDGER_CORRUPT"
        ):
            policy.replay_permit_ledger(self.store, TASK_ID)

        duplicate = [
            self._permit_record(seq=1, permit_id=identity, state="RESERVED"),
            self._permit_record(seq=1, permit_id=self._identity("other"), state="RESERVED"),
        ]
        self._write_permits(duplicate)
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_PERMIT_LEDGER_CORRUPT"
        ):
            policy.replay_permit_ledger(self.store, TASK_ID)

        gap = [
            self._permit_record(seq=1, permit_id=identity, state="RESERVED"),
            self._permit_record(seq=3, permit_id=self._identity("gap"), state="RESERVED"),
        ]
        self._write_permits(gap)
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_PERMIT_LEDGER_CORRUPT"
        ):
            policy.replay_permit_ledger(self.store, TASK_ID)

        two_reserved = [
            self._permit_record(seq=1, permit_id=identity, state="RESERVED"),
            self._permit_record(seq=2, permit_id=identity, state="RESERVED"),
        ]
        self._write_permits(two_reserved)
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_PERMIT_LEDGER_CORRUPT"
        ):
            policy.replay_permit_ledger(self.store, TASK_ID)

        two_released = [
            self._permit_record(seq=1, permit_id=identity, state="RESERVED"),
            self._permit_record(
                seq=2,
                permit_id=identity,
                state="RELEASED_BEFORE_START",
                reason="first",
            ),
            self._permit_record(
                seq=3,
                permit_id=identity,
                state="RELEASED_BEFORE_START",
                reason="second",
            ),
        ]
        self._write_permits(two_released)
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_PERMIT_LEDGER_CORRUPT"
        ):
            policy.replay_permit_ledger(self.store, TASK_ID)

        after_started = [
            self._permit_record(seq=1, permit_id=identity, state="RESERVED"),
            self._permit_record(seq=2, permit_id=identity, state="STARTED"),
            self._permit_record(seq=3, permit_id=identity, state="STARTED"),
        ]
        self._write_permits(after_started)
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_PERMIT_LEDGER_CORRUPT"
        ):
            policy.replay_permit_ledger(self.store, TASK_ID)

        crossed = [
            self._permit_record(
                seq=1, permit_id=identity, state="RESERVED", task_id=OTHER_TASK_ID
            )
        ]
        self._write_permits(crossed)
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_PERMIT_LEDGER_CORRUPT"
        ):
            policy.replay_permit_ledger(self.store, TASK_ID)

        self._write_permits(
            [self._permit_record(seq=1, permit_id=identity, state="RESERVED")]
        )
        path = self._ledger_path()
        path.write_bytes(path.read_bytes() + b"[]\n")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "DISPATCH_PERMIT_LEDGER_CORRUPT"
        ):
            policy.replay_permit_ledger(self.store, TASK_ID)

    def test_precheck_is_read_only_and_fails_closed_for_unauthorized_role(self) -> None:
        self._preflight()
        before = self._task_file_bytes()
        with self.store.lock(TASK_ID):
            policy.precheck_dispatch_permit_locked(
                self.store, TASK_ID, "luna", config=self.config
            )
        self.assertEqual(before, self._task_file_bytes())
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(artifacts.WorkflowError, "ROLE_NOT_ALLOWED"):
                policy.precheck_dispatch_permit_locked(
                    self.store, TASK_ID, "terra", config=self.config
                )
        self.assertEqual(before, self._task_file_bytes())

    def test_ensure_declaration_for_task_is_unique_create_stage(self) -> None:
        nested = tempfile.TemporaryDirectory()
        try:
            repo = _init_repo(Path(nested.name) / "repository")
            (repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
            store = workflow.WorkflowStore(Path(nested.name) / "state")
            task = _valid_task(task_id=TASK_ID, repository_root=repo)
            store.create_task(task)
            decision = _runtime_decision(task)
            store.write_task_artifact_once(
                TASK_ID,
                "route-decision.json",
                decision.to_dict(),
                conflict_code="ROUTE_ALREADY_FROZEN",
            )
            config = {"policy": {"max_technical_retries": 1}, "reason_codes": ["PROMPT_SUFFICIENT"]}
            with store.lock(TASK_ID):
                first = policy.ensure_declaration_for_task(
                    store, TASK_ID, decision=decision, config=config
                )
                second = policy.ensure_declaration_for_task(
                    store, TASK_ID, decision=decision, config=config
                )
            self.assertEqual(first.to_dict(), second.to_dict())
            self.assertEqual(decision.task_sha256, first.envelope_hash)
            self.assertEqual(["luna"], list(first.active_roles))
        finally:
            nested.cleanup()

    def test_identity_formulas_are_distinct_and_stable(self) -> None:
        left = policy.derive_dispatch_identity(
            task_sha256="a" * 64, role="luna", attempt_id="1"
        )
        right = policy.derive_dispatch_identity(
            task_sha256="a" * 64, role="luna", attempt_id="1"
        )
        other = policy.derive_dispatch_identity(
            task_sha256="a" * 64, role="luna", attempt_id="2"
        )
        assignment = policy.derive_assignment_dispatch_identity(
            task_sha256="a" * 64, assignment_id="asg-1", attempt_id="1"
        )
        self.assertEqual(left, right)
        self.assertNotEqual(left, other)
        self.assertNotEqual(left, assignment)
        self.assertRegex(left, r"^[0-9a-f]{64}$")
        self.assertRegex(assignment, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
