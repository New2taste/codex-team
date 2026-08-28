"""Route-declaration sidecar: schema, unique-create, and crash recovery."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_declarations as declarations
from scripts.ai_workflow_routing import RuntimeRouteDecision


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
MODULE_PATH = SCRIPTS / "ai_workflow_declarations.py"
DECLARATION_FILENAME = "route-declaration.json"


def _valid_task(*, task_id: str = "AWF-20260803-001") -> dict[str, object]:
    return {
        "schema_version": "ai-task-1",
        "task_id": task_id,
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


def _runtime_decision(
    task: dict[str, object],
    *,
    route: str = "direct",
    rule_id: str = "PROMPT_SUFFICIENT_ROUTE",
    task_sha256: str | None = None,
    decided_at_utc: str = "2026-08-03T00:00:00Z",
) -> RuntimeRouteDecision:
    wire = artifacts.RouteDecision(
        task_id=str(task["task_id"]),
        route=route,
        rule_id=rule_id,
        task_sha256=task_sha256 or artifacts.artifact_sha256(task),
        request_sha256="a" * 64,
        decided_at_utc=decided_at_utc,
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
    **overrides: object,
) -> declarations.RouteDeclaration:
    kwargs: dict[str, object] = {
        "decision": decision,
        "route_config_hash": "b" * 64,
        "allowed_roles": ("luna", "sol_planner"),
        "active_roles": ("luna",),
        "rule_ids": (decision.rule_id,),
        "reason_codes": ("PROMPT_SUFFICIENT",),
        "max_dispatches": 2,
        "allowed_transitions": ({"from_role": "luna", "to_role": "sol_planner"},),
    }
    kwargs.update(overrides)
    return declarations.build_route_declaration(**kwargs)


def _function_nodes(tree: ast.AST) -> dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _contains_declaration_path(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Constant) and child.value == DECLARATION_FILENAME
        for child in ast.walk(node)
    )


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for child in ast.walk(node):
            if child is target:
                return node.name
    return None


class RouteDeclarationSchemaTest(unittest.TestCase):
    def test_valid_declaration_round_trips(self):
        task = _valid_task()
        decision = _runtime_decision(task)
        declaration = _build_declaration(decision)
        payload = declaration.to_dict()
        declarations.validate_route_declaration(payload)
        self.assertEqual(declarations.ROUTE_DECLARATION_FIELDS, set(payload))
        self.assertEqual("ai-route-declaration-1", payload["schema_version"])
        self.assertEqual("deterministic-router-1", payload["router_version"])
        self.assertEqual(decision.task_sha256, payload["envelope_hash"])
        self.assertEqual(decision.decided_at_utc, payload["declared_at_utc"])
        self.assertEqual(decision.route, payload["selected_route"])
        self.assertEqual(["luna", "sol_planner"], payload["allowed_roles"])
        self.assertEqual(["luna"], payload["active_roles"])

    def test_missing_field_is_rejected(self):
        payload = _build_declaration(_runtime_decision(_valid_task())).to_dict()
        del payload["selected_route"]
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            declarations.validate_route_declaration(payload)

    def test_unknown_field_is_rejected(self):
        payload = _build_declaration(_runtime_decision(_valid_task())).to_dict()
        payload["surprise"] = True
        with self.assertRaisesRegex(artifacts.WorkflowError, "UNKNOWN_FIELD"):
            declarations.validate_route_declaration(payload)

    def test_wrong_schema_version_is_rejected(self):
        payload = _build_declaration(_runtime_decision(_valid_task())).to_dict()
        payload["schema_version"] = "ai-route-declaration-0"
        with self.assertRaisesRegex(artifacts.WorkflowError, "SCHEMA_VERSION"):
            declarations.validate_route_declaration(payload)

    def test_max_dispatches_rejects_negative_non_integer_and_bool(self):
        task = _valid_task()
        decision = _runtime_decision(task)
        payload = _build_declaration(decision).to_dict()
        for value in (-1, 1.5, True, False, "1"):
            mutated = dict(payload)
            mutated["max_dispatches"] = value
            with self.subTest(value=value), self.assertRaisesRegex(
                artifacts.WorkflowError, "INVALID_TYPE"
            ):
                declarations.validate_route_declaration(mutated)

    def test_allowed_roles_rejects_unknown_role_or_empty_list(self):
        decision = _runtime_decision(_valid_task())
        payload = _build_declaration(decision).to_dict()
        unknown = dict(payload)
        unknown["allowed_roles"] = ["not_a_role"]
        unknown["active_roles"] = []
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_ENUM"):
            declarations.validate_route_declaration(unknown)
        empty = dict(payload)
        empty["allowed_roles"] = []
        empty["active_roles"] = []
        with self.assertRaisesRegex(artifacts.WorkflowError, "EMPTY_ARRAY"):
            declarations.validate_route_declaration(empty)

    def test_active_roles_must_be_subset_of_allowed_roles(self):
        decision = _runtime_decision(_valid_task())
        payload = _build_declaration(decision).to_dict()
        payload["active_roles"] = ["sol_reviewer"]
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_ENUM"):
            declarations.validate_route_declaration(payload)

    def test_allowed_transitions_require_closed_role_pair(self):
        decision = _runtime_decision(_valid_task())
        payload = _build_declaration(decision).to_dict()
        missing = dict(payload)
        missing["allowed_transitions"] = [{"from_role": "luna"}]
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            declarations.validate_route_declaration(missing)
        unknown_role = dict(payload)
        unknown_role["allowed_transitions"] = [
            {"from_role": "luna", "to_role": "not_a_role"}
        ]
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_ENUM"):
            declarations.validate_route_declaration(unknown_role)

    def test_build_envelope_hash_follows_decision_and_has_no_caller_param(self):
        task = _valid_task()
        first = _runtime_decision(task, task_sha256="c" * 64)
        second = _runtime_decision(task, task_sha256="d" * 64)
        self.assertEqual("c" * 64, _build_declaration(first).envelope_hash)
        self.assertEqual("d" * 64, _build_declaration(second).envelope_hash)
        parameters = inspect.signature(declarations.build_route_declaration).parameters
        self.assertNotIn("envelope_hash", parameters)
        self.assertNotIn("declared_at_utc", parameters)


class RouteDeclarationStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary.name))
        self.task = _valid_task()
        self.task_id = str(self.task["task_id"])
        self.store.create_task(self.task)
        self.decision = _runtime_decision(self.task)
        self.store.write_task_artifact_once(
            self.task_id,
            "route-decision.json",
            self.decision.to_dict(),
            conflict_code="ROUTE_ALREADY_FROZEN",
        )
        self.declaration = _build_declaration(self.decision)

    def tearDown(self):
        self.temporary.cleanup()

    def _task_dir(self) -> Path:
        return Path(self.temporary.name) / self.task_id

    def _events(self) -> list[dict[str, object]]:
        path = self._task_dir() / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_record_writes_declaration_file_and_declared_event(self):
        with self.store.lock(self.task_id):
            path = declarations.record_route_declaration(
                self.store, self.task_id, self.declaration
            )
        self.assertEqual(DECLARATION_FILENAME, path.name)
        raw = path.read_bytes()
        stored = json.loads(raw.decode("utf-8"))
        declarations.validate_route_declaration(stored)
        events = [event for event in self._events() if event.get("event_type") == "ROUTE_DECLARED"]
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual(self.task_id, event["task_id"])
        self.assertEqual(self.declaration.envelope_hash, event["envelope_hash"])
        self.assertEqual(self.declaration.selected_route, event["selected_route"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), event["declaration_sha256"])

    def test_tampered_task_envelope_is_mismatch(self):
        task_path = self._task_dir() / "task.json"
        mutated = json.loads(task_path.read_text(encoding="utf-8"))
        mutated["objective"] = "tampered objective"
        task_path.write_text(json.dumps(mutated), encoding="utf-8")
        with self.store.lock(self.task_id):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "ROUTE_DECLARATION_MISMATCH"
            ):
                declarations.record_route_declaration(
                    self.store, self.task_id, self.declaration
                )

    def test_missing_or_mismatched_route_decision_is_mismatch(self):
        (self._task_dir() / "route-decision.json").unlink()
        with self.store.lock(self.task_id):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "ROUTE_DECLARATION_MISMATCH"
            ):
                declarations.record_route_declaration(
                    self.store, self.task_id, self.declaration
                )
        mismatched = dict(self.decision.to_dict())
        mismatched["task_sha256"] = "e" * 64
        self.store.write_task_artifact_once(
            self.task_id,
            "route-decision.json",
            mismatched,
            conflict_code="ROUTE_ALREADY_FROZEN",
        )
        with self.store.lock(self.task_id):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "ROUTE_DECLARATION_MISMATCH"
            ):
                declarations.record_route_declaration(
                    self.store, self.task_id, self.declaration
                )

    def test_existing_dispatch_ledger_is_late(self):
        self.store.append_task_ledger(
            self.task_id, "dispatches.jsonl", {"dispatch_id": "late"}
        )
        with self.store.lock(self.task_id):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "ROUTE_DECLARATION_LATE"
            ):
                declarations.record_route_declaration(
                    self.store, self.task_id, self.declaration
                )

    def test_module_source_does_not_compare_declared_at_utc(self):
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

    def test_ensure_is_idempotent_and_rejects_drift(self):
        with self.store.lock(self.task_id):
            first = declarations.ensure_route_declaration(
                self.store, self.task_id, self.declaration
            )
            path = self._task_dir() / DECLARATION_FILENAME
            before = path.read_bytes()
            mtime = path.stat().st_mtime_ns
            rebuilt = _build_declaration(self.decision)
            second = declarations.ensure_route_declaration(
                self.store, self.task_id, rebuilt
            )
            self.assertEqual(first.to_dict(), second.to_dict())
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(mtime, path.stat().st_mtime_ns)
            drifted = _build_declaration(self.decision, max_dispatches=9)
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "ROUTE_DECLARATION_CONFLICT"
            ):
                declarations.ensure_route_declaration(self.store, self.task_id, drifted)

    def test_locked_helpers_require_held_lock(self):
        for function in (
            declarations.record_route_declaration,
            declarations.ensure_route_declaration,
            declarations.load_route_declaration_locked,
        ):
            with self.subTest(function=function.__name__), self.assertRaisesRegex(
                artifacts.WorkflowError, "LOCK_REQUIRED"
            ):
                if function is declarations.load_route_declaration_locked:
                    function(self.store, self.task_id)
                else:
                    function(self.store, self.task_id, self.declaration)

    def test_load_wrapper_returns_none_for_missing_task(self):
        self.assertIsNone(
            declarations.load_route_declaration(self.store, "AWF-20990101-001")
        )


class RouteDeclarationRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary.name))
        self.task = _valid_task()
        self.task_id = str(self.task["task_id"])
        self.store.create_task(self.task)
        self.decision = _runtime_decision(self.task)
        self.store.write_task_artifact_once(
            self.task_id,
            "route-decision.json",
            self.decision.to_dict(),
            conflict_code="ROUTE_ALREADY_FROZEN",
        )
        self.declaration = _build_declaration(self.decision)

    def tearDown(self):
        self.temporary.cleanup()

    def _task_dir(self) -> Path:
        return Path(self.temporary.name) / self.task_id

    def _declaration_path(self) -> Path:
        return self._task_dir() / DECLARATION_FILENAME

    def _write_declaration_file_only(self) -> bytes:
        path = self.store.write_task_artifact_once(
            self.task_id,
            DECLARATION_FILENAME,
            self.declaration.to_dict(),
            conflict_code="ROUTE_DECLARATION_CONFLICT",
        )
        return path.read_bytes()

    def _events(self) -> list[dict[str, object]]:
        path = self._task_dir() / "events.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def _declared_events(self) -> list[dict[str, object]]:
        return [
            event
            for event in self._events()
            if event.get("event_type") == "ROUTE_DECLARED"
        ]

    def test_load_wrapper_recovers_missing_event_without_rewriting_file(self):
        before = self._write_declaration_file_only()
        digest = hashlib.sha256(before).hexdigest()
        self.assertEqual([], self._declared_events())
        loaded = declarations.load_route_declaration(self.store, self.task_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(self.declaration.to_dict(), loaded.to_dict())
        after = self._declaration_path().read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(digest, hashlib.sha256(after).hexdigest())
        events = self._declared_events()
        self.assertEqual(1, len(events))
        self.assertEqual(digest, events[0]["declaration_sha256"])

    def test_load_locked_recovers_missing_event_without_rewriting_file(self):
        before = self._write_declaration_file_only()
        digest = hashlib.sha256(before).hexdigest()
        with self.store.lock(self.task_id):
            loaded = declarations.load_route_declaration_locked(self.store, self.task_id)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(self.declaration.to_dict(), loaded.to_dict())
        after = self._declaration_path().read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(digest, hashlib.sha256(after).hexdigest())
        self.assertEqual(1, len(self._declared_events()))

    def test_ensure_recovers_crash_window_then_returns_idempotently(self):
        before = self._write_declaration_file_only()
        with self.store.lock(self.task_id):
            loaded = declarations.ensure_route_declaration(
                self.store, self.task_id, self.declaration
            )
        self.assertEqual(self.declaration.to_dict(), loaded.to_dict())
        self.assertEqual(before, self._declaration_path().read_bytes())
        self.assertEqual(1, len(self._declared_events()))

    def test_event_without_file_is_corrupt_on_all_entries(self):
        with self.store.lock(self.task_id):
            declarations.record_route_declaration(
                self.store, self.task_id, self.declaration
            )
        self._declaration_path().unlink()
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "ROUTE_DECLARATION_CORRUPT"
        ):
            declarations.load_route_declaration(self.store, self.task_id)
        with self.store.lock(self.task_id):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "ROUTE_DECLARATION_CORRUPT"
            ):
                declarations.load_route_declaration_locked(self.store, self.task_id)
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "ROUTE_DECLARATION_CORRUPT"
            ):
                declarations.ensure_route_declaration(
                    self.store, self.task_id, self.declaration
                )

    def test_both_present_does_not_append_another_event(self):
        with self.store.lock(self.task_id):
            declarations.record_route_declaration(
                self.store, self.task_id, self.declaration
            )
        before = self._events()
        declarations.load_route_declaration(self.store, self.task_id)
        self.assertEqual(before, self._events())

    def test_recover_requires_held_lock(self):
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            declarations.recover_route_declaration_event(self.store, self.task_id)

    def test_append_event_failure_propagates_and_does_not_return_declaration(self):
        self._write_declaration_file_only()
        injected = artifacts.WorkflowError("APPEND_FAILED", "injected append failure")
        with mock.patch.object(self.store, "append_event", side_effect=injected):
            with self.assertRaisesRegex(artifacts.WorkflowError, "APPEND_FAILED"):
                declarations.load_route_declaration(self.store, self.task_id)
        self.assertEqual([], self._declared_events())

    def test_raw_declaration_reads_are_confined_to_helper_with_two_callers(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MODULE_PATH))
        readers: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in {"open", "load_artifact", "read_bytes", "read_text"}:
                continue
            target = node.func.value if isinstance(node.func, ast.Attribute) else node
            hits_path = _contains_declaration_path(target) or any(
                _contains_declaration_path(argument) for argument in node.args
            )
            if not hits_path:
                continue
            owner = _enclosing_function(tree, node)
            if owner is not None:
                readers.add(owner)
        self.assertEqual({"_read_route_declaration_bytes"}, readers)

        functions = _function_nodes(tree)
        recover = functions["recover_route_declaration_event"]
        load_locked = functions["load_route_declaration_locked"]
        recover_source = ast.get_source_segment(source, recover) or ""
        load_source = ast.get_source_segment(source, load_locked) or ""
        self.assertIn("_read_route_declaration_bytes", recover_source)
        self.assertIn("_read_route_declaration_bytes", load_source)
        self.assertIn("recover_route_declaration_event", load_source)

        ordered: list[str] = []
        for node in ast.walk(load_locked):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name in {
                "recover_route_declaration_event",
                "_read_route_declaration_bytes",
            }:
                ordered.append(name)
        recover_index = ordered.index("recover_route_declaration_event")
        helper_index = ordered.index("_read_route_declaration_bytes")
        self.assertLess(recover_index, helper_index)


if __name__ == "__main__":
    unittest.main()
