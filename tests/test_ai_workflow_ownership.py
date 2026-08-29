"""Ownership registry sidecar and control-plane-separated side-effect ledger."""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_authorizations as authorizations
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


def _decision_record(*, actor: str = "owner") -> dict[str, object]:
    return {
        "event_type": "OWNER_DECISION",
        "decision": "defer",
        "actor": actor,
        "timestamp_utc": "2026-08-28T00:00:00Z",
        "previous_state": "AWAITING_OWNER_DECISION",
        "new_state": "DEFERRED",
        "task_sha256": "a" * 64,
    }


def _wrapper_lock_statements(function):
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


class _OwnershipGateMixin:
    ROLE_OWNERS = {
        "src/a.py": "terra",
        "src/pkg/mod.py": "terra",
        "docs/note.md": "luna",
    }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary.name))
        self.task = _parent_task()
        self.store.create_task(self.task)
        self.envelope_hash = artifacts.artifact_sha256(self.task)
        self.actor = "owner"
        self.evidence = _decision_record(actor=self.actor)
        self.store.record_decision(TASK_ID, self.evidence)
        self.owner_evidence_id = artifacts.artifact_sha256(self.evidence)

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

    def _events_path(self) -> Path:
        return self.store._require_task(TASK_ID) / "events.jsonl"

    def _record_registry(
        self, path_owners: dict[str, str] | None = None
    ) -> ownership.OwnershipRegistry:
        registry = ownership.OwnershipRegistry(
            schema_version=ownership.OWNERSHIP_REGISTRY_SCHEMA_VERSION,
            task_id=TASK_ID,
            envelope_hash=self.envelope_hash,
            path_owners=dict(path_owners or self.ROLE_OWNERS),
            registered_at_utc="2026-08-28T00:00:00Z",
        )
        with self.store.lock(TASK_ID):
            ownership.record_ownership_registry(self.store, TASK_ID, registry)
        return registry

    def _issue_transfer(self, **overrides: object) -> authorizations.OwnerAuthorization:
        kwargs: dict[str, object] = {
            "authorization_type": "OWNERSHIP_TRANSFER",
            "actor": self.actor,
            "owner_evidence_id": self.owner_evidence_id,
            "issued_at_utc": "2026-08-28T12:00:00Z",
            "path": "src/a.py",
            "from_role": "terra",
            "to_role": "luna",
            "allowed_paths": ("src/a.py",),
            "max_dispatches": 2,
        }
        kwargs.update(overrides)
        return authorizations.issue_owner_authorization(self.store, TASK_ID, **kwargs)

    def _issue_override(self) -> authorizations.OwnerAuthorization:
        return authorizations.issue_owner_authorization(
            self.store,
            TASK_ID,
            authorization_type="VERDICT_STALE_OVERRIDE",
            actor=self.actor,
            owner_evidence_id=self.owner_evidence_id,
            issued_at_utc="2026-08-28T12:00:00Z",
            candidate_state_digest="b" * 64,
        )

    def _require(
        self,
        role: str,
        *,
        permit_id: str,
        paths: tuple[str, ...],
        authorization_id: str | None = None,
    ) -> None:
        with self.store.lock(TASK_ID):
            ownership.require_write_ownership_locked(
                self.store,
                TASK_ID,
                role,
                permit_id=permit_id,
                paths=paths,
                authorization_id=authorization_id,
            )

    def _lock_with_kind(self, kind: str, *, path: str = "src/generated.py") -> None:
        extra = None
        if kind == "COMMAND_GENERATED":
            extra = {
                "producer": "ROLLOUT_TOOL_EVENTS",
                "producer_ref": "1",
                "command_sha256s": ["ab" * 32],
            }
        ownership.record_side_effect(
            self.store,
            TASK_ID,
            role="terra",
            path=path,
            effect_kind=kind,
            extra=extra,
        )


class ResolvePathOwnerTest(_OwnershipGateMixin, unittest.TestCase):
    def test_transfers_do_not_rewrite_registry_owner(self) -> None:
        self._record_registry()
        first = self._issue_transfer()
        second = self._issue_transfer(
            path="src/a.py",
            from_role="luna",
            to_role="sol",
            allowed_paths=("src/a.py",),
            max_dispatches=1,
        )
        with self.store.lock(TASK_ID):
            authorizations.consume_owner_authorization_locked(
                self.store,
                TASK_ID,
                first.authorization_id,
                binding={
                    "path": first.path,
                    "from_role": first.from_role,
                    "to_role": first.to_role,
                    "allowed_paths": list(first.allowed_paths or ()),
                    "max_dispatches": first.max_dispatches,
                },
            )
            authorizations.consume_owner_authorization_locked(
                self.store,
                TASK_ID,
                second.authorization_id,
                binding={
                    "path": second.path,
                    "from_role": second.from_role,
                    "to_role": second.to_role,
                    "allowed_paths": list(second.allowed_paths or ()),
                    "max_dispatches": second.max_dispatches,
                },
            )
        self.assertEqual(
            "terra",
            ownership.resolve_path_owner(self.store, TASK_ID, "src/a.py"),
        )
        loaded = ownership.load_ownership_registry(self.store, TASK_ID)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual("terra", loaded.path_owners["src/a.py"])

    def test_directory_authorization_uses_longest_prefix(self) -> None:
        self._record_registry({"src": "terra", "src/pkg": "luna", "docs": "sol"})
        self.assertEqual(
            "terra", ownership.resolve_path_owner(self.store, TASK_ID, "src/a.py")
        )
        self.assertEqual(
            "luna",
            ownership.resolve_path_owner(self.store, TASK_ID, "src/pkg/mod.py"),
        )
        self.assertEqual(
            "sol",
            ownership.resolve_path_owner(self.store, TASK_ID, "docs/note.md"),
        )

    def test_relative_dotdot_and_symlink_inputs_normalize_consistently(self) -> None:
        repo = Path(self.temporary.name) / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "a.py").write_text("owned\n", encoding="utf-8")
        (repo / "src" / "alias.py").symlink_to("a.py")
        other = workflow.WorkflowStore(Path(self.temporary.name) / "store")
        task = _parent_task()
        task["repository_root"] = str(repo)
        task["source_worktree"] = str(repo)
        other.create_task(task)
        envelope = artifacts.artifact_sha256(task)
        registry = ownership.OwnershipRegistry(
            schema_version=ownership.OWNERSHIP_REGISTRY_SCHEMA_VERSION,
            task_id=TASK_ID,
            envelope_hash=envelope,
            path_owners={"src/a.py": "terra"},
            registered_at_utc="2026-08-28T00:00:00Z",
        )
        with other.lock(TASK_ID):
            ownership.record_ownership_registry(other, TASK_ID, registry)
        expected = ownership.resolve_path_owner(other, TASK_ID, "src/a.py")
        self.assertEqual("terra", expected)
        self.assertEqual(
            expected,
            ownership.resolve_path_owner(other, TASK_ID, "src/pkg/../a.py"),
        )
        self.assertEqual(
            expected,
            ownership.resolve_path_owner(other, TASK_ID, "./src/a.py"),
        )
        self.assertEqual(
            expected,
            ownership.resolve_path_owner(other, TASK_ID, "src/alias.py"),
        )

    def test_symlink_escaping_worktree_is_rejected(self) -> None:
        repo = Path(self.temporary.name) / "repo"
        outside = Path(self.temporary.name) / "outside"
        (repo / "src").mkdir(parents=True)
        outside.mkdir()
        (repo / "src" / "owned.py").write_text("owned\n", encoding="utf-8")
        (outside / "secret.py").write_text("escaped\n", encoding="utf-8")
        (repo / "src" / "escape.py").symlink_to(outside / "secret.py")
        other = workflow.WorkflowStore(Path(self.temporary.name) / "store-escape")
        task = _parent_task()
        task["repository_root"] = str(repo)
        task["source_worktree"] = str(repo)
        other.create_task(task)
        envelope = artifacts.artifact_sha256(task)
        registry = ownership.OwnershipRegistry(
            schema_version=ownership.OWNERSHIP_REGISTRY_SCHEMA_VERSION,
            task_id=TASK_ID,
            envelope_hash=envelope,
            path_owners={"src": "terra", "src/escape.py": "terra"},
            registered_at_utc="2026-08-28T00:00:00Z",
        )
        with other.lock(TASK_ID):
            ownership.record_ownership_registry(other, TASK_ID, registry)
        with self.assertRaisesRegex(artifacts.WorkflowError, "PLAN_INVALID"):
            ownership.resolve_path_owner(other, TASK_ID, "src/escape.py")
        with self.assertRaisesRegex(artifacts.WorkflowError, "PLAN_INVALID"):
            ownership.verify_actual_write_paths(
                other,
                TASK_ID,
                "terra",
                permit_id="permit-escape",
                actual_paths=("src/escape.py",),
            )
        with self.assertRaisesRegex(artifacts.WorkflowError, "PLAN_INVALID"):
            with other.lock(TASK_ID):
                ownership.require_write_ownership_locked(
                    other,
                    TASK_ID,
                    "terra",
                    permit_id="permit-escape",
                    paths=("src/escape.py",),
                )


class WriteOwnershipGateTest(_OwnershipGateMixin, unittest.TestCase):
    def test_precheck_closed_set_and_does_not_consume(self) -> None:
        self._record_registry()
        issued = self._issue_transfer()
        self.assertEqual(
            "OWNED",
            ownership.precheck_write_ownership(
                self.store, TASK_ID, "terra", paths=("src/a.py",)
            ),
        )
        self.assertEqual(
            "LEASE_REQUIRED",
            ownership.precheck_write_ownership(
                self.store, TASK_ID, "luna", paths=("src/a.py",)
            ),
        )
        self.assertEqual(
            "BLOCKED",
            ownership.precheck_write_ownership(
                self.store, TASK_ID, "terra", paths=("missing.py",)
            ),
        )
        self.assertEqual(
            0,
            authorizations.count_transfer_leases(
                self.store, TASK_ID, issued.authorization_id
            ),
        )
        rows = authorizations.replay_authorizations(self.store, TASK_ID)
        self.assertFalse(any(row.get("record_kind") == "consumption" for row in rows))
        self.assertFalse(any(row.get("record_kind") == "transfer_lease" for row in rows))

    def test_claimed_write_paths_normalizes_plan_scopes(self) -> None:
        claimed = ownership.claimed_write_paths(("src/a.py", "docs/note.md"))
        self.assertEqual(("src/a.py", "docs/note.md"), claimed)
        self.assertEqual(
            ("src/a.py",),
            ownership.claimed_write_paths(("src/pkg/../a.py",)),
        )

    def test_non_owner_requires_authorization_before_locking_effects(self) -> None:
        self._record_registry()
        executor = mock.Mock()
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "OWNERSHIP_TRANSFER_BLOCKED"
        ):
            with self.store.lock(TASK_ID):
                ownership.require_write_ownership_locked(
                    self.store,
                    TASK_ID,
                    "luna",
                    permit_id="permit-blocked",
                    paths=("src/a.py",),
                )
                executor()
        executor.assert_not_called()
        issued = self._issue_transfer(max_dispatches=2)
        self._require(
            "luna",
            permit_id="permit-1",
            paths=("src/a.py",),
            authorization_id=issued.authorization_id,
        )
        self.assertEqual(
            1,
            authorizations.count_transfer_leases(
                self.store, TASK_ID, issued.authorization_id
            ),
        )
        leases = authorizations.leases_for_permit(self.store, TASK_ID, "permit-1")
        self.assertEqual(1, len(leases))
        self.assertEqual("permit-1", leases[0]["permit_id"])
        self.assertEqual(["src/a.py"], leases[0]["allowed_paths"])

    def test_locking_kinds_require_focused_allowed_paths_then_record_lease(
        self,
    ) -> None:
        for kind in ("UNTRACKED_WRITE", "COMMAND_GENERATED"):
            with self.subTest(kind=kind):
                nested = tempfile.TemporaryDirectory()
                try:
                    original_store = self.store
                    original_task = self.task
                    original_hash = self.envelope_hash
                    original_evidence = self.evidence
                    original_evidence_id = self.owner_evidence_id
                    self.store = workflow.WorkflowStore(Path(nested.name))
                    self.task = _parent_task()
                    self.store.create_task(self.task)
                    self.envelope_hash = artifacts.artifact_sha256(self.task)
                    self.evidence = _decision_record(actor=self.actor)
                    self.store.record_decision(TASK_ID, self.evidence)
                    self.owner_evidence_id = artifacts.artifact_sha256(self.evidence)
                    self._record_registry()
                    self._lock_with_kind(kind)
                    override = self._issue_override()
                    with self.assertRaisesRegex(
                        artifacts.WorkflowError, "AUTHORIZATION_SCOPE_MISMATCH"
                    ):
                        self._require(
                            "luna",
                            permit_id="permit-override",
                            paths=("src/a.py",),
                            authorization_id=override.authorization_id,
                        )
                    issued = self._issue_transfer(
                        allowed_paths=("src/a.py",), max_dispatches=1
                    )
                    self._require(
                        "luna",
                        permit_id="permit-focused",
                        paths=("src/a.py",),
                        authorization_id=issued.authorization_id,
                    )
                    self.assertEqual(
                        1,
                        authorizations.count_transfer_leases(
                            self.store, TASK_ID, issued.authorization_id
                        ),
                    )
                finally:
                    self.store = original_store
                    self.task = original_task
                    self.envelope_hash = original_hash
                    self.evidence = original_evidence
                    self.owner_evidence_id = original_evidence_id
                    nested.cleanup()

    def test_claimed_paths_outside_allowed_paths_mismatch(self) -> None:
        self._record_registry()
        self._lock_with_kind("UNTRACKED_WRITE")
        issued = self._issue_transfer(allowed_paths=("src/a.py",))
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_SCOPE_MISMATCH"
        ):
            self._require(
                "luna",
                permit_id="permit-x",
                paths=("src/pkg/mod.py",),
                authorization_id=issued.authorization_id,
            )
        self.assertEqual(
            0,
            authorizations.count_transfer_leases(
                self.store, TASK_ID, issued.authorization_id
            ),
        )

    def test_lease_exhaustion_rejects_further_dispatch(self) -> None:
        self._record_registry()
        issued = self._issue_transfer(max_dispatches=1)
        self._require(
            "luna",
            permit_id="permit-1",
            paths=("src/a.py",),
            authorization_id=issued.authorization_id,
        )
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "AUTHORIZATION_EXHAUSTED"
        ):
            self._require(
                "luna",
                permit_id="permit-2",
                paths=("src/a.py",),
                authorization_id=issued.authorization_id,
            )
        self.assertEqual(
            1,
            authorizations.count_transfer_leases(
                self.store, TASK_ID, issued.authorization_id
            ),
        )

    def test_locked_gate_requires_held_lock(self) -> None:
        self._record_registry()
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            ownership.require_write_ownership_locked(
                self.store,
                TASK_ID,
                "terra",
                permit_id="permit-x",
                paths=("src/a.py",),
            )
        self.assertEqual(
            "_assert_lock_held",
            _first_call_name(ownership.require_write_ownership_locked),
        )

    def test_owner_focused_repair_after_locking_does_not_need_authorization(
        self,
    ) -> None:
        self._record_registry()
        self._lock_with_kind("UNTRACKED_WRITE")
        self.assertEqual(
            "OWNED",
            ownership.precheck_write_ownership(
                self.store, TASK_ID, "terra", paths=("src/a.py", "src/pkg/mod.py")
            ),
        )
        self._require("terra", permit_id="permit-owner", paths=("src/a.py",))
        self.assertEqual(
            (),
            authorizations.leases_for_permit(self.store, TASK_ID, "permit-owner"),
        )


class ActualPathVerificationTest(_OwnershipGateMixin, unittest.TestCase):
    def test_signature_requires_permit_id(self) -> None:
        signature = inspect.signature(ownership.verify_actual_write_paths)
        self.assertIn("permit_id", signature.parameters)
        parameter = signature.parameters["permit_id"]
        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, parameter.kind)
        self.assertIs(inspect.Parameter.empty, parameter.default)
        self.assertNotIn("skip", signature.parameters)

    def test_verify_nested_registry_uses_longest_prefix_not_parent_key(self) -> None:
        self._record_registry({"src": "terra", "src/pkg": "luna"})
        self.assertEqual(
            "luna",
            ownership.resolve_path_owner(self.store, TASK_ID, "src/pkg/mod.py"),
        )
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "OWNERSHIP_TRANSFER_BLOCKED"
        ):
            self._require(
                "terra",
                permit_id="permit-terra",
                paths=("src/pkg/mod.py",),
            )
        with self.assertRaisesRegex(artifacts.WorkflowError, "OWNERSHIP_VIOLATION"):
            ownership.verify_actual_write_paths(
                self.store,
                TASK_ID,
                "terra",
                permit_id="permit-terra",
                actual_paths=("src/pkg/mod.py",),
            )
        ownership.verify_actual_write_paths(
            self.store,
            TASK_ID,
            "luna",
            permit_id="permit-luna",
            actual_paths=("src/pkg/mod.py",),
        )
        ownership.verify_actual_write_paths(
            self.store,
            TASK_ID,
            "terra",
            permit_id="permit-terra-owned",
            actual_paths=("src/a.py",),
        )

    def test_unknown_actual_paths_are_a_caller_reject(self) -> None:
        self._record_registry()
        with self.assertRaisesRegex(
            artifacts.WorkflowError, "ACTUAL_WRITE_PATHS_UNKNOWN"
        ):
            ownership.verify_actual_write_paths(
                self.store,
                TASK_ID,
                "terra",
                permit_id="permit-unknown",
                actual_paths=None,  # type: ignore[arg-type]
            )

    def test_violation_event_shape_is_golden_and_side_effects_stay_closed(
        self,
    ) -> None:
        self._record_registry()
        issued = self._issue_transfer(max_dispatches=1)
        self._require(
            "luna",
            permit_id="permit-a",
            paths=("src/a.py",),
            authorization_id=issued.authorization_id,
        )
        ownership.record_side_effect(
            self.store,
            TASK_ID,
            role="luna",
            path="src/a.py",
            effect_kind="OWNED_WRITE",
            permit_id="permit-a",
        )
        self.assertEqual(
            frozenset(
                {
                    "event_type",
                    "task_id",
                    "envelope_hash",
                    "permit_id",
                    "role",
                    "paths",
                    "timestamp_utc",
                }
            ),
            ownership.OWNERSHIP_VIOLATION_EVENT_FIELDS,
        )
        with self.assertRaisesRegex(artifacts.WorkflowError, "OWNERSHIP_VIOLATION"):
            ownership.verify_actual_write_paths(
                self.store,
                TASK_ID,
                "luna",
                permit_id="permit-b",
                actual_paths=("src/a.py", "src/pkg/mod.py"),
            )
        violations = [
            event
            for event in self._events()
            if event.get("event_type") == ownership.OWNERSHIP_VIOLATION_EVENT_TYPE
        ]
        self.assertEqual(1, len(violations))
        event = violations[0]
        self.assertEqual(ownership.OWNERSHIP_VIOLATION_EVENT_FIELDS, set(event))
        self.assertEqual(
            ownership.OWNERSHIP_VIOLATION_EVENT_TYPE, event["event_type"]
        )
        self.assertEqual(TASK_ID, event["task_id"])
        self.assertEqual(self.envelope_hash, event["envelope_hash"])
        self.assertEqual("permit-b", event["permit_id"])
        self.assertEqual("luna", event["role"])
        self.assertEqual(["src/a.py", "src/pkg/mod.py"], event["paths"])
        self.assertIsInstance(event["timestamp_utc"], str)
        self.assertTrue(event["timestamp_utc"])
        rows = ownership.load_side_effects(self.store, TASK_ID)
        self.assertTrue(rows)
        for row in rows:
            self.assertIn(row["effect_kind"], ownership.EFFECT_KINDS)
            self.assertNotEqual(
                ownership.OWNERSHIP_VIOLATION_EVENT_TYPE, row["effect_kind"]
            )
        with self.store.lock(TASK_ID):
            self.assertTrue(
                ownership.has_unresolved_ownership_violation_locked(
                    self.store, TASK_ID
                )
            )
            self.assertTrue(
                ownership.has_unresolved_ownership_violation_locked(
                    self.store, TASK_ID
                )
            )

    def test_historical_lease_does_not_exempt_later_permit(self) -> None:
        self._record_registry()
        issued = self._issue_transfer(max_dispatches=1)
        self._require(
            "luna",
            permit_id="permit-a",
            paths=("src/a.py",),
            authorization_id=issued.authorization_id,
        )
        leases = authorizations.leases_for_permit(self.store, TASK_ID, "permit-a")
        self.assertEqual(["src/a.py"], leases[0]["allowed_paths"])
        self.assertEqual(
            (), authorizations.leases_for_permit(self.store, TASK_ID, "permit-b")
        )
        with self.assertRaisesRegex(artifacts.WorkflowError, "OWNERSHIP_VIOLATION"):
            ownership.verify_actual_write_paths(
                self.store,
                TASK_ID,
                "luna",
                permit_id="permit-b",
                actual_paths=("src/a.py",),
            )
        violations = [
            event
            for event in self._events()
            if event.get("event_type") == "OWNERSHIP_VIOLATION_RECORDED"
        ]
        self.assertEqual(1, len(violations))
        self.assertEqual("permit-b", violations[0]["permit_id"])
        self.assertEqual(["src/a.py"], violations[0]["paths"])

    def test_current_permit_lease_allows_actual_write(self) -> None:
        self._record_registry()
        issued = self._issue_transfer(max_dispatches=1)
        self._require(
            "luna",
            permit_id="permit-a",
            paths=("src/a.py",),
            authorization_id=issued.authorization_id,
        )
        ownership.verify_actual_write_paths(
            self.store,
            TASK_ID,
            "luna",
            permit_id="permit-a",
            actual_paths=("src/a.py",),
        )
        self.assertFalse(
            any(
                event.get("event_type") == "OWNERSHIP_VIOLATION_RECORDED"
                for event in self._events()
            )
        )


class OwnershipViolationQueryTest(_OwnershipGateMixin, unittest.TestCase):
    def _record_violation_event(self, **overrides: object) -> dict[str, object]:
        event: dict[str, object] = {
            "event_type": ownership.OWNERSHIP_VIOLATION_EVENT_TYPE,
            "task_id": TASK_ID,
            "envelope_hash": self.envelope_hash,
            "permit_id": "permit-v",
            "role": "luna",
            "paths": ["src/a.py"],
            "timestamp_utc": "2026-08-28T12:00:00Z",
        }
        event.update(overrides)
        self.store.append_event(TASK_ID, event)
        return event

    def test_locked_query_requires_held_lock(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            ownership.has_unresolved_ownership_violation_locked(self.store, TASK_ID)
        self.assertEqual(
            "_assert_lock_held",
            _first_call_name(ownership.has_unresolved_ownership_violation_locked),
        )

    def test_self_locking_wrapper_fails_when_lock_already_held(self) -> None:
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "TASK_ALREADY_RUNNING"
            ):
                ownership.has_unresolved_ownership_violation(self.store, TASK_ID)
        stmts = _wrapper_lock_statements(
            ownership.has_unresolved_ownership_violation
        )
        self.assertEqual(1, len(stmts))
        self.assertIsInstance(stmts[0], ast.With)
        source = inspect.getsource(ownership.has_unresolved_ownership_violation)
        self.assertIn("has_unresolved_ownership_violation_locked", source)
        self.assertIn("store.lock(task_id)", source)

    def test_wrapper_delegates_true_outside_lock(self) -> None:
        self._record_violation_event()
        self.assertTrue(
            ownership.has_unresolved_ownership_violation(self.store, TASK_ID)
        )

    def test_corrupt_violation_fields_fail_closed(self) -> None:
        self._record_registry()
        cases = (
            {"paths": ["src/a.py"], "drop": "permit_id"},
            {"paths": ["src/a.py"], "surprise": True},
            {"paths": "src/a.py"},
            {"task_id": OTHER_TASK_ID},
        )
        for index, overrides in enumerate(cases):
            with self.subTest(index=index, overrides=overrides):
                nested = tempfile.TemporaryDirectory()
                try:
                    store = workflow.WorkflowStore(Path(nested.name))
                    task = _parent_task()
                    store.create_task(task)
                    envelope = artifacts.artifact_sha256(task)
                    event: dict[str, object] = {
                        "event_type": "OWNERSHIP_VIOLATION_RECORDED",
                        "task_id": TASK_ID,
                        "envelope_hash": envelope,
                        "permit_id": "permit-v",
                        "role": "luna",
                        "paths": ["src/a.py"],
                        "timestamp_utc": "2026-08-28T12:00:00Z",
                    }
                    payload = dict(overrides)
                    drop = payload.pop("drop", None)
                    event.update(payload)
                    if drop is not None:
                        del event[str(drop)]
                    store.append_event(TASK_ID, event)
                    with store.lock(TASK_ID):
                        with self.assertRaisesRegex(
                            artifacts.WorkflowError,
                            "OWNERSHIP_VIOLATION_LEDGER_CORRUPT",
                        ):
                            ownership.has_unresolved_ownership_violation_locked(
                                store, TASK_ID
                            )
                finally:
                    nested.cleanup()

    def test_truncated_events_jsonl_is_corrupt(self) -> None:
        self._record_violation_event()
        path = self._events_path()
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "OWNERSHIP_VIOLATION_LEDGER_CORRUPT"
            ):
                ownership.has_unresolved_ownership_violation_locked(
                    self.store, TASK_ID
                )

    def test_non_object_event_line_is_corrupt(self) -> None:
        path = self._events_path()
        path.write_bytes(b"[]\n")
        with self.store.lock(TASK_ID):
            with self.assertRaisesRegex(
                artifacts.WorkflowError, "OWNERSHIP_VIOLATION_LEDGER_CORRUPT"
            ):
                ownership.has_unresolved_ownership_violation_locked(
                    self.store, TASK_ID
                )


class OwnershipDistributionTest(unittest.TestCase):
    def test_sync_manifest_lists_ownership_artifacts(self) -> None:
        self.assertIn(
            "ai_workflow_ownership_registry.schema.json",
            sync_plugin.CONFIG_FILES,
        )
        self.assertIn("ai_workflow_side_effect.schema.json", sync_plugin.CONFIG_FILES)
        self.assertIn("ai_workflow_ownership.py", sync_plugin.RUNTIME_FILES)

    def test_module_does_not_import_host_kernel(self) -> None:
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
        self.assertIn("ai_workflow_authorizations", imported)


if __name__ == "__main__":
    unittest.main()
