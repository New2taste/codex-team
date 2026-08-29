"""FS observation, COMMAND_GENERATED producers, and effectful-role derivation."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import stat
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path, PurePosixPath

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_candidate_state as candidate_state
from scripts import ai_workflow_ownership as ownership
from scripts import ai_workflow_planning as planning
from scripts import ai_workflow_side_effects as side_effects
from scripts import sync_plugin


ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "AWF-20260803-001"
PLAN_SHA256 = "ab" * 32
SUBTASK_ID = "construction-601"
COMMAND_EVENT_INDEX = 1
SHELL_COMMAND = "python -m unittest tests.test_parser"
TOOL_COMMAND = ["git", "status", "--porcelain"]

# Frozen parse_codex_jsonl-shaped stdout events (thread + command_execution items).
ROLLOUT_EVENTS_WITH_COMMANDS: tuple[dict[str, object], ...] = (
    {
        "type": "thread.started",
        "thread_id": "019fc73c-4d40-7c20-a82a-c5a9ae078bcf",
    },
    {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": SHELL_COMMAND,
            "cwd": "/work",
            "exit_code": 0,
            "aggregated_output": "ok",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "type": "file_change",
            "path": "src/generated.py",
        },
    },
    {
        "type": "event_msg",
        "payload": {
            "item": {
                "type": "command_execution",
                "command": TOOL_COMMAND,
                "cwd": "/work",
            }
        },
    },
    {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
)
ROLLOUT_EVENTS_WITHOUT_COMMANDS: tuple[dict[str, object], ...] = (
    {
        "type": "thread.started",
        "thread_id": "019fc73c-4d40-7c20-a82a-c5a9ae078bcf",
    },
    {"type": "turn.completed"},
)


def _command_sha256(command: object) -> str:
    if isinstance(command, str):
        material = command.encode("utf-8")
    else:
        material = artifacts.canonical_json(command).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        shell=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace')}"
        )
    return result


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "-b", "main")
    _run_git(path, "config", "user.email", "side-effect@example.test")
    _run_git(path, "config", "user.name", "Side Effect Test")
    _run_git(path, "config", "commit.gpgsign", "false")
    _run_git(path, "config", "core.autocrlf", "false")
    return path


def _write(repo: Path, relative: str, content: str) -> Path:
    destination = repo / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def _commit(repo: Path, message: str) -> str:
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-m", message)
    return _run_git(repo, "rev-parse", "HEAD").stdout.decode("ascii").strip()


def _parent_task(*, task_id: str = TASK_ID, repository_root: Path | None = None) -> dict[str, object]:
    root = str(repository_root or ROOT)
    return {
        "schema_version": "ai-task-1",
        "task_id": task_id,
        "task_type": "REMEDIATION",
        "objective": "implement one bounded, approved repair",
        "repository_root": root,
        "source_worktree": root,
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
            }
        ],
        "stages": [["task-a"]],
    }


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


def _exclusions(repo: Path) -> tuple[PurePosixPath, ...]:
    return candidate_state.candidate_exclusions(
        repo, repo / candidate_state.STATE_ROOT_PREFIX
    )


def _snapshot(repo: Path) -> side_effects.FSSnapshot:
    return side_effects.capture_fs_snapshot(repo, exclusions=_exclusions(repo))


def _change(
    path: str,
    change_kind: str,
    *,
    mode: str = "100644",
    kind: str = "file",
    digest: str | None = None,
) -> side_effects.FSChange:
    entry = None
    if change_kind != "DELETED":
        entry = side_effects.FSEntry(
            path=path,
            mode=mode,
            kind=kind,
            content_sha256=digest or ("d" * 64),
        )
    return side_effects.FSChange(path=path, change_kind=change_kind, entry_after=entry)


class SnapshotDiffClassifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = _init_repo(Path(self.temporary.name) / "repository")
        _write(self.repo, "tracked.txt", "one\n")
        _write(self.repo, "src/a.py", "print(1)\n")
        self.head = _commit(self.repo, "base")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_added_modified_deleted_untracked_and_mode_changes(self) -> None:
        before = _snapshot(self.repo)
        _write(self.repo, "added.txt", "new\n")
        _write(self.repo, "src/a.py", "print(2)\n")
        _write(self.repo, "untracked.txt", "loose\n")
        (self.repo / "tracked.txt").unlink()
        os.chmod(self.repo / "src/a.py", 0o755)
        after = _snapshot(self.repo)
        changes = {item.path: item for item in side_effects.diff_fs_snapshots(before, after)}
        self.assertEqual("ADDED", changes["added.txt"].change_kind)
        self.assertEqual("ADDED", changes["untracked.txt"].change_kind)
        self.assertEqual("DELETED", changes["tracked.txt"].change_kind)
        self.assertIsNone(changes["tracked.txt"].entry_after)
        self.assertEqual("MODIFIED", changes["src/a.py"].change_kind)
        self.assertEqual("100755", changes["src/a.py"].entry_after.mode)
        self.assertTrue(stat.S_ISREG((self.repo / "src/a.py").stat().st_mode))

    def test_excluded_control_plane_changes_do_not_appear_in_diff(self) -> None:
        before = _snapshot(self.repo)
        _write(self.repo, "data/state/ai-workflow/events.jsonl", "{}\n")
        _write(self.repo, ".codex/sessions/rollout-1.jsonl", "{}\n")
        _write(self.repo, "visible.py", "ok\n")
        after = _snapshot(self.repo)
        paths = {item.path for item in side_effects.diff_fs_snapshots(before, after)}
        self.assertIn("visible.py", paths)
        self.assertFalse(any(path.startswith("data/state/ai-workflow") for path in paths))
        self.assertFalse(any(path.startswith(".codex/sessions") for path in paths))

    def test_classify_owned_untracked_and_control_plane_never_command_generated(self) -> None:
        owners = {"src/a.py": "terra", "src/pkg/mod.py": "terra"}
        self.assertEqual(
            "OWNED_WRITE",
            side_effects.classify_side_effect(
                _change("src/a.py", "MODIFIED"), path_owners=owners
            ),
        )
        self.assertEqual(
            "UNTRACKED_WRITE",
            side_effects.classify_side_effect(
                _change("docs/note.md", "ADDED"), path_owners=owners
            ),
        )
        self.assertEqual(
            "CONTROL_PLANE_ARTIFACT",
            side_effects.classify_side_effect(
                _change("data/state/ai-workflow/events.jsonl", "ADDED"),
                path_owners=owners,
            ),
        )
        self.assertEqual(
            "CONTROL_PLANE_ARTIFACT",
            side_effects.classify_side_effect(
                _change(".codex/sessions/rollout.jsonl", "ADDED"),
                path_owners=owners,
            ),
        )
        commandish = _change("logs/commands.jsonl", "ADDED")
        self.assertNotEqual(
            "COMMAND_GENERATED",
            side_effects.classify_side_effect(commandish, path_owners=owners),
        )
        self.assertEqual(
            "UNTRACKED_WRITE",
            side_effects.classify_side_effect(commandish, path_owners=owners),
        )

    def test_classify_side_effect_has_no_command_generated_return_path(self) -> None:
        tree = ast.parse(inspect.getsource(side_effects.classify_side_effect))
        func = tree.body[0]
        self.assertIsInstance(func, ast.FunctionDef)
        for node in ast.walk(func):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            if isinstance(node.value, ast.Constant):
                self.assertNotEqual("COMMAND_GENERATED", node.value.value)


class CommandProducerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary.name) / "state")
        self.store.create_task(_parent_task())
        self.repo = _init_repo(Path(self.temporary.name) / "repository")
        _write(self.repo, "README.md", "base\n")
        _commit(self.repo, "base")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _observe(
        self,
        *,
        after_write: str | None = "src/new.py",
        events: tuple[dict[str, object], ...] = (),
        construction_step: dict[str, object] | None = None,
        path_owners: dict[str, str] | None = None,
    ) -> tuple[side_effects.FSChange, ...]:
        if path_owners is not None:
            plan = planning.validate_plan(_plan_document(), _parent_task())
            registry = ownership.build_ownership_registry(
                task_id=TASK_ID,
                envelope_hash=artifacts.artifact_sha256(_parent_task()),
                plan=plan,
                registered_at_utc="2026-08-28T00:00:00Z",
            )
            with self.store.lock(TASK_ID):
                ownership.record_ownership_registry(self.store, TASK_ID, registry)
        before = _snapshot(self.repo)
        if after_write is not None:
            _write(self.repo, after_write, "payload\n")
        after = _snapshot(self.repo)
        return side_effects.observe_execution_side_effects(
            self.store,
            TASK_ID,
            role="terra",
            permit_id="permit-1",
            before=before,
            after=after,
            rollout_events=events,
            construction_step=construction_step,
        )

    def test_extract_command_executions_golden_and_empty(self) -> None:
        self.assertEqual((), side_effects.extract_command_executions(()))
        extracted = side_effects.extract_command_executions(ROLLOUT_EVENTS_WITH_COMMANDS)
        self.assertEqual(2, len(extracted))
        self.assertEqual(_command_sha256(SHELL_COMMAND), extracted[0].command_sha256)
        self.assertEqual("ROLLOUT_TOOL_EVENTS", extracted[0].producer)
        self.assertEqual(str(COMMAND_EVENT_INDEX), extracted[0].producer_ref)
        self.assertEqual(_command_sha256(TOOL_COMMAND), extracted[1].command_sha256)
        self.assertEqual("ROLLOUT_TOOL_EVENTS", extracted[1].producer)
        self.assertEqual("3", extracted[1].producer_ref)
        self.assertEqual(
            (),
            side_effects.extract_command_executions(ROLLOUT_EVENTS_WITHOUT_COMMANDS),
        )

    def test_observe_records_rollout_command_generated_not_from_fs_guess(self) -> None:
        self._observe(
            after_write="src/a.py",
            events=ROLLOUT_EVENTS_WITH_COMMANDS,
            path_owners={},
        )
        rows = ownership.load_side_effects(self.store, TASK_ID)
        kinds = [row["effect_kind"] for row in rows]
        self.assertIn("COMMAND_GENERATED", kinds)
        generated = next(row for row in rows if row["effect_kind"] == "COMMAND_GENERATED")
        self.assertEqual("ROLLOUT_TOOL_EVENTS", generated["producer"])
        self.assertEqual(str(COMMAND_EVENT_INDEX), generated["producer_ref"])
        self.assertEqual(
            [_command_sha256(SHELL_COMMAND), _command_sha256(TOOL_COMMAND)],
            generated["command_sha256s"],
        )
        self.store._require_task(TASK_ID).joinpath(ownership.SIDE_EFFECT_LEDGER).unlink()
        self._observe(after_write="docs/note.md", events=ROLLOUT_EVENTS_WITHOUT_COMMANDS)
        kinds = [row["effect_kind"] for row in ownership.load_side_effects(self.store, TASK_ID)]
        self.assertNotIn("COMMAND_GENERATED", kinds)
        self.assertTrue(set(kinds) <= {"OWNED_WRITE", "UNTRACKED_WRITE", "CONTROL_PLANE_ARTIFACT"})

    def test_construction_step_retags_producer_and_rejects_malformed_refs(self) -> None:
        extracted = side_effects.extract_command_executions(ROLLOUT_EVENTS_WITH_COMMANDS)
        self._observe(events=ROLLOUT_EVENTS_WITH_COMMANDS)
        rollout_row = next(
            row
            for row in ownership.load_side_effects(self.store, TASK_ID)
            if row["effect_kind"] == "COMMAND_GENERATED"
        )
        self.assertEqual("ROLLOUT_TOOL_EVENTS", rollout_row["producer"])
        other = workflow.WorkflowStore(Path(self.temporary.name) / "state-b")
        other.create_task(_parent_task())
        before = _snapshot(self.repo)
        after = _snapshot(self.repo)
        side_effects.observe_execution_side_effects(
            other,
            TASK_ID,
            role="luna_construction",
            permit_id=None,
            before=before,
            after=after,
            rollout_events=ROLLOUT_EVENTS_WITH_COMMANDS,
            construction_step={"plan_sha256": PLAN_SHA256, "subtask_id": SUBTASK_ID},
        )
        frozen_row = next(
            row
            for row in ownership.load_side_effects(other, TASK_ID)
            if row["effect_kind"] == "COMMAND_GENERATED"
        )
        expected_ref = f"{PLAN_SHA256}:{SUBTASK_ID}"
        self.assertEqual("CONSTRUCTION_FROZEN_STEP", frozen_row["producer"])
        self.assertEqual(expected_ref, frozen_row["producer_ref"])
        self.assertNotEqual(rollout_row["producer"], frozen_row["producer"])
        self.assertEqual(
            {item.producer for item in extracted},
            {"ROLLOUT_TOOL_EVENTS"},
        )
        retagged = side_effects.retag_command_executions(
            extracted,
            producer="CONSTRUCTION_FROZEN_STEP",
            producer_ref=expected_ref,
        )
        self.assertEqual({"CONSTRUCTION_FROZEN_STEP"}, {item.producer for item in retagged})
        self.assertEqual(expected_ref, side_effects.construction_step_producer_ref(
            plan_sha256=PLAN_SHA256, subtask_id=SUBTASK_ID
        ))
        with self.assertRaises(artifacts.WorkflowError):
            side_effects.construction_step_producer_ref(plan_sha256="abc", subtask_id=SUBTASK_ID)
        with self.assertRaises(artifacts.WorkflowError):
            side_effects.construction_step_producer_ref(plan_sha256=PLAN_SHA256.upper(), subtask_id=SUBTASK_ID)
        with self.assertRaises(artifacts.WorkflowError):
            side_effects.construction_step_producer_ref(plan_sha256=PLAN_SHA256, subtask_id="")
        with self.assertRaises(artifacts.WorkflowError):
            side_effects.retag_command_executions(
                extracted, producer="MODEL_SELF_REPORT", producer_ref="x"
            )

    def test_command_execution_producer_is_not_a_constant_field(self) -> None:
        tree = ast.parse(inspect.getsource(side_effects.CommandExecution))
        class_def = next(
            node for node in tree.body if isinstance(node, ast.ClassDef)
        )
        producer_ann = next(
            node
            for node in class_def.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "producer"
        )
        self.assertIsNone(producer_ann.value)

    def test_external_locked_recorder_asserts_lock_first(self) -> None:
        self.assertEqual(
            "_assert_lock_held",
            _first_call_name(side_effects.record_external_side_effect_locked),
        )
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            side_effects.record_external_side_effect_locked(
                self.store, TASK_ID, role="terra", permit_id="permit-1"
            )
        with self.store.lock(TASK_ID):
            side_effects.record_external_side_effect_locked(
                self.store, TASK_ID, role="terra", permit_id="permit-1"
            )
        rows = ownership.load_side_effects(self.store, TASK_ID)
        self.assertEqual("EXTERNAL", rows[0]["effect_kind"])
        self.assertEqual("permit-1", rows[0]["permit_id"])

    def test_unobserved_is_locking(self) -> None:
        side_effects.record_unobserved_side_effect(
            self.store,
            TASK_ID,
            role="terra",
            permit_id=None,
            reason="timeout",
        )
        self.assertTrue(ownership.has_ownership_locking_side_effect(self.store, TASK_ID))
        self.assertEqual(
            "UNOBSERVED_ASSUMED_PRESENT",
            ownership.load_side_effects(self.store, TASK_ID)[0]["effect_kind"],
        )


class EffectfulRoleDerivationTest(unittest.TestCase):
    def test_toml_hand_compute_matches_and_excludes_read_only_roles(self) -> None:
        with (ROOT / "config" / "ai_workflow.toml").open("rb") as handle:
            config = tomllib.load(handle)
        expected = frozenset(
            name
            for name, spec in config["roles"].items()
            if isinstance(spec, dict)
            and spec.get("sandbox") in side_effects.EFFECTFUL_ROLE_SANDBOXES
        )
        self.assertEqual(expected, side_effects.derive_effectful_roles(config))
        self.assertIn("luna_construction", expected)
        self.assertIn("terra", expected)
        self.assertIn("terra_xhigh", expected)
        self.assertNotIn("sol_medium_reviewer", expected)
        self.assertNotIn("luna", expected)
        self.assertTrue(expected <= frozenset(config["roles"]))
        self.assertEqual(
            frozenset({"workspace-write", "assignment-scoped-write"}),
            side_effects.EFFECTFUL_ROLE_SANDBOXES,
        )

    def test_runtime_file_is_on_the_plugin_manifest(self) -> None:
        self.assertIn("ai_workflow_side_effects.py", sync_plugin.RUNTIME_FILES)


class CommandProducersClosedSetTest(unittest.TestCase):
    def test_closed_producer_set(self) -> None:
        self.assertEqual(
            frozenset({"ROLLOUT_TOOL_EVENTS", "CONSTRUCTION_FROZEN_STEP"}),
            side_effects.COMMAND_PRODUCERS,
        )


if __name__ == "__main__":
    unittest.main()
