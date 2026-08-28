"""Host-kernel primitives: artifacts I/O, content IDs, store ledger, lock registry."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_artifacts as artifacts


ROOT = Path(__file__).resolve().parents[1]


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


class CanonicalJsonTest(unittest.TestCase):
    def test_key_order_utf8_and_compact_separators(self):
        left = {"b": 2, "a": "中文"}
        right = {"a": "中文", "b": 2}
        encoded = artifacts.canonical_json(left)
        self.assertEqual(encoded, artifacts.canonical_json(right))
        self.assertEqual(encoded, '{"a":"中文","b":2}')
        self.assertNotIn("\\u", encoded)
        self.assertNotIn(" ", encoded)
        self.assertEqual(json.loads(encoded), {"a": "中文", "b": 2})


class ReadJsonlTest(unittest.TestCase):
    def test_missing_file_returns_empty_tuple(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.jsonl"
            self.assertEqual((), artifacts.read_jsonl(path, code="LEDGER"))

    def test_truncated_trailing_record_is_corrupt(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.jsonl"
            path.write_bytes(b'{"n":1}')
            with self.assertRaisesRegex(artifacts.WorkflowError, "LEDGER_CORRUPT"):
                artifacts.read_jsonl(path, code="LEDGER")

    def test_invalid_utf8_is_corrupt(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.jsonl"
            path.write_bytes(b"\xff\n")
            with self.assertRaisesRegex(artifacts.WorkflowError, "LEDGER_CORRUPT"):
                artifacts.read_jsonl(path, code="LEDGER")

    def test_non_object_line_is_corrupt(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.jsonl"
            path.write_text('{"n":1}\n[]\n', encoding="utf-8")
            with self.assertRaisesRegex(artifacts.WorkflowError, "LEDGER_CORRUPT"):
                artifacts.read_jsonl(path, code="LEDGER")


class ContentIdTest(unittest.TestCase):
    KIND = "demo-kind"
    EXCLUDE = frozenset({"record_id"})

    def _fields(self, **overrides: object) -> dict[str, object]:
        payload = {"record_id": "garbage", "name": "alpha", "count": 1}
        payload.update(overrides)
        return payload

    def test_non_excluded_field_change_alters_id(self):
        original = artifacts.content_id(self.KIND, self._fields(), exclude=self.EXCLUDE)
        mutated = artifacts.content_id(
            self.KIND, self._fields(name="beta"), exclude=self.EXCLUDE
        )
        self.assertNotEqual(original, mutated)

    def test_excluded_field_change_does_not_alter_id(self):
        original = artifacts.content_id(self.KIND, self._fields(), exclude=self.EXCLUDE)
        mutated = artifacts.content_id(
            self.KIND, self._fields(record_id="other-garbage"), exclude=self.EXCLUDE
        )
        self.assertEqual(original, mutated)

    def test_key_order_does_not_alter_id(self):
        left = artifacts.content_id(self.KIND, self._fields(), exclude=self.EXCLUDE)
        right = artifacts.content_id(
            self.KIND,
            {"count": 1, "record_id": "garbage", "name": "alpha"},
            exclude=self.EXCLUDE,
        )
        self.assertEqual(left, right)


class VerifyContentIdTest(unittest.TestCase):
    KIND = "demo-kind"
    EXCLUDE = frozenset({"record_id"})

    def _record(self) -> dict[str, object]:
        fields = {"record_id": "garbage", "name": "alpha", "count": 1}
        record_id = artifacts.content_id(self.KIND, fields, exclude=self.EXCLUDE)
        return {"record_id": record_id, "name": "alpha", "count": 1}

    def test_matching_record_is_accepted(self):
        artifacts.verify_content_id(
            self.KIND, self._record(), exclude=self.EXCLUDE, id_field="record_id"
        )

    def test_mutated_payload_is_rejected(self):
        record = self._record()
        record["name"] = "beta"
        with self.assertRaisesRegex(artifacts.WorkflowError, "CONTENT_ID_MISMATCH"):
            artifacts.verify_content_id(
                self.KIND, record, exclude=self.EXCLUDE, id_field="record_id"
            )

    def test_generate_and_verify_exclude_mismatch_cannot_pass(self):
        generate_exclude = frozenset({"a", "b"})
        verify_exclude = frozenset({"a"})
        fields = {"a": "garbage", "b": "secret", "c": "payload"}
        record_id = artifacts.content_id("demo-kind", fields, exclude=generate_exclude)
        record = {"a": record_id, "b": "secret", "c": "payload"}
        with self.assertRaisesRegex(artifacts.WorkflowError, "CONTENT_ID_MISMATCH"):
            artifacts.verify_content_id(
                "demo-kind", record, exclude=verify_exclude, id_field="a"
            )

    def test_id_field_not_in_exclude_is_rejected(self):
        record = self._record()
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_CONTENT_ID"):
            artifacts.verify_content_id(
                self.KIND,
                record,
                exclude=frozenset({"name"}),
                id_field="record_id",
            )


class SortedStrsTest(unittest.TestCase):
    def test_rejects_non_string_elements(self):
        with self.assertRaisesRegex(artifacts.WorkflowError, "INVALID_RECORD"):
            artifacts.sorted_strs(["ok", 1])


class ReexportTest(unittest.TestCase):
    def test_host_reexports_kernel_primitives(self):
        self.assertIs(workflow.WorkflowError, artifacts.WorkflowError)
        self.assertIs(workflow.write_json_once, artifacts.write_json_once)
        self.assertIs(workflow.append_jsonl, artifacts.append_jsonl)
        self.assertIs(workflow._canonical_json, artifacts.canonical_json)

    def test_process_generation_is_stable_hex(self):
        self.assertRegex(artifacts.PROCESS_GENERATION, r"^[0-9a-f]{32}$")
        self.assertEqual(artifacts.PROCESS_GENERATION, artifacts.PROCESS_GENERATION)


class TaskStoreProtocolTest(unittest.TestCase):
    def test_protocol_declares_store_methods(self):
        required = {
            "lock",
            "_require_task",
            "append_event",
            "write_task_artifact_once",
            "append_task_ledger",
            "read_task_ledger",
            "_assert_lock_held",
        }
        self.assertTrue(getattr(artifacts.TaskStoreProtocol, "_is_protocol", False))
        self.assertTrue(required.issubset(set(dir(artifacts.TaskStoreProtocol))))


class WorkflowStoreKernelTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary.name))
        self.task_id = "AWF-20260803-001"
        self.store.create_task(_valid_task(task_id=self.task_id))

    def tearDown(self):
        self.temporary.cleanup()

    def test_write_task_artifact_once_conflict_uses_given_code(self):
        path = self.store.write_task_artifact_once(
            self.task_id,
            "route-declaration.json",
            {"ok": True},
            conflict_code="ROUTE_DECLARATION_CONFLICT",
        )
        self.assertEqual(path.name, "route-declaration.json")
        with self.assertRaisesRegex(
            workflow.WorkflowError, "ROUTE_DECLARATION_CONFLICT"
        ):
            self.store.write_task_artifact_once(
                self.task_id,
                "route-declaration.json",
                {"ok": False},
                conflict_code="ROUTE_DECLARATION_CONFLICT",
            )

    def test_task_ledger_round_trip(self):
        self.store.append_task_ledger(
            self.task_id, "final-verdicts.jsonl", {"n": 1}
        )
        self.store.append_task_ledger(
            self.task_id, "final-verdicts.jsonl", {"n": 2}
        )
        self.assertEqual(
            ({"n": 1}, {"n": 2}),
            self.store.read_task_ledger(self.task_id, "final-verdicts.jsonl"),
        )

    def test_append_task_ledger_rejects_illegal_name(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_RECORD"):
            self.store.append_task_ledger(
                self.task_id, "Not-Valid.jsonl", {"n": 1}
            )
        with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_RECORD"):
            self.store.append_task_ledger(self.task_id, "foo.json", {"n": 1})
        with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_RECORD"):
            self.store.append_task_ledger(
                self.task_id, "foo_bar.jsonl", {"n": 1}
            )

    def test_assert_lock_held_outside_and_inside_lock(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "LOCK_REQUIRED"):
            self.store._assert_lock_held(self.task_id)
        with self.store.lock(self.task_id):
            self.store._assert_lock_held(self.task_id)

    def test_nested_lock_is_task_already_running(self):
        with self.store.lock(self.task_id):
            with self.assertRaisesRegex(
                workflow.WorkflowError, "TASK_ALREADY_RUNNING"
            ):
                with self.store.lock(self.task_id):
                    pass

    def test_nested_lock_rejected_by_held_set_even_if_flock_succeeds(self):
        with mock.patch.object(workflow.fcntl, "flock"):
            with self.store.lock(self.task_id):
                with self.assertRaisesRegex(
                    workflow.WorkflowError, "TASK_ALREADY_RUNNING"
                ):
                    with self.store.lock(self.task_id):
                        pass
                self.store._assert_lock_held(self.task_id)


if __name__ == "__main__":
    unittest.main()
