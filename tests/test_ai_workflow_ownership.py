"""Ownership registry sidecar and control-plane-separated side-effect ledger."""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_ownership as ownership
from scripts import ai_workflow_planning as planning
from scripts import sync_plugin


ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "AWF-20260803-001"
OTHER_TASK_ID = "AWF-20260803-002"
REGISTRY_FILENAME = "ownership-registry.json"
LEDGER_NAME = "side-effects.jsonl"
CONTROL_PLANE_PATHS = (
    "route-declaration.json",
    "ownership-registry.json",
    "preflight-records.jsonl",
    "dispatch-permits.jsonl",
)
LOCKING_KINDS = (
    "UNTRACKED_WRITE",
    "COMMAND_GENERATED",
    "EXTERNAL",
    "UNOBSERVED_ASSUMED_PRESENT",
)


def _parent_task(*, task_id: str = TASK_ID) -> dict[str, object]:
    return {
        "schema_version": "ai-task-1",
        "task_id": task_id,
        "task_type": "REMEDIATION",
        "objective": "implement one bounded, approved repair",
        "repository_root": str(ROOT),
        "source_worktree": str(ROOT),
        "base_commit": "b" * 40,
        "candidate_commit": "c" * 40,
        "authoritative_files": ["README.md"],
        "allowed_write_paths": ["src", "docs"],
        "forbidden_actions": ["merge", "push"],
        "risk_flags": [],
        "acceptance_commands": ["python -m unittest"],
        "verification_level": "L1",
        "human_gates": ["EXECUTION_APPROVAL"],
    }


def _plan_document(*, task_id: str = TASK_ID) -> dict[str, object]:
    return {
        "schema_version": "ai-plan-1",
        "plan_id": "plan-20260803-001",
        "task_id": task_id,
        "goal": "complete the bounded repair",
        "done_when": ["focused tests pass"],
        "tasks": [
            {
                "id": "task-a",
                "owner_role": "terra",
                "read_scope": [],
                "write_scope": ["src/a.py", "src/pkg/mod.py"],
                "do_not_touch": [],
                "depends_on": [],
                "expected_result": "bounded result for task-a",
                "verification_commands": ["python -m unittest"],
                "first_artifact": "tests/task-a.py",
                "evidence_level": "L1",
            },
            {
                "id": "task-b",
                "owner_role": "luna",
                "read_scope": [],
                "write_scope": ["docs/note.md"],
                "do_not_touch": [],
                "depends_on": [],
                "expected_result": "bounded result for task-b",
                "verification_commands": ["python -m unittest"],
                "first_artifact": "tests/task-b.py",
                "evidence_level": "L1",
            },
        ],
        "stages": [["task-a", "task-b"]],
    }


def _frozen_plan(*, task_id: str = TASK_ID) -> planning.FrozenPlan:
    return planning.validate_plan(_plan_document(task_id=task_id), _parent_task(task_id=task_id))


def _build_registry(
    plan: planning.FrozenPlan,
    *,
    task_id: str = TASK_ID,
    envelope_hash: str | None = None,
    registered_at_utc: str = "2026-08-28T00:00:00Z",
) -> ownership.OwnershipRegistry:
    return ownership.build_ownership_registry(
        task_id=task_id,
        envelope_hash=envelope_hash or ("a" * 64),
        plan=plan,
        registered_at_utc=registered_at_utc,
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


class OwnershipRegistrySchemaTest(unittest.TestCase):
    def test_closed_field_set_and_schema_version(self) -> None:
        registry = _build_registry(_frozen_plan())
        payload = registry.to_dict()
        ownership.validate_ownership_registry(payload)
        self.assertEqual(ownership.OWNERSHIP_REGISTRY_FIELDS, set(payload))
        self.assertEqual("ai-ownership-registry-1", payload["schema_version"])
        self.assertEqual(
            "ai-ownership-registry-1",
            ownership.OWNERSHIP_REGISTRY_SCHEMA_VERSION,
        )

    def test_path_owners_match_scope_owner_map_after_normalize_scope(self) -> None:
        plan = _frozen_plan()
        expected = planning.scope_owner_map(plan)
        registry = _build_registry(plan)
        self.assertEqual(expected, registry.path_owners)
        for path in registry.path_owners:
            self.assertEqual(planning.normalize_scope(path).as_posix(), path)
            self.assertNotIn(".", Path(path).parts)
            self.assertNotIn("..", Path(path).parts)
            self.assertNotIn("", path.split("/"))

    def test_missing_and_unknown_fields_are_rejected(self) -> None:
        payload = _build_registry(_frozen_plan()).to_dict()
        del payload["path_owners"]
        with self.assertRaisesRegex(artifacts.WorkflowError, "MISSING_FIELD"):
            ownership.validate_ownership_registry(payload)
        extra = _build_registry(_frozen_plan()).to_dict()
        extra["surprise"] = True
        with self.assertRaisesRegex(artifacts.WorkflowError, "UNKNOWN_FIELD"):
            ownership.validate_ownership_registry(extra)


class OwnershipRegistryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary.name))
        self.task = _parent_task()
        self.store.create_task(self.task)
        self.plan = _frozen_plan()
        self.envelope_hash = artifacts.artifact_sha256(self.task)
        self.registry = _build_registry(self.plan, envelope_hash=self.envelope_hash)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _task_json(self) -> dict[str, object]:
        return artifacts.load_artifact(self.store._require_task(TASK_ID) / "task.json")

    def test_duplicate_registry_is_conflict(self) -> None:
        with self.store.lock(TASK_ID):
            path = ownership.record_ownership_registry(
                self.store, TASK_ID, self.registry
            )
            self.assertEqual(REGISTRY_FILENAME, path.name)
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "OWNERSHIP_REGISTRY_CONFLICT"
            ):
                ownership.record_ownership_registry(
                    self.store, TASK_ID, self.registry
                )

    def test_task_envelope_fields_unchanged_after_registry_write(self) -> None:
        before = self._task_json()
        before_keys = set(before)
        self.assertEqual(workflow.TASK_FIELDS, before_keys | {"paired_case_id"})
        self.assertEqual(workflow.REQUIRED_TASK_FIELDS, before_keys)
        with self.store.lock(TASK_ID):
            ownership.record_ownership_registry(self.store, TASK_ID, self.registry)
        after = self._task_json()
        self.assertEqual(before_keys, set(after))
        self.assertEqual(before, after)
        loaded = ownership.load_ownership_registry(self.store, TASK_ID)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(self.registry.to_dict(), loaded.to_dict())

    def test_load_returns_none_when_missing(self) -> None:
        self.assertIsNone(ownership.load_ownership_registry(self.store, TASK_ID))


class SideEffectLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary.name))
        self.store.create_task(_parent_task())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _events(self) -> list[dict[str, object]]:
        path = self.store._require_task(TASK_ID) / "events.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def test_closed_effect_kinds_exclude_violation_event_type(self) -> None:
        self.assertEqual(
            frozenset(
                {
                    "CONTROL_PLANE_ARTIFACT",
                    "OWNED_WRITE",
                    "UNTRACKED_WRITE",
                    "COMMAND_GENERATED",
                    "EXTERNAL",
                    "UNOBSERVED_ASSUMED_PRESENT",
                }
            ),
            ownership.EFFECT_KINDS,
        )
        self.assertEqual(
            frozenset(
                {
                    "OWNED_WRITE",
                    "UNTRACKED_WRITE",
                    "COMMAND_GENERATED",
                    "EXTERNAL",
                    "UNOBSERVED_ASSUMED_PRESENT",
                }
            ),
            ownership.LOCKING_EFFECT_KINDS,
        )
        self.assertNotIn("CONTROL_PLANE_ARTIFACT", ownership.LOCKING_EFFECT_KINDS)
        self.assertEqual(
            "OWNERSHIP_VIOLATION_RECORDED",
            ownership.OWNERSHIP_VIOLATION_EVENT_TYPE,
        )
        self.assertNotIn(
            ownership.OWNERSHIP_VIOLATION_EVENT_TYPE, ownership.EFFECT_KINDS
        )
        self.assertNotIn("seq", ownership.SIDE_EFFECT_FIELDS)

    def test_record_appends_ledger_and_side_effect_recorded_event(self) -> None:
        ownership.record_side_effect(
            self.store,
            TASK_ID,
            role="terra",
            path="src/a.py",
            effect_kind="OWNED_WRITE",
            permit_id="permit-1",
        )
        rows = ownership.load_side_effects(self.store, TASK_ID)
        self.assertEqual(1, len(rows))
        self.assertEqual("ai-side-effect-1", rows[0]["schema_version"])
        self.assertEqual(TASK_ID, rows[0]["task_id"])
        self.assertEqual("terra", rows[0]["role"])
        self.assertEqual("src/a.py", rows[0]["path"])
        self.assertEqual("OWNED_WRITE", rows[0]["effect_kind"])
        self.assertEqual("permit-1", rows[0]["permit_id"])
        self.assertNotIn("seq", rows[0])
        events = [
            event
            for event in self._events()
            if event.get("event_type") == "SIDE_EFFECT_RECORDED"
        ]
        self.assertEqual(1, len(events))
        self.assertEqual(TASK_ID, events[0]["task_id"])
        self.assertEqual("OWNED_WRITE", events[0]["effect_kind"])
        self.assertEqual("src/a.py", events[0]["path"])

    def test_effect_kind_outside_closed_set_is_rejected(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_ENUM"):
            ownership.record_side_effect(
                self.store,
                TASK_ID,
                role="terra",
                path="src/a.py",
                effect_kind="NOT_A_KIND",
            )
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_ENUM"):
            ownership.record_side_effect(
                self.store,
                TASK_ID,
                role="terra",
                path="src/a.py",
                effect_kind="OWNERSHIP_VIOLATION_RECORDED",
            )
        self.assertEqual((), ownership.load_side_effects(self.store, TASK_ID))
        self.assertFalse(
            any(
                event.get("event_type") == "OWNERSHIP_VIOLATION_RECORDED"
                for event in self._events()
            )
        )

    def test_control_plane_artifacts_do_not_lock_ownership(self) -> None:
        self.assertFalse(
            ownership.has_ownership_locking_side_effect(self.store, TASK_ID)
        )
        for path in CONTROL_PLANE_PATHS:
            ownership.record_side_effect(
                self.store,
                TASK_ID,
                role="host",
                path=path,
                effect_kind="CONTROL_PLANE_ARTIFACT",
            )
            self.assertFalse(
                ownership.has_ownership_locking_side_effect(self.store, TASK_ID),
                path,
            )
        rows = ownership.load_side_effects(self.store, TASK_ID)
        self.assertEqual(len(CONTROL_PLANE_PATHS), len(rows))
        self.assertEqual(
            ["CONTROL_PLANE_ARTIFACT"] * len(CONTROL_PLANE_PATHS),
            [row["effect_kind"] for row in rows],
        )

    def test_locking_kinds_set_ownership_locking_flag(self) -> None:
        for kind in LOCKING_KINDS:
            with self.subTest(kind=kind):
                nested = tempfile.TemporaryDirectory()
                try:
                    store = workflow.WorkflowStore(Path(nested.name))
                    store.create_task(_parent_task())
                    extra = None
                    if kind == "COMMAND_GENERATED":
                        extra = {
                            "producer": "ROLLOUT_TOOL_EVENTS",
                            "producer_ref": "1",
                            "command_sha256s": ["ab" * 32],
                        }
                    ownership.record_side_effect(
                        store,
                        TASK_ID,
                        role="terra",
                        path="src/generated.py",
                        effect_kind=kind,
                        extra=extra,
                    )
                    self.assertTrue(
                        ownership.has_ownership_locking_side_effect(store, TASK_ID)
                    )
                    if extra is not None:
                        row = ownership.load_side_effects(store, TASK_ID)[0]
                        self.assertEqual("ROLLOUT_TOOL_EVENTS", row["producer"])
                        self.assertEqual("1", row["producer_ref"])
                        self.assertEqual(["ab" * 32], row["command_sha256s"])
                finally:
                    nested.cleanup()

    def test_locked_recorder_requires_held_lock(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            ownership.record_side_effect_locked(
                self.store,
                TASK_ID,
                role="terra",
                path="src/a.py",
                effect_kind="OWNED_WRITE",
            )
        self.assertEqual(
            "_assert_lock_held",
            _first_call_name(ownership.record_side_effect_locked),
        )

    def test_self_locking_wrapper_fails_when_lock_already_held(self) -> None:
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "TASK_ALREADY_RUNNING"
            ):
                ownership.record_side_effect(
                    self.store,
                    TASK_ID,
                    role="terra",
                    path="src/a.py",
                    effect_kind="OWNED_WRITE",
                )


class SideEffectReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary.name))
        self.store.create_task(_parent_task())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ledger_path(self) -> Path:
        return self.store._require_task(TASK_ID) / LEDGER_NAME

    def _valid_bytes(self) -> bytes:
        ownership.record_side_effect(
            self.store,
            TASK_ID,
            role="terra",
            path="src/a.py",
            effect_kind="OWNED_WRITE",
        )
        return self._ledger_path().read_bytes()

    def test_truncated_trailing_record_is_corrupt(self) -> None:
        raw = self._valid_bytes().rstrip(b"\n")
        self._ledger_path().write_bytes(raw)
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "SIDE_EFFECT_LEDGER_CORRUPT"
        ):
            ownership.load_side_effects(self.store, TASK_ID)

    def test_foreign_task_id_is_corrupt(self) -> None:
        self._valid_bytes()
        foreign = {
            "schema_version": "ai-side-effect-1",
            "task_id": OTHER_TASK_ID,
            "role": "terra",
            "path": "src/a.py",
            "effect_kind": "OWNED_WRITE",
        }
        with self._ledger_path().open("a", encoding="utf-8") as handle:
            handle.write(artifacts.canonical_json(foreign) + "\n")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "SIDE_EFFECT_LEDGER_CORRUPT"
        ):
            ownership.load_side_effects(self.store, TASK_ID)

    def test_non_object_line_is_corrupt(self) -> None:
        raw = self._valid_bytes()
        self._ledger_path().write_bytes(raw + b"[]\n")
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "SIDE_EFFECT_LEDGER_CORRUPT"
        ):
            ownership.load_side_effects(self.store, TASK_ID)

    def test_effect_kind_outside_closed_set_on_disk_is_corrupt(self) -> None:
        raw = self._valid_bytes()
        poisoned = {
            "schema_version": "ai-side-effect-1",
            "task_id": TASK_ID,
            "role": "terra",
            "path": "src/a.py",
            "effect_kind": "OWNERSHIP_VIOLATION_RECORDED",
        }
        self._ledger_path().write_bytes(
            raw + (artifacts.canonical_json(poisoned) + "\n").encode("utf-8")
        )
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "SIDE_EFFECT_LEDGER_CORRUPT"
        ):
            ownership.load_side_effects(self.store, TASK_ID)

    def test_ledger_api_has_no_seq_parameter(self) -> None:
        self.assertNotIn(
            "seq", inspect.signature(ownership.record_side_effect).parameters
        )
        self.assertNotIn(
            "seq", inspect.signature(ownership.load_side_effects).parameters
        )


class OwnershipDistributionTest(unittest.TestCase):
    def test_sync_manifest_lists_ownership_artifacts(self) -> None:
        self.assertIn(
            "ai_workflow_ownership_registry.schema.json",
            sync_plugin.CONFIG_FILES,
        )
        self.assertIn("ai_workflow_side_effect.schema.json", sync_plugin.CONFIG_FILES)
        self.assertIn("ai_workflow_ownership.py", sync_plugin.RUNTIME_FILES)

    def test_module_does_not_import_host_or_authorizations(self) -> None:
        source = (ROOT / "scripts" / "ai_workflow_ownership.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("ai_workflow", imported)
        self.assertNotIn("ai_workflow_repairs", imported)
        self.assertNotIn("sync_plugin", imported)
        self.assertNotIn("ai_workflow_authorizations", imported)


if __name__ == "__main__":
    unittest.main()
