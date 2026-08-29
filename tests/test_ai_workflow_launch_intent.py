"""Launch-intent events and versioned fork/nested runtime evidence producers."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts
from scripts import ai_workflow_declarations as declarations
from scripts import ai_workflow_dispatch_policy as policy
from scripts import ai_workflow_evidence as evidence
from scripts import ai_workflow_preflight as preflight
from scripts import sync_plugin
from tests.test_ai_workflow import (
    CodexRunnerTest,
    TaskValidationTest,
    _RecordingPopen,
    _compat_popen,
    _install_declaration,
)
from tests.test_ai_workflow_runtime import THREAD_ID, write_exec_rollout


ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "AWF-20260803-001"
OTHER_TASK_ID = "AWF-20260803-002"
GOLDEN_LAUNCH_INTENT_ID = (
    "6cd23384986f15cb6ca1fcb5a5178de4fb964f328242d45b940af72538c5856d"
)
GOLDEN_EVIDENCE_ID = (
    "2d7a8bc3e41ffe601fba52c2f5698fd891285be07846798071ea7e1ac3099cf3"
)
FROZEN_RUNTIME_EVIDENCE_RECORDED_FIELDS = frozenset(
    {
        "event_type",
        "attempt_id",
        "requested_role",
        "thread_id",
        "execution_surface",
        "runtime_evidence_sha256",
        "usage",
        "result_sha256",
    }
)
FROZEN_ASSIGNMENT_RUNTIME_EVIDENCE_FIELDS = frozenset(
    {
        "event_type",
        "attempt_id",
        "requested_role",
        "thread_id",
        "execution_surface",
        "runtime_evidence_sha256",
    }
)


def _sha256_canonical(value: object) -> str:
    return hashlib.sha256(artifacts.canonical_json(value).encode("utf-8")).hexdigest()


def _golden_launch_intent(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_type": "LAUNCH_INTENT_RECORDED",
        "event_id": "deadbeef" * 8,
        "task_id": TASK_ID,
        "envelope_hash": "a" * 64,
        "permit_id": "b" * 64,
        "role": "luna",
        "command_sha256": "c" * 64,
        "tool_mapping_sha256": "d" * 64,
        "route_config_hash": "e" * 64,
        "launcher_version": "ai-workflow-launcher-1",
        "install_version": "f" * 64,
        "timestamp_utc": "2026-08-28T00:00:00Z",
    }
    event.update(overrides)
    return event


def _golden_evidence(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": "ai-runtime-evidence-2",
        "evidence_id": "deadbeef" * 8,
        "task_id": TASK_ID,
        "envelope_hash": "a" * 64,
        "event_index": 3,
        "observed_agent_type": None,
        "native_agent_id": None,
        "native_thread_id": None,
        "fork_state": "VERIFIED_NONE",
        "nested_state": "VERIFIED_NONE",
        "recorded_at_utc": "2026-08-28T00:00:00Z",
    }
    record.update(overrides)
    return record


def _complete_observation(**overrides: object) -> dict[str, object]:
    observed: dict[str, object] = {
        "observed_agent_type": None,
        "native_agent_id": None,
        "native_thread_id": None,
        "fork": False,
        "nested": False,
    }
    observed.update(overrides)
    return observed


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


def _events(store: workflow.WorkflowStore, task_id: str) -> list[dict[str, object]]:
    path = store._require_task(task_id) / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _intent_events(store: workflow.WorkflowStore, task_id: str) -> list[dict[str, object]]:
    return [
        event
        for event in _events(store, task_id)
        if event.get("event_type") == evidence.LAUNCH_INTENT_EVENT_TYPE
    ]


class GoldenPreimageTest(unittest.TestCase):
    def test_launch_intent_exclude_is_only_event_id(self) -> None:
        self.assertEqual(frozenset({"event_id"}), evidence.LAUNCH_INTENT_ID_EXCLUDE)

    def test_runtime_evidence_exclude_is_only_evidence_id(self) -> None:
        self.assertEqual(frozenset({"evidence_id"}), evidence.RUNTIME_EVIDENCE_ID_EXCLUDE)

    def test_field_sets_match_frozen_contracts(self) -> None:
        self.assertEqual(
            frozenset(
                {
                    "event_type",
                    "event_id",
                    "task_id",
                    "envelope_hash",
                    "permit_id",
                    "role",
                    "command_sha256",
                    "tool_mapping_sha256",
                    "route_config_hash",
                    "launcher_version",
                    "install_version",
                    "timestamp_utc",
                }
            ),
            evidence.LAUNCH_INTENT_EVENT_FIELDS,
        )
        self.assertEqual(
            frozenset(
                {
                    "schema_version",
                    "evidence_id",
                    "task_id",
                    "envelope_hash",
                    "event_index",
                    "observed_agent_type",
                    "native_agent_id",
                    "native_thread_id",
                    "fork_state",
                    "nested_state",
                    "recorded_at_utc",
                }
            ),
            evidence.RUNTIME_EVIDENCE_V2_FIELDS,
        )
        self.assertEqual("LAUNCH_INTENT_RECORDED", evidence.LAUNCH_INTENT_EVENT_TYPE)
        self.assertEqual("ai-runtime-evidence-2", evidence.RUNTIME_EVIDENCE_V2_SCHEMA_VERSION)
        self.assertEqual(
            frozenset({"VERIFIED_NONE", "VERIFIED_PRESENT", "AUTHORITY_UNAVAILABLE"}),
            evidence.FORK_STATES,
        )
        self.assertEqual(evidence.FORK_STATES, evidence.NESTED_STATES)

    def test_frozen_launch_intent_hashes_to_literal_golden(self) -> None:
        computed = evidence.compute_launch_intent_id(_golden_launch_intent())
        self.assertEqual(GOLDEN_LAUNCH_INTENT_ID, computed)
        self.assertEqual(64, len(computed))

    def test_frozen_evidence_hashes_to_literal_golden(self) -> None:
        computed = evidence.compute_evidence_id(_golden_evidence())
        self.assertEqual(GOLDEN_EVIDENCE_ID, computed)
        self.assertEqual(64, len(computed))

    def test_prefilled_event_id_does_not_affect_launch_intent_id(self) -> None:
        left = evidence.compute_launch_intent_id(_golden_launch_intent(event_id="deadbeef" * 8))
        right = evidence.compute_launch_intent_id(_golden_launch_intent(event_id="cafebabe" * 8))
        self.assertEqual(GOLDEN_LAUNCH_INTENT_ID, left)
        self.assertEqual(left, right)

    def test_prefilled_evidence_id_does_not_affect_evidence_id(self) -> None:
        left = evidence.compute_evidence_id(_golden_evidence(evidence_id="deadbeef" * 8))
        right = evidence.compute_evidence_id(_golden_evidence(evidence_id="cafebabe" * 8))
        self.assertEqual(GOLDEN_EVIDENCE_ID, left)
        self.assertEqual(left, right)

    def test_verify_uses_the_same_exclude_and_projection(self) -> None:
        event = _golden_launch_intent()
        event["event_id"] = evidence.compute_launch_intent_id(event)
        evidence.verify_launch_intent_id(event)
        event["role"] = "terra"
        with self.assertRaisesRegex(artifacts.WorkflowError, "CONTENT_ID_MISMATCH"):
            evidence.verify_launch_intent_id(event)

        record = _golden_evidence()
        record["evidence_id"] = evidence.compute_evidence_id(record)
        evidence.verify_evidence_id(record)
        record["fork_state"] = "VERIFIED_PRESENT"
        with self.assertRaisesRegex(artifacts.WorkflowError, "CONTENT_ID_MISMATCH"):
            evidence.verify_evidence_id(record)

    def test_v2_record_and_schema_have_no_seq_field(self) -> None:
        self.assertNotIn("seq", evidence.RUNTIME_EVIDENCE_V2_FIELDS)
        schema = json.loads(
            (ROOT / "config" / "ai_workflow_runtime_evidence_v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("seq", schema.get("properties", {}))
        self.assertEqual("ai-runtime-evidence-2", schema["properties"]["schema_version"]["const"])


class ForkNestedEnumerationTest(unittest.TestCase):
    def test_complete_observation_without_fork_or_nested_is_verified_none(self) -> None:
        self.assertEqual(
            ("VERIFIED_NONE", "VERIFIED_NONE"),
            evidence.derive_fork_nested_states(_complete_observation()),
        )

    def test_complete_observation_with_fork_and_nested_is_verified_present(self) -> None:
        self.assertEqual(
            ("VERIFIED_PRESENT", "VERIFIED_PRESENT"),
            evidence.derive_fork_nested_states(
                _complete_observation(fork=True, nested=True)
            ),
        )

    def test_missing_observed_agent_type_is_authority_unavailable(self) -> None:
        observed = _complete_observation()
        del observed["observed_agent_type"]
        self.assertEqual(
            ("AUTHORITY_UNAVAILABLE", "AUTHORITY_UNAVAILABLE"),
            evidence.derive_fork_nested_states(observed),
        )

    def test_missing_native_agent_id_is_authority_unavailable(self) -> None:
        observed = _complete_observation()
        del observed["native_agent_id"]
        self.assertEqual(
            ("AUTHORITY_UNAVAILABLE", "AUTHORITY_UNAVAILABLE"),
            evidence.derive_fork_nested_states(observed),
        )

    def test_missing_native_thread_id_is_authority_unavailable(self) -> None:
        observed = _complete_observation()
        del observed["native_thread_id"]
        self.assertEqual(
            ("AUTHORITY_UNAVAILABLE", "AUTHORITY_UNAVAILABLE"),
            evidence.derive_fork_nested_states(observed),
        )

    def test_request_fork_turns_none_cannot_stand_in_for_missing_observation(self) -> None:
        self.assertEqual(
            ("AUTHORITY_UNAVAILABLE", "AUTHORITY_UNAVAILABLE"),
            evidence.derive_fork_nested_states({"fork_turns": "none"}),
        )

    def test_values_outside_closed_set_are_rejected(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_ENUM|INVALID_TYPE"):
            evidence.derive_fork_nested_states(_complete_observation(fork="maybe"))
        record = _golden_evidence(fork_state="YES")
        record["evidence_id"] = evidence.compute_evidence_id(record)
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_ENUM"):
            evidence.validate_runtime_evidence_v2(record)


class _EvidenceStoreMixin:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repo = root / "repository"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "evidence@example.test"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Evidence Test"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        (self.repo / "README.md").write_text("repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
        self.store = workflow.WorkflowStore(root / "state")
        self.task = TaskValidationTest().valid_task()
        self.task["task_id"] = TASK_ID
        self.task["repository_root"] = str(self.repo)
        self.store.create_task(self.task)
        _install_declaration(self.store, self.task, allowed_roles=("luna",), active_roles=("luna",))
        self.envelope_hash = artifacts.artifact_sha256(self.task)
        self.declaration = declarations.load_route_declaration(self.store, TASK_ID)
        assert self.declaration is not None
        self.permit = policy.DispatchPermit(
            permit_id="b" * 64,
            task_id=TASK_ID,
            role="luna",
            reservation_seq=1,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()


class LaunchIntentRecordTest(_EvidenceStoreMixin, unittest.TestCase):
    def test_first_statement_asserts_lock_held(self) -> None:
        self.assertEqual("_assert_lock_held", _first_call_name(evidence.record_launch_intent))

    def test_record_without_lock_is_rejected(self) -> None:
        with self.assertRaisesRegex(artifacts.WorkflowError, "LOCK_REQUIRED"):
            evidence.record_launch_intent(
                self.store,
                TASK_ID,
                permit=self.permit,
                role="luna",
                argv=("codex", "exec"),
                tool_mapping={},
            )

    def test_recorded_event_matches_golden_fields_and_hashes(self) -> None:
        argv = ("codex", "exec", "--json")
        tool_mapping = {"sandbox": "read-only"}
        with mock.patch.object(evidence, "_utc_now", return_value="2026-08-28T00:00:00Z"):
            with self.store.lock(TASK_ID):
                evidence.record_launch_intent(
                    self.store,
                    TASK_ID,
                    permit=self.permit,
                    role="luna",
                    argv=argv,
                    tool_mapping=tool_mapping,
                )
        events = _intent_events(self.store, TASK_ID)
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual(evidence.LAUNCH_INTENT_EVENT_FIELDS, set(event))
        self.assertEqual(evidence.LAUNCH_INTENT_EVENT_TYPE, event["event_type"])
        self.assertEqual(TASK_ID, event["task_id"])
        self.assertEqual(self.envelope_hash, event["envelope_hash"])
        self.assertEqual(self.permit.permit_id, event["permit_id"])
        self.assertEqual("luna", event["role"])
        self.assertEqual(_sha256_canonical(list(argv)), event["command_sha256"])
        self.assertEqual(_sha256_canonical(tool_mapping), event["tool_mapping_sha256"])
        self.assertEqual(self.declaration.route_config_hash, event["route_config_hash"])
        self.assertEqual(preflight.LAUNCHER_VERSION, event["launcher_version"])
        self.assertEqual(preflight.compute_install_version(), event["install_version"])
        self.assertEqual("2026-08-28T00:00:00Z", event["timestamp_utc"])
        evidence.verify_launch_intent_id(event)
        junk = dict(event)
        junk["event_id"] = "cafebabe" * 8
        self.assertEqual(event["event_id"], evidence.compute_launch_intent_id(junk))


class RuntimeEvidenceV2ReplayTest(_EvidenceStoreMixin, unittest.TestCase):
    def _seed_runtime_event(self) -> int:
        self.store.append_event(
            TASK_ID,
            {
                "event_type": "RUNTIME_EVIDENCE_RECORDED",
                "attempt_id": "attempt-1",
                "requested_role": "luna",
                "thread_id": THREAD_ID,
                "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
                "runtime_evidence_sha256": "a" * 64,
            },
        )
        return len(_events(self.store, TASK_ID)) - 1

    def test_append_matches_event_index_and_has_no_seq(self) -> None:
        event_index = self._seed_runtime_event()
        evidence.append_runtime_evidence_v2(
            self.store,
            TASK_ID,
            event_index=event_index,
            observed=_complete_observation(),
            recorded_at_utc="2026-08-28T00:00:00Z",
        )
        rows = evidence.replay_runtime_evidence_v2(self.store, TASK_ID)
        self.assertEqual(1, len(rows))
        record = rows[0]
        self.assertEqual(evidence.RUNTIME_EVIDENCE_V2_FIELDS, set(record))
        self.assertNotIn("seq", record)
        self.assertEqual(event_index, record["event_index"])
        events = _events(self.store, TASK_ID)
        self.assertEqual("RUNTIME_EVIDENCE_RECORDED", events[record["event_index"]]["event_type"])
        self.assertEqual("VERIFIED_NONE", record["fork_state"])
        self.assertEqual("VERIFIED_NONE", record["nested_state"])
        evidence.verify_evidence_id(record)

    def test_duplicate_event_index_is_corrupt(self) -> None:
        event_index = self._seed_runtime_event()
        evidence.append_runtime_evidence_v2(
            self.store,
            TASK_ID,
            event_index=event_index,
            observed=_complete_observation(),
            recorded_at_utc="2026-08-28T00:00:00Z",
        )
        path = self.store._require_task(TASK_ID) / evidence.RUNTIME_EVIDENCE_V2_LEDGER
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        duplicate = dict(first)
        duplicate["recorded_at_utc"] = "2026-08-28T00:00:01Z"
        duplicate["evidence_id"] = evidence.compute_evidence_id(duplicate)
        path.write_text(
            json.dumps(first, ensure_ascii=False, separators=(",", ":"))
            + "\n"
            + json.dumps(duplicate, ensure_ascii=False, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(artifacts.WorkflowError, "EVIDENCE_LEDGER_CORRUPT"):
            evidence.replay_runtime_evidence_v2(self.store, TASK_ID)

    def test_event_index_pointing_at_non_runtime_evidence_is_corrupt(self) -> None:
        self.store.append_event(TASK_ID, {"event_type": "STATE_TRANSITION", "new_state": "TASK_VALIDATED"})
        evidence.append_runtime_evidence_v2(
            self.store,
            TASK_ID,
            event_index=len(_events(self.store, TASK_ID)) - 1,
            observed=_complete_observation(),
            recorded_at_utc="2026-08-28T00:00:00Z",
        )
        with self.assertRaisesRegex(artifacts.WorkflowError, "EVIDENCE_LEDGER_CORRUPT"):
            evidence.replay_runtime_evidence_v2(self.store, TASK_ID)

    def test_truncated_trailing_record_is_corrupt(self) -> None:
        event_index = self._seed_runtime_event()
        evidence.append_runtime_evidence_v2(
            self.store,
            TASK_ID,
            event_index=event_index,
            observed=_complete_observation(),
            recorded_at_utc="2026-08-28T00:00:00Z",
        )
        path = self.store._require_task(TASK_ID) / evidence.RUNTIME_EVIDENCE_V2_LEDGER
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        with self.assertRaisesRegex(artifacts.WorkflowError, "EVIDENCE_LEDGER_CORRUPT"):
            evidence.replay_runtime_evidence_v2(self.store, TASK_ID)

    def test_cross_task_record_is_corrupt(self) -> None:
        event_index = self._seed_runtime_event()
        evidence.append_runtime_evidence_v2(
            self.store,
            TASK_ID,
            event_index=event_index,
            observed=_complete_observation(),
            recorded_at_utc="2026-08-28T00:00:00Z",
        )
        path = self.store._require_task(TASK_ID) / evidence.RUNTIME_EVIDENCE_V2_LEDGER
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record["task_id"] = OTHER_TASK_ID
        path.write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(artifacts.WorkflowError, "EVIDENCE_LEDGER_CORRUPT"):
            evidence.replay_runtime_evidence_v2(self.store, TASK_ID)

    def test_non_object_line_is_corrupt(self) -> None:
        event_index = self._seed_runtime_event()
        evidence.append_runtime_evidence_v2(
            self.store,
            TASK_ID,
            event_index=event_index,
            observed=_complete_observation(),
            recorded_at_utc="2026-08-28T00:00:00Z",
        )
        path = self.store._require_task(TASK_ID) / evidence.RUNTIME_EVIDENCE_V2_LEDGER
        path.write_bytes(path.read_bytes() + b"[]\n")
        with self.assertRaisesRegex(artifacts.WorkflowError, "EVIDENCE_LEDGER_CORRUPT"):
            evidence.replay_runtime_evidence_v2(self.store, TASK_ID)

    def test_tampered_content_id_is_corrupt(self) -> None:
        event_index = self._seed_runtime_event()
        evidence.append_runtime_evidence_v2(
            self.store,
            TASK_ID,
            event_index=event_index,
            observed=_complete_observation(),
            recorded_at_utc="2026-08-28T00:00:00Z",
        )
        path = self.store._require_task(TASK_ID) / evidence.RUNTIME_EVIDENCE_V2_LEDGER
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record["evidence_id"] = "cafebabe" * 8
        path.write_text(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(artifacts.WorkflowError, "EVIDENCE_LEDGER_CORRUPT"):
            evidence.replay_runtime_evidence_v2(self.store, TASK_ID)


class RunCodexLaunchIntentHubTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repo = root / "repository"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "hub@example.test"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Hub Test"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
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
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def test_launch_intent_is_recorded_after_reserved_before_popen(self) -> None:
        _install_declaration(self.store, self.task, allowed_roles=("luna",), active_roles=("luna",))
        order: list[str] = []
        real_append = workflow.WorkflowStore.append_event
        real_ledger = workflow.WorkflowStore.append_task_ledger

        def tracking_append(store, task_id, event):
            order.append(str(event.get("event_type")))
            return real_append(store, task_id, event)

        def tracking_ledger(store, task_id, name, record):
            if name == policy.DISPATCH_PERMIT_LEDGER:
                order.append(f"PERMIT:{record.get('state')}")
            return real_ledger(store, task_id, name, record)

        class Popen(_RecordingPopen):
            _result = CodexRunnerTest().valid_result()

            def __init__(self, command, *args, **kwargs):
                super().__init__(command, *args, **kwargs)
                if self._delegate is None:
                    order.append("POPEN")

        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow.WorkflowStore, "append_event", tracking_append),
            mock.patch.object(workflow.WorkflowStore, "append_task_ledger", tracking_ledger),
            mock.patch.object(workflow.subprocess, "Popen", Popen),
        ):
            workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertLess(order.index("PERMIT:RESERVED"), order.index("LAUNCH_INTENT_RECORDED"))
        self.assertLess(order.index("LAUNCH_INTENT_RECORDED"), order.index("POPEN"))
        events = _intent_events(self.store, self.task_id)
        self.assertEqual(1, len(events))
        event = events[0]
        self.assertEqual(evidence.LAUNCH_INTENT_EVENT_FIELDS, set(event))
        self.assertEqual(self._permit_records()[0]["permit_id"], event["permit_id"])
        self.assertEqual(artifacts.artifact_sha256(self.task), event["envelope_hash"])
        self.assertEqual(_sha256_canonical(list(Popen.calls[0][0])), event["command_sha256"])
        self.assertEqual(preflight.LAUNCHER_VERSION, event["launcher_version"])
        self.assertEqual(preflight.compute_install_version(), event["install_version"])
        evidence.verify_launch_intent_id(event)

    def test_missing_declaration_does_not_record_launch_intent(self) -> None:
        popen = self._popen(CodexRunnerTest().valid_result())
        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_DECLARATION_MISSING"):
                workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertEqual([], _intent_events(self.store, self.task_id))
        self.assertEqual([], self._permit_records())

    def test_retired_identity_does_not_record_launch_intent(self) -> None:
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
        identity = self._permit_records()[0]["permit_id"]
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
                mock.patch.object(workflow, "derive_dispatch_identity", return_value=identity),
                mock.patch.object(workflow.subprocess, "Popen", popen),
            ):
                with self.assertRaisesRegex(workflow.WorkflowError, "DISPATCH_IDENTITY_RETIRED"):
                    workflow.run_codex("luna", self.task, "task contract", self._paths())
        self.assertEqual([], _intent_events(self.store, self.task_id))

    def test_fake_runner_records_launch_intent_without_popen(self) -> None:
        _install_declaration(
            self.store, self.task, allowed_roles=("luna", "sol_planner"), active_roles=("luna", "sol_planner")
        )
        class PipelineRunner:
            is_live_model = False

            def run(self, role, task):
                return CodexRunnerTest().valid_result(role)

        with mock.patch.object(workflow, "WORKFLOW_STATE_ROOT", self.state_root):
            workflow.run_until_gate(
                self.task_id,
                runner=PipelineRunner(),
                allow_live_model=False,
                state_root=self.state_root,
            )
        events = _intent_events(self.store, self.task_id)
        self.assertTrue(events)
        self.assertEqual(evidence.LAUNCH_INTENT_EVENT_FIELDS, set(events[0]))
        evidence.verify_launch_intent_id(events[0])
        self.assertEqual(
            _sha256_canonical(["fake-runner", "luna"]),
            events[0]["command_sha256"],
        )

    def test_runtime_evidence_recorded_field_set_is_unchanged_and_v2_sidecar_matches(self) -> None:
        _install_declaration(self.store, self.task, allowed_roles=("luna",), active_roles=("luna",))
        sessions = Path(self.temporary.name) / "sessions"
        write_exec_rollout(sessions)
        rollout_path = sessions / f"rollout-{THREAD_ID}"
        rollout = json.loads(rollout_path.read_text(encoding="utf-8"))
        rollout["cwd"] = str(self.repo.resolve())
        rollout_path.write_text(json.dumps(rollout), encoding="utf-8")
        paths = self._paths()
        paths = workflow.RunPaths(
            repo=paths.repo,
            output_path=paths.output_path,
            schema_path=paths.schema_path,
            logs_dir=paths.logs_dir,
            state_root=paths.state_root,
            runtime_evidence_required=True,
            runtime_sessions_dir=sessions,
        )

        def write_result(command, *args, **kwargs):
            attempt_output = Path(command[command.index("-o") + 1])
            attempt_output.write_text(
                json.dumps(CodexRunnerTest().valid_result()), encoding="utf-8"
            )
            payload = "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": THREAD_ID}),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 10,
                                "cached_input_tokens": 3,
                                "output_tokens": 2,
                            },
                        }
                    ),
                )
            )
            return subprocess.CompletedProcess(command, 0, stdout=payload + "\n", stderr="")

        with (
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow.subprocess, "Popen", _compat_popen(write_result)),
        ):
            workflow.run_codex("luna", self.task, "Read only.", paths)
        recorded = [
            event
            for event in _events(self.store, self.task_id)
            if event.get("event_type") == "RUNTIME_EVIDENCE_RECORDED"
        ]
        self.assertEqual(1, len(recorded))
        self.assertEqual(FROZEN_RUNTIME_EVIDENCE_RECORDED_FIELDS, set(recorded[0]))
        rows = evidence.replay_runtime_evidence_v2(self.store, self.task_id)
        self.assertEqual(1, len(rows))
        event_index = rows[0]["event_index"]
        self.assertEqual(
            "RUNTIME_EVIDENCE_RECORDED",
            _events(self.store, self.task_id)[event_index]["event_type"],
        )


class EvidenceImportDisciplineTest(unittest.TestCase):
    def test_record_launch_intent_does_not_self_lock(self) -> None:
        source = inspect.getsource(evidence.record_launch_intent)
        self.assertNotIn("store.lock(", source)

    def test_module_does_not_import_host_or_sync(self) -> None:
        tree = ast.parse(
            (ROOT / "scripts" / "ai_workflow_evidence.py").read_text(encoding="utf-8")
        )
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("ai_workflow", imported - {"ai_workflow_artifacts", "ai_workflow_declarations", "ai_workflow_preflight", "ai_workflow_evidence"})
        self.assertNotIn("ai_workflow_repairs", imported)
        self.assertNotIn("sync_plugin", imported)

    def test_sync_manifest_lists_evidence_artifacts(self) -> None:
        self.assertIn("ai_workflow_runtime_evidence_v2.schema.json", sync_plugin.CONFIG_FILES)
        self.assertIn("ai_workflow_evidence.py", sync_plugin.RUNTIME_FILES)


if __name__ == "__main__":
    unittest.main()
