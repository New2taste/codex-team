import hashlib
import inspect
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from dataclasses import asdict
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_planning as planning
from scripts import ai_workflow_repairs as repairs
from scripts import ai_workflow_scheduler as scheduler
from scripts import sync_plugin


ROOT = Path(__file__).resolve().parents[1]


def remediation_task(*, task_id="AWF-20260803-001", candidate_commit="c" * 40):
    return {
        "schema_version": "ai-task-1",
        "task_id": task_id,
        "task_type": "REMEDIATION",
        "objective": "implement one bounded, approved repair",
        "repository_root": str(ROOT),
        "source_worktree": str(ROOT),
        "base_commit": "b" * 40,
        "candidate_commit": candidate_commit,
        "authoritative_files": ["README.md"],
        "allowed_write_paths": ["src"],
        "forbidden_actions": ["merge", "push"],
        "risk_flags": [],
        "acceptance_commands": ["python -m unittest"],
        "verification_level": "L1",
        "human_gates": ["EXECUTION_APPROVAL"],
    }


def plan_task(
    identifier,
    write_scope=None,
    depends_on=None,
    *,
    owner_role="terra",
    verification_commands=None,
):
    return {
        "id": identifier,
        "owner_role": owner_role,
        "read_scope": [],
        "write_scope": [] if write_scope is None else write_scope,
        "do_not_touch": [],
        "depends_on": [] if depends_on is None else depends_on,
        "expected_result": f"bounded result for {identifier}",
        "verification_commands": (
            ["python -m unittest"] if verification_commands is None else verification_commands
        ),
        "first_artifact": f"tests/{identifier}.py",
        "evidence_level": "L1",
    }


def valid_plan(tasks, stages=None, *, task_id="AWF-20260803-001"):
    return {
        "schema_version": "ai-plan-1",
        "plan_id": "plan-20260803-001",
        "task_id": task_id,
        "goal": "complete the bounded repair",
        "done_when": ["focused tests pass"],
        "tasks": tasks,
        "stages": stages if stages is not None else [[task["id"] for task in tasks]],
    }


def mixed_plan_document():
    return valid_plan(
        tasks=[
            plan_task("read-a", owner_role="luna"),
            plan_task("read-b", owner_role="luna"),
            plan_task("write-c", ["src/c.py"], owner_role="terra"),
            plan_task("write-d", ["src/d.py"], ["read-a", "read-b", "write-c"], owner_role="terra"),
        ],
        stages=[["read-a", "read-b", "write-c"], ["write-d"]],
    )


def slot_starvation_plan_document(*, task_id="AWF-20260803-001"):
    return valid_plan(
        tasks=[
            plan_task("a-write", ["src/a.py"], owner_role="terra"),
            plan_task("b-write", ["src/b.py"], owner_role="terra_xhigh"),
            plan_task("c-write", ["src/c.py"], owner_role="terra"),
            plan_task("d-read", owner_role="luna"),
            plan_task("e-read", owner_role="luna"),
        ],
        stages=[["a-write", "b-write", "c-write", "d-read", "e-read"]],
        task_id=task_id,
    )


def read_only_overflow_plan_document():
    return valid_plan(
        tasks=[
            plan_task("read-a", owner_role="luna"),
            plan_task("read-b", owner_role="luna"),
            plan_task("read-c", owner_role="luna"),
        ]
    )


def ledger_bytes(store, task_id):
    path = store.root / task_id / "scheduler.jsonl"
    if not path.exists():
        return b""
    return path.read_bytes()


def ledger_events(store, task_id):
    text = ledger_bytes(store, task_id).decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def canonical_event_id(event):
    payload = {key: value for key, value in event.items() if key != "event_id"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SchedulerHarness(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary_directory.name) / "state")
        self.task = remediation_task()
        self.store.create_task(self.task)
        self.frozen = workflow.validate_plan(mixed_plan_document(), self.task)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def receipt(
        self,
        proposal,
        *,
        status="IMPLEMENTED_CANDIDATE",
        frozen=None,
        write_result=True,
        **overrides,
    ):
        plan = self.frozen if frozen is None else frozen
        result = workflow.FakeRunner().run(proposal["owner_role"], self.task)
        if proposal["owner_role"] != "luna":
            result["status"] = status
        elif status != "IMPLEMENTED_CANDIDATE":
            result["status"] = {
                "NEEDS_CLARIFICATION": "PARTIALLY_SUPPORTED",
                "BLOCKED": "BLOCKED",
            }[status]
        result["changed_files"] = []
        result.update(
            {
                "dispatch_id": proposal["dispatch_id"],
                "task_id": plan.task_id,
                "step_id": proposal["subtask_id"],
                "attempt": proposal["attempt"],
            }
        )
        result_bytes = (workflow._canonical_json(result) + "\n").encode("utf-8")
        result_path = (
            self.store.root
            / plan.task_id
            / "scheduler-results"
            / f"{proposal['dispatch_id']}.json"
        )
        if write_result:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            if not result_path.exists():
                result_path.write_bytes(result_bytes)
        value = {
            "schema_version": "construction-receipt-1",
            "task_id": plan.task_id,
            "subtask_id": proposal["subtask_id"],
            "dispatch_id": proposal["dispatch_id"],
            "plan_sha256": plan.plan_sha256,
            "task_sha256": plan.task_sha256,
            "candidate_commit": plan.candidate_commit,
            "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
            "status": status,
        }
        value.update(overrides)
        return value


class SchedulerContractTest(SchedulerHarness):
    def test_schema_is_strict_plan_scheduler_1(self):
        schema = json.loads((ROOT / "config" / "ai_workflow_scheduler.schema.json").read_text())
        self.assertFalse(schema["$defs"]["receipt"]["additionalProperties"])
        self.assertEqual(
            "construction-receipt-1",
            schema["$defs"]["receipt"]["properties"]["schema_version"]["const"],
        )
        self.assertEqual(
            {"IMPLEMENTED_CANDIDATE", "NEEDS_CLARIFICATION", "BLOCKED"},
            set(schema["$defs"]["receipt"]["properties"]["status"]["enum"]),
        )
        variants = schema["oneOf"]
        self.assertEqual(4, len(variants))
        event_types = set()
        for variant in variants:
            self.assertFalse(variant["additionalProperties"])
            self.assertEqual(set(variant["properties"]), set(variant["required"]))
            self.assertEqual("plan-scheduler-1", variant["properties"]["schema_version"]["const"])
            event_types.add(variant["properties"]["event_type"]["const"])
        self.assertEqual(
            {
                "SCHEDULER_OPENED",
                "STEP_DISPATCHED",
                "STEP_RECEIPTED",
                "FINAL_ACCEPTANCE_OPENED",
            },
            event_types,
        )
        opened = next(
            item for item in variants if item["properties"]["event_type"]["const"] == "SCHEDULER_OPENED"
        )
        self.assertNotIn("receipt", opened["properties"])
        final = next(
            item
            for item in variants
            if item["properties"]["event_type"]["const"] == "FINAL_ACCEPTANCE_OPENED"
        )
        self.assertEqual("^[0-9a-f]{40}$", final["properties"]["candidate_commit"]["pattern"])
        self.assertEqual("^[0-9a-f]{64}$", final["properties"]["acceptance_task_sha256"]["pattern"])

    def test_frozen_plan_and_ready_batch_signatures_are_unchanged(self):
        self.assertEqual(
            (
                "schema_version",
                "plan_id",
                "task_id",
                "goal",
                "done_when",
                "tasks",
                "stages",
                "plan_sha256",
                "task_sha256",
                "base_commit",
                "candidate_commit",
            ),
            tuple(workflow.FrozenPlan.__dataclass_fields__),
        )
        self.assertEqual(
            ["plan", "completed", "dispatched", "capacity"],
            list(inspect.signature(workflow.ready_batch).parameters),
        )

    def test_plugin_scheduler_mirrors_are_byte_identical(self):
        pairs = (
            (
                ROOT / "config" / "ai_workflow_scheduler.schema.json",
                ROOT / "plugins" / "ai-workflow" / "config" / "ai_workflow_scheduler.schema.json",
            ),
            (
                ROOT / "scripts" / "ai_workflow_scheduler.py",
                ROOT / "plugins" / "ai-workflow" / "runtime" / "ai_workflow_scheduler.py",
            ),
        )
        for source, target in pairs:
            self.assertEqual(source.read_bytes(), target.read_bytes())
        self.assertIn("ai_workflow_scheduler.schema.json", sync_plugin.CONFIG_FILES)
        self.assertIn("ai_workflow_scheduler.py", sync_plugin.RUNTIME_FILES)


class SchedulerDispatchTest(SchedulerHarness):
    def test_receipt_requires_controller_locatable_regular_result_with_exact_hash_and_identity(self):
        proposals = scheduler.dispatch_ready_batch(self.store, self.frozen)
        proposal = next(item for item in proposals if item["subtask_id"] == "write-c")
        missing = self.receipt(proposal, write_result=False)
        original = ledger_bytes(self.store, self.frozen.task_id)
        with self.assertRaisesRegex(workflow.WorkflowError, "RECEIPT_RESULT_MISSING"):
            scheduler.record_step_receipt(self.store, self.frozen, missing)
        self.assertEqual(original, ledger_bytes(self.store, self.frozen.task_id))

        receipt = self.receipt(proposal)
        with self.assertRaisesRegex(workflow.WorkflowError, "RECEIPT_RESULT_HASH_MISMATCH"):
            scheduler.record_step_receipt(
                self.store,
                self.frozen,
                {**receipt, "result_sha256": "0" * 64},
            )
        result_path = (
            self.store.root
            / self.frozen.task_id
            / "scheduler-results"
            / f"{proposal['dispatch_id']}.json"
        )
        result_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(workflow.WorkflowError, "RECEIPT_RESULT_HASH_MISMATCH"):
            scheduler.record_step_receipt(self.store, self.frozen, receipt)

        result_path.unlink()
        outside = Path(self.temporary_directory.name) / "outside"
        outside.mkdir()
        (outside / result_path.name).write_text("{}\n", encoding="utf-8")
        result_path.parent.rmdir()
        result_path.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(workflow.WorkflowError, "RECEIPT_RESULT_UNSAFE"):
            scheduler.record_step_receipt(self.store, self.frozen, receipt)

    def test_receipt_rejects_result_role_or_status_identity_mismatch(self):
        proposal = scheduler.dispatch_ready_batch(self.store, self.frozen)[0]
        receipt = self.receipt(proposal)
        result_path = (
            self.store.root
            / self.frozen.task_id
            / "scheduler-results"
            / f"{proposal['dispatch_id']}.json"
        )
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["role"] = "terra"
        result_bytes = (workflow._canonical_json(result) + "\n").encode("utf-8")
        result_path.write_bytes(result_bytes)
        with self.assertRaisesRegex(workflow.WorkflowError, "RECEIPT_RESULT_IDENTITY_MISMATCH"):
            scheduler.record_step_receipt(
                self.store,
                self.frozen,
                {**receipt, "result_sha256": hashlib.sha256(result_bytes).hexdigest()},
            )

    def test_result_document_rejects_cross_step_identity_swap(self):
        proposals = scheduler.dispatch_ready_batch(self.store, self.frozen)
        first, second = proposals[0], proposals[1]
        result = workflow.FakeRunner().run(first["owner_role"], self.task)
        result.update(
            {
                "dispatch_id": second["dispatch_id"],
                "task_id": self.frozen.task_id,
                "step_id": second["subtask_id"],
                "attempt": second["attempt"],
            }
        )
        raw = (workflow._canonical_json(result) + "\n").encode("utf-8")
        path = (
            self.store.root
            / self.frozen.task_id
            / "scheduler-results"
            / f"{first['dispatch_id']}.json"
        )
        path.parent.mkdir()
        path.write_bytes(raw)
        forged = self.receipt(
            first,
            write_result=False,
            result_sha256=hashlib.sha256(raw).hexdigest(),
        )
        with self.assertRaisesRegex(
            workflow.WorkflowError, "RECEIPT_RESULT_IDENTITY_MISMATCH"
        ):
            scheduler.record_step_receipt(self.store, self.frozen, forged)

    def test_result_rejects_hardlinks_and_oversized_files(self):
        proposal = scheduler.dispatch_ready_batch(self.store, self.frozen)[0]
        receipt = self.receipt(proposal)
        path = (
            self.store.root
            / self.frozen.task_id
            / "scheduler-results"
            / f"{proposal['dispatch_id']}.json"
        )
        outside = Path(self.temporary_directory.name) / "hardlinked-result.json"
        path.replace(outside)
        os.link(outside, path)
        with self.assertRaisesRegex(workflow.WorkflowError, "RECEIPT_RESULT_UNSAFE"):
            scheduler.record_step_receipt(self.store, self.frozen, receipt)

        path.unlink()
        oversized = workflow.FakeRunner().run(proposal["owner_role"], self.task)
        oversized["summary"] = "x" * (1024 * 1024)
        raw = (workflow._canonical_json(oversized) + "\n").encode("utf-8")
        path.write_bytes(raw)
        with self.assertRaisesRegex(workflow.WorkflowError, "RECEIPT_RESULT_UNSAFE"):
            scheduler.record_step_receipt(
                self.store,
                self.frozen,
                {**receipt, "result_sha256": hashlib.sha256(raw).hexdigest()},
            )

    def test_result_directory_swap_during_open_is_rejected(self):
        proposal = scheduler.dispatch_ready_batch(self.store, self.frozen)[0]
        receipt = self.receipt(proposal)
        result_dir = (
            self.store.root / self.frozen.task_id / "scheduler-results"
        )
        original_dir = result_dir.with_name("scheduler-results-original")
        outside = Path(self.temporary_directory.name) / "outside-results"
        outside.mkdir()
        (outside / f"{proposal['dispatch_id']}.json").write_bytes(
            (result_dir / f"{proposal['dispatch_id']}.json").read_bytes()
        )
        real_open = os.open
        swapped = False

        def swap_then_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if not swapped and Path(path).name == f"{proposal['dispatch_id']}.json":
                swapped = True
                result_dir.rename(original_dir)
                result_dir.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(scheduler.os, "open", side_effect=swap_then_open):
            with self.assertRaisesRegex(
                workflow.WorkflowError, "RECEIPT_RESULT_UNSAFE"
            ):
                scheduler.record_step_receipt(self.store, self.frozen, receipt)

    def test_schedule_result_hashes_published_bytes_without_path_reread(self):
        proposal = scheduler.dispatch_ready_batch(self.store, self.frozen)[0]
        source = Path(self.temporary_directory.name) / "result.json"
        source.write_text(
            workflow._canonical_json(
                workflow.FakeRunner().run(proposal["owner_role"], self.task)
            )
            + "\n",
            encoding="utf-8",
        )
        plan_path = Path(self.temporary_directory.name) / "plan.json"
        plan_path.write_text(
            workflow._canonical_json(self.frozen.to_dict()) + "\n",
            encoding="utf-8",
        )
        real_publish = workflow.write_json_once
        published_bytes = b""

        def publish_then_replace(path, value, *, conflict_code):
            nonlocal published_bytes
            published_bytes = (
                workflow._canonical_json(value) + "\n"
            ).encode("utf-8")
            digest = real_publish(path, value, conflict_code=conflict_code)
            replaced = dict(value)
            replaced["summary"] = "attacker replaced the published body"
            workflow.atomic_write_json(path, replaced)
            return digest

        output = StringIO()
        with mock.patch.object(
            workflow, "write_json_once", side_effect=publish_then_replace
        ), redirect_stdout(output):
            exit_code = workflow.main(
                [
                    "schedule-result",
                    self.frozen.task_id,
                    "--plan",
                    str(plan_path),
                    "--dispatch-id",
                    proposal["dispatch_id"],
                    "--result",
                    str(source),
                    "--root",
                    str(self.store.root),
                ]
            )

        self.assertEqual(0, exit_code)
        receipt = json.loads(output.getvalue())
        self.assertEqual(
            hashlib.sha256(published_bytes).hexdigest(),
            receipt["result_sha256"],
        )
        with self.assertRaisesRegex(
            workflow.WorkflowError, "RECEIPT_RESULT_HASH_MISMATCH"
        ):
            scheduler.record_step_receipt(self.store, self.frozen, receipt)
        replay = scheduler.replay_scheduler(self.store, self.frozen)
        self.assertNotIn(proposal["subtask_id"], replay.completed)

    def test_schedule_result_remains_write_once_for_same_and_conflicting_results(self):
        proposal = scheduler.dispatch_ready_batch(self.store, self.frozen)[0]
        first = Path(self.temporary_directory.name) / "first.json"
        first_result = workflow.FakeRunner().run(proposal["owner_role"], self.task)
        first.write_text(
            workflow._canonical_json(first_result) + "\n", encoding="utf-8"
        )

        receipt = workflow._schedule_result(
            self.store, self.frozen, proposal["dispatch_id"], first
        )
        result_path = (
            self.store.root
            / self.frozen.task_id
            / "scheduler-results"
            / f"{proposal['dispatch_id']}.json"
        )
        self.assertEqual(
            hashlib.sha256(result_path.read_bytes()).hexdigest(),
            receipt["result_sha256"],
        )
        with self.assertRaisesRegex(
            workflow.WorkflowError, "RECEIPT_RESULT_CONFLICT"
        ):
            workflow._schedule_result(
                self.store, self.frozen, proposal["dispatch_id"], first
            )

        conflicting = Path(self.temporary_directory.name) / "conflicting.json"
        first_result["summary"] = "different result body"
        conflicting.write_text(
            workflow._canonical_json(first_result) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            workflow.WorkflowError, "RECEIPT_RESULT_CONFLICT"
        ):
            workflow._schedule_result(
                self.store,
                self.frozen,
                proposal["dispatch_id"],
                conflicting,
            )

    def test_dispatch_replays_ledger_then_calls_ready_batch(self):
        with mock.patch.object(scheduler, "ready_batch", wraps=planning.ready_batch) as ready:
            proposals = scheduler.dispatch_ready_batch(self.store, self.frozen)

        ready.assert_called()
        plan, completed, dispatched, capacity = ready.call_args.args
        self.assertEqual(self.frozen.plan_sha256, plan.plan_sha256)
        self.assertEqual(set(), set(completed))
        self.assertEqual(set(), set(dispatched))
        self.assertIsInstance(capacity, int)
        self.assertGreaterEqual(capacity, len(self.frozen.tasks))
        self.assertEqual(("read-a", "read-b", "write-c"), tuple(item["subtask_id"] for item in proposals))
        self.assertNotIn("write-d", {item["subtask_id"] for item in proposals})

    def test_receipt_is_required_to_complete_and_hash_drift_is_rejected(self):
        proposals = scheduler.dispatch_ready_batch(self.store, self.frozen)
        write = next(item for item in proposals if item["subtask_id"] == "write-c")
        original = ledger_bytes(self.store, self.frozen.task_id)

        with self.assertRaisesRegex(workflow.WorkflowError, "RECEIPT_IDENTITY_DRIFT|DISPATCH_IDENTITY_DRIFT"):
            scheduler.record_step_receipt(
                self.store,
                self.frozen,
                self.receipt(write, write_result=False, plan_sha256="0" * 64),
            )
        self.assertEqual(original, ledger_bytes(self.store, self.frozen.task_id))

        scheduler.record_step_receipt(
            self.store,
            self.frozen,
            self.receipt(write, status="NEEDS_CLARIFICATION"),
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "DUPLICATE_RECEIPT"):
            scheduler.record_step_receipt(
                self.store,
                self.frozen,
                self.receipt(write, status="IMPLEMENTED_CANDIDATE"),
            )
        retried = scheduler.dispatch_ready_batch(self.store, self.frozen)
        self.assertEqual(("write-c",), tuple(item["subtask_id"] for item in retried))
        self.assertEqual(2, retried[0]["attempt"])
        self.assertNotEqual(write["dispatch_id"], retried[0]["dispatch_id"])
        self.assertNotIn("write-d", {item["subtask_id"] for item in retried})

        for item in proposals:
            if item["subtask_id"] == "write-c":
                continue
            scheduler.record_step_receipt(self.store, self.frozen, self.receipt(item))
        scheduler.record_step_receipt(self.store, self.frozen, self.receipt(retried[0]))
        stage_two = scheduler.dispatch_ready_batch(self.store, self.frozen)
        self.assertEqual(("write-d",), tuple(item["subtask_id"] for item in stage_two))

    def test_stage_barrier_never_advances_while_prior_stage_is_open(self):
        first = scheduler.dispatch_ready_batch(self.store, self.frozen)
        self.assertEqual({"read-a", "read-b", "write-c"}, {item["subtask_id"] for item in first})
        second = scheduler.dispatch_ready_batch(self.store, self.frozen)
        self.assertEqual((), second)
        self.assertEqual(
            ["read-a", "read-b", "write-c"],
            [
                event["subtask_id"]
                for event in ledger_events(self.store, self.frozen.task_id)
                if event["event_type"] == "STEP_DISPATCHED"
            ],
        )

    def test_same_stage_slots_are_two_read_only_and_one_writer(self):
        overflow_frozen = workflow.validate_plan(read_only_overflow_plan_document(), self.task)
        limited = scheduler.dispatch_ready_batch(self.store, overflow_frozen)
        self.assertEqual(("read-a", "read-b"), tuple(item["subtask_id"] for item in limited))
        self.assertEqual(
            2,
            sum(1 for item in limited if item["owner_role"] in workflow.READ_ONLY_ROLES),
        )

        starvation_task = remediation_task(task_id="AWF-20260803-002")
        starvation_store = workflow.WorkflowStore(Path(self.temporary_directory.name) / "starvation")
        starvation_store.create_task(starvation_task)
        starvation_frozen = workflow.validate_plan(
            slot_starvation_plan_document(task_id=starvation_task["task_id"]),
            starvation_task,
        )
        selected = scheduler.dispatch_ready_batch(starvation_store, starvation_frozen)
        self.assertEqual(("a-write", "d-read", "e-read"), tuple(item["subtask_id"] for item in selected))
        self.assertEqual(
            1,
            sum(1 for item in selected if item["owner_role"] in {"luna_construction", "terra", "terra_xhigh"}),
        )
        self.assertEqual(2, sum(1 for item in selected if item["owner_role"] in workflow.READ_ONLY_ROLES))

    def test_each_step_gets_an_isolated_worktree_path_inside_the_repository(self):
        proposals = scheduler.dispatch_ready_batch(self.store, self.frozen)
        paths = [Path(item["worktree_path"]) for item in proposals]
        self.assertEqual(len(paths), len(set(paths)))
        root = Path(self.task["repository_root"]).resolve()
        for item, path in zip(proposals, paths):
            expected = root / ".codex-worktrees" / self.frozen.task_id.lower() / item["subtask_id"]
            self.assertEqual(expected, path)
            self.assertEqual(expected, path.resolve())
            path.resolve().relative_to(root)
            self.assertFalse(path.exists())
        self.assertFalse(any(path.exists() for path in paths))

    def test_duplicate_dispatch_identity_rejects_and_leaves_scheduler_ledger_bytes_unchanged(self):
        first = scheduler.dispatch_ready_batch(self.store, self.frozen)
        original = ledger_bytes(self.store, self.frozen.task_id)
        target = first[0]

        with self.assertRaisesRegex(workflow.WorkflowError, "STEP_IN_FLIGHT"):
            scheduler.dispatch_step(
                self.store,
                self.frozen,
                target["subtask_id"],
                attempt=target["attempt"],
            )
        self.assertEqual(original, ledger_bytes(self.store, self.frozen.task_id))

    def test_scheduler_ledger_rejects_symlink_and_hardlink_replay(self):
        scheduler.dispatch_ready_batch(self.store, self.frozen)
        path = self.store.root / self.frozen.task_id / "scheduler.jsonl"
        outside = Path(self.temporary_directory.name) / "scheduler-ledger-outside.jsonl"
        original = path.read_bytes()
        path.replace(outside)

        path.symlink_to(outside)
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_LEDGER_INVALID"):
            scheduler.replay_scheduler(self.store, self.frozen)
        self.assertEqual(original, outside.read_bytes())

        path.unlink()
        os.link(outside, path)
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_LEDGER_INVALID"):
            scheduler.replay_scheduler(self.store, self.frozen)
        self.assertEqual(original, outside.read_bytes())

    def test_scheduler_and_dispatch_ledgers_reject_linked_append_targets(self):
        task_dir = self.store.root / self.frozen.task_id
        outside_scheduler = Path(self.temporary_directory.name) / "empty-scheduler.jsonl"
        outside_scheduler.write_bytes(b"")
        (task_dir / "scheduler.jsonl").symlink_to(outside_scheduler)
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_LEDGER_INVALID"):
            scheduler.dispatch_ready_batch(self.store, self.frozen)
        self.assertEqual(b"", outside_scheduler.read_bytes())

        (task_dir / "scheduler.jsonl").unlink()
        outside_dispatch = Path(self.temporary_directory.name) / "empty-dispatch.jsonl"
        outside_dispatch.write_bytes(b"")
        (task_dir / "dispatches.jsonl").symlink_to(outside_dispatch)
        with self.assertRaisesRegex(workflow.WorkflowError, "DISPATCH_READ_ERROR"):
            scheduler.dispatch_ready_batch(self.store, self.frozen)
        self.assertEqual(b"", outside_dispatch.read_bytes())

        (task_dir / "dispatches.jsonl").unlink()
        os.link(outside_dispatch, task_dir / "dispatches.jsonl")
        with self.assertRaisesRegex(workflow.WorkflowError, "DISPATCH_READ_ERROR"):
            scheduler.dispatch_ready_batch(self.store, self.frozen)
        self.assertEqual(b"", outside_dispatch.read_bytes())

        append_path = task_dir / "append-only.jsonl"
        append_path.symlink_to(outside_dispatch)
        with self.assertRaisesRegex(workflow.WorkflowError, "APPEND_UNSAFE"):
            workflow.append_jsonl(append_path, {"event": "blocked"})
        append_path.unlink()
        os.link(outside_dispatch, append_path)
        with self.assertRaisesRegex(workflow.WorkflowError, "APPEND_UNSAFE"):
            workflow.append_jsonl(append_path, {"event": "blocked"})
        self.assertEqual(b"", outside_dispatch.read_bytes())

    def test_direct_dispatch_rejects_step_outside_current_ready_batch(self):
        original = ledger_bytes(self.store, self.frozen.task_id)
        with self.assertRaisesRegex(workflow.WorkflowError, "STEP_NOT_READY"):
            scheduler.dispatch_step(self.store, self.frozen, "write-d", attempt=1)
        self.assertEqual(original, ledger_bytes(self.store, self.frozen.task_id))

    def test_write_json_once_rejects_parent_directory_swap_before_publish(self):
        parent = Path(self.temporary_directory.name) / "artifact-parent"
        parent.mkdir()
        target = parent / "artifact.json"
        outside = Path(self.temporary_directory.name) / "artifact-outside"
        backup = Path(self.temporary_directory.name) / "artifact-parent-original"
        real_open = workflow.os.open
        swapped = False

        def swap_parent_after_open(path, flags, *args, **kwargs):
            nonlocal swapped
            descriptor = real_open(path, flags, *args, **kwargs)
            if not swapped and Path(path) == parent and flags & os.O_DIRECTORY:
                swapped = True
                outside.mkdir()
                parent.rename(backup)
                temporary = next(backup.glob(f".{target.name}.*.tmp"))
                (outside / temporary.name).write_bytes(temporary.read_bytes())
                parent.symlink_to(outside, target_is_directory=True)
            return descriptor

        with mock.patch.object(workflow.os, "open", side_effect=swap_parent_after_open):
            with self.assertRaisesRegex(workflow.WorkflowError, "ATOMIC_WRITE_FAILED"):
                workflow.write_json_once(target, {"safe": True}, conflict_code="CONFLICT")
        self.assertFalse((outside / target.name).exists())
        self.assertFalse((backup / target.name).exists())

    def test_corrupt_scheduler_history_fails_closed_without_rewrite(self):
        scheduler.dispatch_ready_batch(self.store, self.frozen)
        path = self.store.root / self.frozen.task_id / "scheduler.jsonl"
        original = b'{"event_type":"BROKEN"}\n'
        path.write_bytes(original)

        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_LEDGER_INVALID"):
            scheduler.dispatch_ready_batch(self.store, self.frozen)
        self.assertEqual(original, path.read_bytes())

    def test_final_acceptance_opens_only_after_every_implemented_receipt_and_at_most_once(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_ACCEPTANCE_NOT_READY"):
            scheduler.open_final_acceptance(
                self.store, self.frozen, "AWF-20260803-900", "d" * 40
            )
        self.assertFalse((self.store.root / "AWF-20260803-900").exists())

        proposals = scheduler.dispatch_ready_batch(self.store, self.frozen)
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_ACCEPTANCE_NOT_READY"):
            scheduler.open_final_acceptance(
                self.store, self.frozen, "AWF-20260803-900", "d" * 40
            )

        for item in proposals:
            scheduler.record_step_receipt(self.store, self.frozen, self.receipt(item))
        remaining = scheduler.dispatch_ready_batch(self.store, self.frozen)
        for item in remaining:
            scheduler.record_step_receipt(self.store, self.frozen, self.receipt(item))

        with self.assertRaisesRegex(
            workflow.WorkflowError,
            "FINAL_CANDIDATE_HEAD_MISMATCH|FINAL_CANDIDATE_NOT_ANCESTOR|FINAL_CANDIDATE_DIRTY",
        ):
            scheduler.open_final_acceptance(
                self.store, self.frozen, "AWF-20260803-900", "d" * 40
            )
        self.assertFalse((self.store.root / "AWF-20260803-900").exists())
        self.assertFalse(
            any(
                event["event_type"] == "FINAL_ACCEPTANCE_OPENED"
                for event in ledger_events(self.store, self.frozen.task_id)
            )
        )

    def test_scheduler_does_not_call_runners_or_per_step_reviewers(self):
        reviewer_roles = {
            "sol_reviewer",
            "terra_xhigh_reviewer",
            "sol_medium_reviewer",
        }
        with mock.patch.object(workflow, "run_enforced_construction") as construction, mock.patch.object(
            workflow, "_run_pipeline_role"
        ) as pipeline:
            proposals = scheduler.dispatch_ready_batch(self.store, self.frozen)
            construction.assert_not_called()
            pipeline.assert_not_called()
        self.assertTrue(proposals)
        self.assertTrue(
            {item["owner_role"] for item in proposals}.isdisjoint(reviewer_roles)
        )
        self.assertTrue(
            {
                event.get("owner_role")
                for event in ledger_events(self.store, self.frozen.task_id)
            }.isdisjoint(reviewer_roles)
        )

    def test_event_ids_are_canonical_hashes_and_dispatch_reuses_planning_identity(self):
        proposals = scheduler.dispatch_ready_batch(self.store, self.frozen)
        events = ledger_events(self.store, self.frozen.task_id)
        self.assertEqual("SCHEDULER_OPENED", events[0]["event_type"])
        self.assertEqual(self.frozen.candidate_commit, events[0]["candidate_commit"])
        previous = None
        for index, event in enumerate(events):
            self.assertEqual(index, event["event_index"])
            self.assertEqual(previous, event["previous_event_id"])
            self.assertEqual(canonical_event_id(event), event["event_id"])
            self.assertEqual("plan-scheduler-1", event["schema_version"])
            self.assertEqual(self.frozen.plan_sha256, event["plan_sha256"])
            self.assertEqual(self.frozen.task_sha256, event["task_sha256"])
            previous = event["event_id"]
        write = next(item for item in proposals if item["subtask_id"] == "write-c")
        expected = workflow.dispatch_id(
            self.frozen.plan_sha256,
            self.frozen.task_sha256,
            "write-c",
            write["attempt"],
            self.frozen.candidate_commit,
        )
        self.assertEqual(expected, write["dispatch_id"])


class SchedulerPreReviewFixTest(SchedulerHarness):
    def _single_write_frozen(self, identifier="write-c"):
        document = valid_plan(tasks=[plan_task(identifier, [f"src/{identifier}.py"])])
        return workflow.validate_plan(document, self.task)

    def test_failed_receipt_releases_attempt_and_retry_uses_new_dispatch_id(self):
        frozen = self._single_write_frozen()
        first = scheduler.dispatch_ready_batch(self.store, frozen)
        self.assertEqual(1, first[0]["attempt"])
        scheduler.record_step_receipt(
            self.store, frozen, self.receipt(first[0], status="BLOCKED", frozen=frozen)
        )
        replayed = scheduler.replay_scheduler(self.store, frozen)
        self.assertIn(first[0]["dispatch_id"], replayed.receipted_dispatch_ids)
        self.assertNotIn("write-c", replayed.dispatched)
        self.assertNotIn("write-c", replayed.in_flight)
        self.assertNotIn("write-c", replayed.completed)

        second = scheduler.dispatch_ready_batch(self.store, frozen)
        self.assertEqual(("write-c",), tuple(item["subtask_id"] for item in second))
        self.assertEqual(2, second[0]["attempt"])
        self.assertNotEqual(first[0]["dispatch_id"], second[0]["dispatch_id"])
        self.assertEqual(first[0]["worktree_path"], second[0]["worktree_path"])
        scheduler.record_step_receipt(self.store, frozen, self.receipt(second[0], frozen=frozen))
        self.assertEqual((), scheduler.dispatch_ready_batch(self.store, frozen))
        self.assertEqual({"write-c"}, scheduler.replay_scheduler(self.store, frozen).completed)

    def test_duplicate_receipt_is_rejected_and_leaves_ledger_bytes_unchanged(self):
        frozen = self._single_write_frozen()
        proposal = scheduler.dispatch_ready_batch(self.store, frozen)[0]
        scheduler.record_step_receipt(
            self.store, frozen, self.receipt(proposal, status="NEEDS_CLARIFICATION", frozen=frozen)
        )
        original = ledger_bytes(self.store, frozen.task_id)

        with self.assertRaisesRegex(workflow.WorkflowError, "DUPLICATE_RECEIPT"):
            scheduler.record_step_receipt(self.store, frozen, self.receipt(proposal, frozen=frozen))
        self.assertEqual(original, ledger_bytes(self.store, frozen.task_id))

    def test_second_attempt_ledger_replays(self):
        frozen = self._single_write_frozen()
        first = scheduler.dispatch_ready_batch(self.store, frozen)[0]
        scheduler.record_step_receipt(
            self.store, frozen, self.receipt(first, status="NEEDS_CLARIFICATION", frozen=frozen)
        )
        second = scheduler.dispatch_ready_batch(self.store, frozen)[0]
        replayed = scheduler.replay_scheduler(self.store, frozen)
        self.assertEqual(2, second["attempt"])
        self.assertIn(first["dispatch_id"], replayed.receipted_dispatch_ids)
        self.assertNotIn(second["dispatch_id"], replayed.receipted_dispatch_ids)
        self.assertEqual({"write-c"}, replayed.dispatched)
        self.assertEqual(first["worktree_path"], second["worktree_path"])

    def test_dispatch_rejected_while_in_flight_completed_or_final_opened(self):
        frozen = self._single_write_frozen()
        first = scheduler.dispatch_ready_batch(self.store, frozen)[0]
        original = ledger_bytes(self.store, frozen.task_id)
        with self.assertRaisesRegex(workflow.WorkflowError, "STEP_IN_FLIGHT"):
            scheduler.dispatch_step(self.store, frozen, "write-c", attempt=2)
        self.assertEqual(original, ledger_bytes(self.store, frozen.task_id))

        scheduler.record_step_receipt(self.store, frozen, self.receipt(first, frozen=frozen))
        completed_bytes = ledger_bytes(self.store, frozen.task_id)
        with self.assertRaisesRegex(workflow.WorkflowError, "STEP_ALREADY_COMPLETED"):
            scheduler.dispatch_step(self.store, frozen, "write-c", attempt=2)
        self.assertEqual(completed_bytes, ledger_bytes(self.store, frozen.task_id))

    def test_foreign_event_fields_are_rejected_by_replay(self):
        scheduler.dispatch_ready_batch(self.store, self.frozen)
        path = self.store.root / self.frozen.task_id / "scheduler.jsonl"
        events = ledger_events(self.store, self.frozen.task_id)
        opened = dict(events[0])
        opened["receipt"] = {"schema_version": "construction-receipt-1"}
        opened.pop("event_id")
        opened["event_id"] = canonical_event_id(opened)
        rewritten = [opened, *events[1:]]
        path.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for item in rewritten
            ),
            encoding="utf-8",
        )
        original = path.read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_LEDGER_INVALID"):
            scheduler.replay_scheduler(self.store, self.frozen)
        self.assertEqual(original, path.read_bytes())

    def test_orphan_dispatch_is_recovered_on_the_same_identity(self):
        frozen = self._single_write_frozen()
        real_append = scheduler._append_jsonl

        def fail_step_dispatched(path, record):
            if record.get("event_type") == "STEP_DISPATCHED":
                raise workflow.WorkflowError("APPEND_FAILED", "injected scheduler append failure")
            return real_append(path, record)

        with mock.patch.object(scheduler, "_append_jsonl", side_effect=fail_step_dispatched):
            with self.assertRaisesRegex(workflow.WorkflowError, "APPEND_FAILED"):
                scheduler.dispatch_step(self.store, frozen, "write-c", attempt=1)

        dispatch_lines = (
            self.store.root / frozen.task_id / "dispatches.jsonl"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(dispatch_lines))
        self.assertFalse(
            any(
                event["event_type"] == "STEP_DISPATCHED"
                for event in ledger_events(self.store, frozen.task_id)
            )
        )
        recovered = scheduler.dispatch_step(self.store, frozen, "write-c", attempt=1)
        self.assertEqual("STEP_DISPATCHED", recovered["event_type"])
        self.assertEqual(1, recovered["attempt"])
        self.assertEqual(
            1,
            len((self.store.root / frozen.task_id / "dispatches.jsonl").read_text().splitlines()),
        )
        replayed = scheduler.replay_scheduler(self.store, frozen)
        self.assertEqual({"write-c"}, replayed.dispatched)

    def test_mismatched_orphan_dispatch_is_not_swallowed(self):
        frozen = self._single_write_frozen()
        identity = workflow.dispatch_id(
            frozen.plan_sha256,
            frozen.task_sha256,
            "write-c",
            1,
            frozen.candidate_commit,
        )
        self.store.record_dispatch(
            frozen.task_id,
            identity,
            {
                "event_type": "DISPATCH_RECORDED",
                "owner_task_id": "write-c",
                "owner_role": "terra",
                "plan_sha256": frozen.plan_sha256,
                "task_sha256": frozen.task_sha256,
                "scope_sha256": "0" * 64,
                "subtask_id": "write-c",
                "attempt": 1,
                "candidate_commit": frozen.candidate_commit,
            },
        )
        original = ledger_bytes(self.store, frozen.task_id)
        with self.assertRaisesRegex(workflow.WorkflowError, "ORPHAN_DISPATCH_MISMATCH"):
            scheduler.dispatch_step(self.store, frozen, "write-c", attempt=1)
        self.assertEqual(original, ledger_bytes(self.store, frozen.task_id))

    def test_forbidden_section_roles_produce_no_proposal(self):
        for role in ("sol_planner", "sol_reviewer", "terra_xhigh_reviewer", "sol_xhigh"):
            with self.subTest(role=role):
                task = remediation_task(task_id="AWF-20260803-011")
                store = workflow.WorkflowStore(
                    Path(self.temporary_directory.name) / f"forbidden-{role}"
                )
                store.create_task(task)
                frozen = workflow.validate_plan(
                    valid_plan(
                        tasks=[plan_task("read-a", owner_role=role)],
                        task_id=task["task_id"],
                    ),
                    task,
                )
                with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_ROLE_INVALID"):
                    scheduler.dispatch_ready_batch(store, frozen)
                self.assertEqual(b"", ledger_bytes(store, task["task_id"]))

    def test_final_acceptance_requires_explicit_candidate_commit(self):
        frozen = self._single_write_frozen()
        proposal = scheduler.dispatch_ready_batch(self.store, frozen)[0]
        scheduler.record_step_receipt(self.store, frozen, self.receipt(proposal, frozen=frozen))
        with self.assertRaises(TypeError):
            scheduler.open_final_acceptance(self.store, frozen, "AWF-20260803-900")
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_CANDIDATE_INVALID"):
            scheduler.open_final_acceptance(
                self.store, frozen, "AWF-20260803-900", frozen.candidate_commit[:-1]
            )
        with self.assertRaisesRegex(
            workflow.WorkflowError,
            "FINAL_CANDIDATE_HEAD_MISMATCH|FINAL_CANDIDATE_NOT_ANCESTOR|FINAL_CANDIDATE_DIRTY",
        ):
            scheduler.open_final_acceptance(
                self.store, frozen, "AWF-20260803-900", "d" * 40
            )
        self.assertFalse((self.store.root / "AWF-20260803-900").exists())

    def test_worktree_path_rejects_case_and_injection(self):
        root = Path(self.task["repository_root"]).resolve()
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_WORKTREE_INVALID"):
            scheduler.isolated_worktree_path(str(root), "AWF-20260803-001", "Read-a")
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_WORKTREE_INVALID"):
            scheduler.isolated_worktree_path(str(root), "AWF-20260803-001", "../etc")
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_WORKTREE_INVALID"):
            scheduler.isolated_worktree_path(str(root), "AWF-20260803-001", "foo/bar")
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_WORKTREE_INVALID"):
            scheduler.isolated_worktree_path(str(root), "AWF-20260803-001", "foo\\bar")
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_WORKTREE_INVALID"):
            scheduler.isolated_worktree_path(str(root), "AWF-20260803-001", "foo\nbar")
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_WORKTREE_INVALID"):
            scheduler.isolated_worktree_path(str(root), "AWF-20260803-001", "foo\x00bar")
        outside = Path(self.temporary_directory.name) / "outside"
        repo = Path(self.temporary_directory.name) / "linked-repo"
        outside.mkdir()
        repo.mkdir()
        (repo / ".codex-worktrees").symlink_to(outside)
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_WORKTREE_INVALID"):
            scheduler.isolated_worktree_path(str(repo.resolve()), "AWF-20260803-001", "read-a")


class FinalAcceptanceCaseTest(unittest.TestCase):
    PARENT_ID = "AWF-20260803-001"
    CHILD_ID = "AWF-20260803-900"

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.repository_root = root / "repository"
        self.repository_root.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "scheduler-final@example.invalid")
        self._git("config", "user.name", "Scheduler Final Tests")
        (self.repository_root / "README.md").write_text("base\n", encoding="utf-8")
        (self.repository_root / "src").mkdir()
        (self.repository_root / "src" / "c.py").write_text("C = 1\n", encoding="utf-8")
        (self.repository_root / "src" / "d.py").write_text("D = 1\n", encoding="utf-8")
        self._git("add", "README.md", "src/c.py", "src/d.py")
        self._git("commit", "-q", "-m", "base")
        self.base_commit = self._git("rev-parse", "HEAD")
        (self.repository_root / "src" / "c.py").write_text("C = 2\n", encoding="utf-8")
        self._git("add", "src/c.py")
        self._git("commit", "-q", "-m", "candidate")
        self.candidate_commit = self._git("rev-parse", "HEAD")
        self.store = workflow.WorkflowStore(root / "state")
        self.task = {
            "schema_version": "ai-task-1",
            "task_id": self.PARENT_ID,
            "task_type": "REMEDIATION",
            "objective": "implement one bounded, approved repair",
            "repository_root": str(self.repository_root),
            "source_worktree": str(self.repository_root),
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "authoritative_files": ["README.md"],
            "allowed_write_paths": ["src"],
            "forbidden_actions": ["merge"],
            "risk_flags": [],
            "acceptance_commands": ["python -m unittest"],
            "verification_level": "L1",
            "human_gates": ["EXECUTION_APPROVAL"],
        }
        self.store.create_task(self.task)
        self.frozen = workflow.validate_plan(
            valid_plan(
                tasks=[
                    plan_task(
                        "read-a",
                        owner_role="luna",
                        verification_commands=["python -m unittest"],
                    ),
                    plan_task(
                        "write-c",
                        ["src/c.py"],
                        owner_role="terra",
                        verification_commands=["git diff --check"],
                    ),
                    plan_task(
                        "write-d",
                        ["src/d.py"],
                        ["write-c"],
                        owner_role="terra",
                        verification_commands=["python -m unittest"],
                    ),
                ],
                stages=[["read-a", "write-c"], ["write-d"]],
            ),
            self.task,
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _git(self, *args: str) -> str:
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_DATE": "2026-08-26T00:00:00Z",
                "GIT_COMMITTER_DATE": "2026-08-26T00:00:00Z",
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repository_root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def receipt(self, proposal, *, status="IMPLEMENTED_CANDIDATE", **overrides):
        result = workflow.FakeRunner().run(proposal["owner_role"], self.task)
        if proposal["owner_role"] != "luna":
            result["status"] = status
        elif status != "IMPLEMENTED_CANDIDATE":
            result["status"] = {
                "NEEDS_CLARIFICATION": "PARTIALLY_SUPPORTED",
                "BLOCKED": "BLOCKED",
            }[status]
        result["changed_files"] = []
        result.update(
            {
                "dispatch_id": proposal["dispatch_id"],
                "task_id": self.frozen.task_id,
                "step_id": proposal["subtask_id"],
                "attempt": proposal["attempt"],
            }
        )
        result_bytes = (workflow._canonical_json(result) + "\n").encode("utf-8")
        result_path = (
            self.store.root
            / self.frozen.task_id
            / "scheduler-results"
            / f"{proposal['dispatch_id']}.json"
        )
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if not result_path.exists():
            result_path.write_bytes(result_bytes)
        value = {
            "schema_version": "construction-receipt-1",
            "task_id": self.frozen.task_id,
            "subtask_id": proposal["subtask_id"],
            "dispatch_id": proposal["dispatch_id"],
            "plan_sha256": self.frozen.plan_sha256,
            "task_sha256": self.frozen.task_sha256,
            "candidate_commit": self.frozen.candidate_commit,
            "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
            "status": status,
        }
        value.update(overrides)
        return value

    def _complete_all(self):
        first = scheduler.dispatch_ready_batch(self.store, self.frozen)
        for item in first:
            scheduler.record_step_receipt(self.store, self.frozen, self.receipt(item))
        remaining = scheduler.dispatch_ready_batch(self.store, self.frozen)
        for item in remaining:
            scheduler.record_step_receipt(self.store, self.frozen, self.receipt(item))

    def _create(self, child_id=None, candidate=None):
        return scheduler.create_final_acceptance_case(
            self.store,
            self.frozen,
            child_id or self.CHILD_ID,
            candidate or self.candidate_commit,
        )

    def _child_path(self, child_id=None):
        return self.store.root / (child_id or self.CHILD_ID) / "task.json"

    def _final_events(self):
        return [
            event
            for event in ledger_events(self.store, self.frozen.task_id)
            if event["event_type"] == "FINAL_ACCEPTANCE_OPENED"
        ]

    def _owner_receipt(self, child_id=None):
        identifier = child_id or self.CHILD_ID
        assignment_id = hashlib.sha256(f"open:{identifier}".encode("utf-8")).hexdigest()
        thread_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex:owner:{identifier}"))
        evidence = {
            "schema_version": "runtime-evidence-1",
            "attempt_id": "owner-open-attempt-1",
            "requested_role": "luna_construction",
            "execution_surface": "CODEX_EXEC_ROLE_CONTRACT",
            "observed_agent_type": None,
            "native_agent_id": None,
            "native_thread_id": None,
            "observed_model": "gpt-5.6-luna",
            "observed_reasoning_effort": "max",
            "observed_sandbox_policy": "workspace-write",
            "observed_permission_profile": "workspace-write",
            "observed_cwd": str(self.repository_root),
            "evidence_source": "LOCAL_ROLLOUT",
            "observed_at_utc": "2026-08-26T00:00:00+00:00",
            "verification_status": "VERIFIED",
            "failure_reasons": [],
        }
        digest = hashlib.sha256(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return (
            repairs.VerifiedActorReceipt(
                assignment_id=assignment_id,
                execution_surface="CODEX_EXEC_ROLE_CONTRACT",
                runtime_instance_id=thread_id,
                attempt_id="owner-open-attempt-1",
                requested_role="luna_construction",
                observed_model="gpt-5.6-luna",
                observed_reasoning_effort="max",
                observed_sandbox_policy="workspace-write",
                observed_permission_profile="workspace-write",
                observed_cwd=str(self.repository_root),
                runtime_evidence_sha256=digest,
                native_agent_uuid=None,
                codex_thread_id=thread_id,
            ),
            evidence,
        )

    def _record_owner_evidence(self, child_id=None):
        identifier = child_id or self.CHILD_ID
        receipt, evidence = self._owner_receipt(identifier)
        workflow.write_runtime_evidence(self.store, identifier, evidence)
        self.store.append_event(
            identifier,
            {
                "event_type": "RUNTIME_EVIDENCE_RECORDED",
                "attempt_id": receipt.attempt_id,
                "requested_role": receipt.requested_role,
                "execution_surface": receipt.execution_surface,
                "thread_id": receipt.codex_thread_id,
            },
        )
        return receipt

    def _acceptor(self):
        runtime = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex:sol-acceptor"))
        return repairs.ActorIdentity(
            identity=f"CODEX_EXEC_ROLE_CONTRACT:{runtime}",
            role="sol_medium_reviewer",
        )

    def test_incomplete_receipts_do_not_create_child_or_final_event(self):
        first = scheduler.dispatch_ready_batch(self.store, self.frozen)
        scheduler.record_step_receipt(self.store, self.frozen, self.receipt(first[0]))
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_ACCEPTANCE_NOT_READY"):
            self._create()
        self.assertFalse(self._child_path().exists())
        self.assertEqual([], self._final_events())

    def test_inflight_step_does_not_create_child_or_final_event(self):
        scheduler.dispatch_ready_batch(self.store, self.frozen)
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_ACCEPTANCE_NOT_READY"):
            self._create()
        self.assertFalse(self._child_path().exists())
        self.assertEqual([], self._final_events())

    def test_dirty_repository_rejects_without_child_or_event(self):
        self._complete_all()
        (self.repository_root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_CANDIDATE_DIRTY|HEAD"):
            self._create()
        self.assertFalse(self._child_path().exists())
        self.assertEqual([], self._final_events())

    def test_head_mismatch_rejects_without_child_or_event(self):
        self._complete_all()
        other = "a" * 40
        self.assertNotEqual(other, self.candidate_commit)
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_CANDIDATE_HEAD_MISMATCH"):
            self._create(candidate=other)
        self.assertFalse(self._child_path().exists())
        self.assertEqual([], self._final_events())

    def test_child_fields_are_the_deterministic_projection(self):
        self._complete_all()
        child = self._create()
        self.assertEqual("ACCEPTANCE", child["task_type"])
        self.assertEqual(self.CHILD_ID, child["task_id"])
        self.assertEqual(self.frozen.goal, child["objective"])
        self.assertEqual(self.frozen.base_commit, child["base_commit"])
        self.assertEqual(self.candidate_commit, child["candidate_commit"])
        self.assertEqual(str(self.repository_root), child["repository_root"])
        self.assertEqual(str(self.repository_root), child["source_worktree"])
        self.assertEqual(["README.md"], child["authoritative_files"])
        self.assertEqual([], child["risk_flags"])
        self.assertEqual("L1", child["verification_level"])
        self.assertEqual(["src/c.py", "src/d.py"], child["allowed_write_paths"])
        self.assertEqual(["merge", "push"], child["forbidden_actions"])
        self.assertEqual(
            ["python -m unittest", "git diff --check"],
            child["acceptance_commands"],
        )
        self.assertEqual(["FINAL_ACCEPTANCE", "XHIGH_APPROVAL"], child["human_gates"])
        stored = json.loads(self._child_path().read_text(encoding="utf-8"))
        self.assertEqual(child, stored)
        events = self._final_events()
        self.assertEqual(1, len(events))
        self.assertEqual(self.CHILD_ID, events[0]["acceptance_task_id"])
        self.assertEqual(self.candidate_commit, events[0]["candidate_commit"])
        child_events = self.store.root / self.CHILD_ID / "events.jsonl"
        self.assertFalse(child_events.exists())

    def test_existing_unequal_child_without_final_event_fails_closed(self):
        self._complete_all()
        planted = {
            "schema_version": "ai-task-1",
            "task_id": self.CHILD_ID,
            "task_type": "ACCEPTANCE",
            "objective": "forged child",
            "repository_root": str(self.repository_root),
            "source_worktree": str(self.repository_root),
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "authoritative_files": ["README.md"],
            "allowed_write_paths": ["src/c.py"],
            "forbidden_actions": ["merge", "push"],
            "risk_flags": [],
            "acceptance_commands": ["python -m unittest"],
            "verification_level": "L1",
            "human_gates": ["FINAL_ACCEPTANCE", "XHIGH_APPROVAL"],
        }
        self.store.create_task(planted)
        planted_bytes = self._child_path().read_bytes()
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_ACCEPTANCE_CHILD_MISMATCH"):
            self._create()
        self.assertEqual(planted_bytes, self._child_path().read_bytes())
        self.assertEqual([], self._final_events())

    def test_event_append_failure_after_child_create_is_recoverable(self):
        self._complete_all()
        real_append = scheduler._append_jsonl

        def fail_final(path, record):
            if record.get("event_type") == "FINAL_ACCEPTANCE_OPENED":
                raise workflow.WorkflowError("APPEND_FAILED", "injected scheduler append failure")
            return real_append(path, record)

        with mock.patch.object(scheduler, "_append_jsonl", side_effect=fail_final):
            with self.assertRaisesRegex(workflow.WorkflowError, "APPEND_FAILED"):
                self._create()
        self.assertTrue(self._child_path().exists())
        original_child = self._child_path().read_bytes()
        self.assertEqual([], self._final_events())
        recovered = self._create()
        self.assertEqual(original_child, self._child_path().read_bytes())
        self.assertEqual(self.CHILD_ID, recovered["task_id"])
        self.assertEqual(1, len(self._final_events()))

    def test_identical_retry_is_idempotent_and_divergent_values_are_rejected(self):
        self._complete_all()
        first = self._create()
        ledger = ledger_bytes(self.store, self.frozen.task_id)
        child_bytes = self._child_path().read_bytes()
        second = self._create()
        self.assertEqual(first, second)
        self.assertEqual(ledger, ledger_bytes(self.store, self.frozen.task_id))
        self.assertEqual(child_bytes, self._child_path().read_bytes())
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_ACCEPTANCE"):
            self._create(child_id="AWF-20260803-901")
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_ACCEPTANCE"):
            self._create(candidate="b" * 40)
        self.assertEqual(ledger, ledger_bytes(self.store, self.frozen.task_id))
        self.assertEqual(child_bytes, self._child_path().read_bytes())
        self.assertFalse((self.store.root / "AWF-20260803-901").exists())

    def test_create_does_not_issue_review_or_call_runners(self):
        self._complete_all()
        with mock.patch.object(repairs, "issue_acceptance_assignment") as issue, mock.patch.object(
            repairs, "run_assignment", create=True
        ) as run:
            self._create()
            issue.assert_not_called()
            run.assert_not_called()
        replay = repairs.replay_acceptance_ledger(self.store, self.CHILD_ID)
        self.assertIsNone(replay)

    def test_issue_requires_recorded_owner_evidence_then_emits_one_sol_review(self):
        self._complete_all()
        child = self._create()
        acceptor = self._acceptor()
        receipt, _ = self._owner_receipt()
        with self.assertRaisesRegex(workflow.WorkflowError, "ACCEPTANCE_RECEIPT"):
            scheduler.issue_final_acceptance(
                self.store, self.frozen, child["task_id"], receipt, acceptor
            )
        owner = self._record_owner_evidence()
        with mock.patch.object(repairs, "run_assignment", create=True) as run, mock.patch.object(
            workflow, "run_assignment"
        ) as workflow_run:
            assignment = scheduler.issue_final_acceptance(
                self.store, self.frozen, child["task_id"], owner, acceptor
            )
            run.assert_not_called()
            workflow_run.assert_not_called()
        self.assertEqual("REVIEW_1", assignment.phase)
        self.assertEqual("sol_medium_reviewer", assignment.expected_actor.role)
        self.assertEqual(acceptor, assignment.expected_actor)
        replay = repairs.replay_acceptance_ledger(self.store, child["task_id"])
        issued = [
            item for item in replay.assignments.values() if item.phase == "REVIEW_1"
        ]
        self.assertEqual(1, len(issued))
        self.assertEqual(assignment.assignment_id, issued[0].assignment_id)
        with self.assertRaises(workflow.WorkflowError):
            scheduler.issue_final_acceptance(
                self.store, self.frozen, child["task_id"], owner, acceptor
            )
        replay_after = repairs.replay_acceptance_ledger(self.store, child["task_id"])
        self.assertEqual(1, len(replay_after.assignments))

    def test_wrong_actor_does_not_open_acceptance(self):
        self._complete_all()
        child = self._create()
        owner = self._record_owner_evidence()
        wrong = repairs.ActorIdentity(
            identity="CODEX_EXEC_ROLE_CONTRACT:terra-wrong",
            role="terra_xhigh_reviewer",
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "ACCEPTANCE_SEQUENCE_INVALID"):
            scheduler.issue_final_acceptance(
                self.store, self.frozen, child["task_id"], owner, wrong
            )
        child_events = self.store.root / child["task_id"] / "events.jsonl"
        if child_events.exists():
            records = [json.loads(line) for line in child_events.read_text(encoding="utf-8").splitlines()]
            self.assertFalse(
                any(
                    record.get("event_type") == "ACCEPTANCE_OPENED"
                    or record.get("ledger_version") == "adversarial-acceptance-1"
                    for record in records
                )
            )
        self.assertIsNone(repairs.replay_acceptance_ledger(self.store, child["task_id"]))

    def test_issue_assignment_append_failure_is_retryable(self):
        self._complete_all()
        child = self._create()
        owner = self._record_owner_evidence()
        acceptor = self._acceptor()
        real_append = repairs._v2_append

        def fail_issued(store, task_id, replay, context, event_type, candidate_commit, fields):
            if event_type == "ASSIGNMENT_ISSUED":
                raise workflow.WorkflowError("APPEND_FAILED", "injected assignment append failure")
            return real_append(store, task_id, replay, context, event_type, candidate_commit, fields)

        with mock.patch.object(repairs, "_v2_append", side_effect=fail_issued):
            with self.assertRaisesRegex(workflow.WorkflowError, "APPEND_FAILED"):
                scheduler.issue_final_acceptance(
                    self.store, self.frozen, child["task_id"], owner, acceptor
                )
        opened = repairs.replay_acceptance_ledger(self.store, child["task_id"])
        self.assertIsNotNone(opened)
        self.assertEqual({}, opened.assignments)
        assignment = scheduler.issue_final_acceptance(
            self.store, self.frozen, child["task_id"], owner, acceptor
        )
        self.assertEqual("REVIEW_1", assignment.phase)
        replay = repairs.replay_acceptance_ledger(self.store, child["task_id"])
        self.assertEqual(1, len(replay.assignments))

    def test_descendant_final_candidate_with_allowed_changes_succeeds(self):
        self._complete_all()
        (self.repository_root / "src" / "c.py").write_text("C = integrated\n", encoding="utf-8")
        self._git("add", "src/c.py")
        self._git("commit", "-q", "-m", "integrate")
        final = self._git("rev-parse", "HEAD")
        self.assertNotEqual(self.frozen.candidate_commit, final)
        child = self._create(candidate=final)
        self.assertEqual(final, child["candidate_commit"])
        event = self._final_events()[0]
        self.assertEqual(workflow.artifact_sha256(child), event["acceptance_task_sha256"])
        receipts = [
            item
            for item in ledger_events(self.store, self.frozen.task_id)
            if item["event_type"] == "STEP_RECEIPTED"
        ]
        self.assertEqual(receipts[-1]["event_id"], event["previous_event_id"])

    def test_non_ancestor_and_out_of_scope_commits_are_rejected(self):
        self._complete_all()
        (self.repository_root / "README.md").write_text("unrelated\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-q", "-m", "out of scope")
        out_of_scope = self._git("rev-parse", "HEAD")
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_CANDIDATE_SCOPE"):
            self._create(candidate=out_of_scope)
        self.assertFalse(self._child_path().exists())
        self.assertEqual([], self._final_events())
        self._git("reset", "--hard", self.candidate_commit)
        self._git("checkout", "--orphan", "unrelated")
        (self.repository_root / "src" / "c.py").write_text("orphan\n", encoding="utf-8")
        self._git("add", "src/c.py")
        self._git("commit", "-q", "-m", "orphan")
        orphan = self._git("rev-parse", "HEAD")
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_CANDIDATE_NOT_ANCESTOR"):
            self._create(candidate=orphan)
        self.assertFalse(self._child_path().exists())
        self.assertEqual([], self._final_events())

    def test_tampered_event_child_hash_fails_replay_and_tampered_child_fails_issue(self):
        self._complete_all()
        child = self._create()
        path = self.store.root / self.frozen.task_id / "scheduler.jsonl"
        events = ledger_events(self.store, self.frozen.task_id)
        final = dict(events[-1])
        final["acceptance_task_sha256"] = "0" * 64
        final.pop("event_id")
        final["event_id"] = canonical_event_id(final)
        rewritten = events[:-1] + [final]
        path.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for item in rewritten
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "SCHEDULER_LEDGER_INVALID"):
            scheduler.replay_scheduler(self.store, self.frozen)
        path.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for item in events
            ),
            encoding="utf-8",
        )
        stored = json.loads(self._child_path().read_text(encoding="utf-8"))
        stored["objective"] = "tampered"
        self._child_path().write_text(
            json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        owner = self._record_owner_evidence()
        with self.assertRaisesRegex(
            workflow.WorkflowError,
            "FINAL_ACCEPTANCE_CHILD_MISMATCH|SCHEDULER_LEDGER_INVALID",
        ):
            scheduler.issue_final_acceptance(
                self.store, self.frozen, child["task_id"], owner, self._acceptor()
            )

    def test_replay_rejects_missing_or_symlinked_bound_child_task(self):
        self._complete_all()
        self._create()
        task_path = self._child_path()
        original = task_path.read_bytes()
        task_path.unlink()
        with self.assertRaisesRegex(
            workflow.WorkflowError,
            "FINAL_ACCEPTANCE_CHILD_MISMATCH|SCHEDULER_LEDGER_INVALID",
        ):
            scheduler.replay_scheduler(self.store, self.frozen)

        outside = Path(self.temporary_directory.name) / "outside-child.json"
        outside.write_bytes(original)
        task_path.symlink_to(outside)
        with self.assertRaisesRegex(
            workflow.WorkflowError,
            "FINAL_ACCEPTANCE_CHILD_MISMATCH|SCHEDULER_LEDGER_INVALID",
        ):
            scheduler.replay_scheduler(self.store, self.frozen)

    def test_legacy_open_api_cannot_create_a_ghost_binding(self):
        self._complete_all()
        created = scheduler.open_final_acceptance(
            self.store, self.frozen, self.CHILD_ID, self.candidate_commit
        )
        self.assertEqual(self.CHILD_ID, created["task_id"])
        self.assertTrue(self._child_path().exists())
        self.assertEqual(1, len(self._final_events()))
        self.assertEqual(
            workflow.artifact_sha256(created),
            self._final_events()[0]["acceptance_task_sha256"],
        )

    def test_head_drift_between_create_and_append_is_rejected_and_recoverable(self):
        self._complete_all()
        real_create = self.store.create_task

        def create_then_drift(task):
            path = real_create(task)
            (self.repository_root / "src" / "c.py").write_text("C = drifted\n", encoding="utf-8")
            self._git("add", "src/c.py")
            self._git("commit", "-q", "-m", "drift")
            return path

        with mock.patch.object(self.store, "create_task", side_effect=create_then_drift):
            with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_CANDIDATE_HEAD_MISMATCH"):
                self._create()
        self.assertTrue(self._child_path().exists())
        self.assertEqual([], self._final_events())
        self._git("reset", "--hard", self.candidate_commit)
        recovered = self._create()
        self.assertEqual(self.CHILD_ID, recovered["task_id"])
        self.assertEqual(1, len(self._final_events()))

    def test_source_worktree_is_projected_to_the_bound_checkout(self):
        other = Path(self.temporary_directory.name) / "other-worktree"
        other.mkdir()
        self.task["source_worktree"] = str(other)
        drifted = workflow.WorkflowStore(Path(self.temporary_directory.name) / "state-source")
        drifted.create_task(self.task)
        frozen = workflow.validate_plan(
            valid_plan(
                tasks=[
                    plan_task("write-c", ["src/c.py"], owner_role="terra"),
                ],
                stages=[["write-c"]],
            ),
            self.task,
        )
        proposal = scheduler.dispatch_ready_batch(drifted, frozen)[0]
        result = workflow.FakeRunner().run("terra", self.task)
        result.update(
            {
                "dispatch_id": proposal["dispatch_id"],
                "task_id": frozen.task_id,
                "step_id": proposal["subtask_id"],
                "attempt": proposal["attempt"],
            }
        )
        result_bytes = (workflow._canonical_json(result) + "\n").encode("utf-8")
        result_path = (
            drifted.root
            / frozen.task_id
            / "scheduler-results"
            / f"{proposal['dispatch_id']}.json"
        )
        result_path.parent.mkdir()
        result_path.write_bytes(result_bytes)
        scheduler.record_step_receipt(
            drifted,
            frozen,
            {
                "schema_version": "construction-receipt-1",
                "task_id": frozen.task_id,
                "subtask_id": proposal["subtask_id"],
                "dispatch_id": proposal["dispatch_id"],
                "plan_sha256": frozen.plan_sha256,
                "task_sha256": frozen.task_sha256,
                "candidate_commit": frozen.candidate_commit,
                "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                "status": "IMPLEMENTED_CANDIDATE",
            },
        )
        child = scheduler.create_final_acceptance_case(
            drifted, frozen, self.CHILD_ID, self.candidate_commit
        )
        self.assertEqual(str(self.repository_root), child["repository_root"])
        self.assertTrue(child["source_worktree"] in {None, str(self.repository_root)})
        self.assertNotEqual(str(other), child["source_worktree"])

    def test_final_event_schema_requires_child_hash(self):
        schema = json.loads((ROOT / "config" / "ai_workflow_scheduler.schema.json").read_text())
        final = next(
            item
            for item in schema["oneOf"]
            if item["properties"]["event_type"]["const"] == "FINAL_ACCEPTANCE_OPENED"
        )
        self.assertEqual("^[0-9a-f]{64}$", final["properties"]["acceptance_task_sha256"]["pattern"])
        self.assertIn("acceptance_task_sha256", final["required"])

    def test_dispatch_is_rejected_after_final_acceptance_child_is_bound(self):
        self._complete_all()
        self._create()
        with self.assertRaisesRegex(workflow.WorkflowError, "FINAL_ACCEPTANCE_ALREADY_OPEN"):
            scheduler.dispatch_step(self.store, self.frozen, "write-c", attempt=3)

    def test_scheduler_cli_drives_batch_results_receipts_and_first_final_review(self):
        task_path = self.store.root / self.PARENT_ID / "task.json"
        plan_path = Path(self.temporary_directory.name) / "plan.json"
        plan_path.write_text(
            workflow._canonical_json(self.frozen.to_dict()) + "\n",
            encoding="utf-8",
        )

        while True:
            output = StringIO()
            with redirect_stdout(output):
                exit_code = workflow.main(
                    [
                        "schedule-batch",
                        "--task",
                        str(task_path),
                        "--plan",
                        str(plan_path),
                        "--root",
                        str(self.store.root),
                    ]
                )
            self.assertEqual(0, exit_code)
            proposals = json.loads(output.getvalue())
            if not proposals:
                break
            for proposal in proposals:
                result = workflow.FakeRunner().run(proposal["owner_role"], self.task)
                source = (
                    Path(self.temporary_directory.name)
                    / f"{proposal['dispatch_id']}-result.json"
                )
                source.write_text(
                    workflow._canonical_json(result) + "\n",
                    encoding="utf-8",
                )
                result_output = StringIO()
                with redirect_stdout(result_output):
                    self.assertEqual(
                        0,
                        workflow.main(
                            [
                                "schedule-result",
                                self.PARENT_ID,
                                "--plan",
                                str(plan_path),
                                "--dispatch-id",
                                proposal["dispatch_id"],
                                "--result",
                                str(source),
                                "--root",
                                str(self.store.root),
                            ]
                        ),
                    )
                receipt = json.loads(result_output.getvalue())
                receipt_path = (
                    Path(self.temporary_directory.name)
                    / f"{proposal['dispatch_id']}-receipt.json"
                )
                receipt_path.write_text(
                    workflow._canonical_json(receipt) + "\n",
                    encoding="utf-8",
                )
                with redirect_stdout(StringIO()):
                    self.assertEqual(
                        0,
                        workflow.main(
                            [
                                "schedule-receipt",
                                self.PARENT_ID,
                                "--plan",
                                str(plan_path),
                                "--receipt",
                                str(receipt_path),
                                "--root",
                                str(self.store.root),
                            ]
                        ),
                    )

        with redirect_stdout(StringIO()):
            self.assertEqual(
                0,
                workflow.main(
                    [
                        "schedule-final",
                        self.PARENT_ID,
                        "--plan",
                        str(plan_path),
                        "--acceptance-task-id",
                        self.CHILD_ID,
                        "--candidate-commit",
                        self.candidate_commit,
                        "--root",
                        str(self.store.root),
                    ]
                ),
            )
        owner = self._record_owner_evidence()
        owner_path = Path(self.temporary_directory.name) / "owner-receipt.json"
        owner_path.write_text(
            workflow._canonical_json(asdict(owner)) + "\n",
            encoding="utf-8",
        )
        acceptor = self._acceptor()
        acceptor_path = Path(self.temporary_directory.name) / "acceptor.json"
        acceptor_path.write_text(
            workflow._canonical_json(asdict(acceptor)) + "\n",
            encoding="utf-8",
        )
        issue_output = StringIO()
        with redirect_stdout(issue_output):
            issue_exit = workflow.main(
                [
                    "schedule-final",
                    self.PARENT_ID,
                    "--plan",
                    str(plan_path),
                    "--acceptance-task-id",
                    self.CHILD_ID,
                    "--candidate-commit",
                    self.candidate_commit,
                    "--owner-receipt",
                    str(owner_path),
                    "--acceptor",
                    str(acceptor_path),
                    "--root",
                    str(self.store.root),
                ]
            )
        self.assertEqual(0, issue_exit, issue_output.getvalue())
        self.assertEqual("REVIEW_1", json.loads(issue_output.getvalue())["phase"])


if __name__ == "__main__":
    unittest.main()
