import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import ai_workflow as workflow


ROOT = Path(__file__).resolve().parents[1]


def remediation_task(allowed_write_paths=None, *, candidate_commit="c" * 40):
    return {
        "schema_version": "ai-task-1",
        "task_id": "AWF-20260803-001",
        "task_type": "REMEDIATION",
        "objective": "implement one bounded, approved repair",
        "repository_root": str(ROOT),
        "source_worktree": str(ROOT),
        "base_commit": "b" * 40,
        "candidate_commit": candidate_commit,
        "authoritative_files": ["README.md"],
        "allowed_write_paths": ["src"] if allowed_write_paths is None else allowed_write_paths,
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
    read_scope=None,
    do_not_touch=None,
    construction_envelope=None,
):
    task = {
        "id": identifier,
        "owner_role": owner_role,
        "read_scope": [] if read_scope is None else read_scope,
        "write_scope": [] if write_scope is None else write_scope,
        "do_not_touch": [] if do_not_touch is None else do_not_touch,
        "depends_on": [] if depends_on is None else depends_on,
        "expected_result": f"bounded result for {identifier}",
        "verification_commands": ["python -m unittest"],
        "first_artifact": f"tests/{identifier}.py",
        "evidence_level": "L1",
    }
    if construction_envelope is not None:
        task["construction_envelope"] = construction_envelope
    return task


def luna_construction_envelope(paths):
    return {
        "allowed_paths": paths,
        "done_when": ["the bounded implementation test passes"],
        "evidence": {
            "L0": ["inspect the source path"],
            "L1": ["run the focused unit test"],
            "L2": ["inspect the candidate diff"],
        },
        "negative_checks": ["remove the expected behavior and observe the test fail"],
    }


def valid_plan(tasks=None, stages=None):
    selected_tasks = [plan_task("task-a", ["src/a.py"])] if tasks is None else tasks
    return {
        "schema_version": "ai-plan-1",
        "plan_id": "plan-20260803-001",
        "task_id": "AWF-20260803-001",
        "goal": "complete the bounded repair",
        "done_when": ["focused tests pass"],
        "tasks": selected_tasks,
        "stages": [[task["id"] for task in selected_tasks]] if stages is None else stages,
    }


class PlanScopeValidationTest(unittest.TestCase):
    def test_luna_execution_context_revalidates_the_exact_envelope_step(self):
        plan = valid_plan(
            tasks=[
                plan_task(
                    "task-a",
                    ["src/a.py"],
                    owner_role="luna_construction",
                    construction_envelope=luna_construction_envelope(["src/a.py"]),
                )
            ]
        )

        selected = workflow.require_luna_construction_step(
            plan, remediation_task(), "task-a"
        )

        self.assertEqual("task-a", selected.id)
        self.assertEqual(("src/a.py",), selected.write_scope)

    def test_luna_construction_owner_requires_a_complete_local_envelope(self):
        plan = valid_plan(
            tasks=[
                plan_task(
                    "task-a",
                    ["src/a.py"],
                    owner_role="luna_construction",
                    construction_envelope=luna_construction_envelope(["src/a.py"]),
                )
            ]
        )

        frozen = workflow.validate_plan(plan, remediation_task())

        self.assertEqual("luna_construction", frozen.tasks[0].owner_role)
        self.assertEqual(("src/a.py",), frozen.tasks[0].construction_envelope.allowed_paths)

    def test_luna_construction_owner_rejects_missing_mutation_evidence(self):
        envelope = luna_construction_envelope(["src/a.py"])
        del envelope["negative_checks"]
        plan = valid_plan(
            tasks=[
                plan_task(
                    "task-a",
                    ["src/a.py"],
                    owner_role="luna_construction",
                    construction_envelope=envelope,
                )
            ]
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            workflow.validate_plan(plan, remediation_task())

    def test_each_new_terra_os_role_is_a_valid_frozen_plan_owner(self):
        for role in (
            "sol_medium_supervisor",
            "terra_xhigh",
            "sol_medium_reviewer",
            "sol_xhigh_planner",
        ):
            with self.subTest(role=role):
                frozen = workflow.validate_plan(
                    valid_plan(tasks=[plan_task("task-a", [], owner_role=role)]),
                    remediation_task(),
                )

                self.assertEqual(role, frozen.tasks[0].owner_role)

    def test_valid_plan_freezes_scope_ownership_by_subtask_id(self):
        plan = valid_plan(
            tasks=[
                plan_task("task-a", ["src/a.py"], owner_role="terra"),
                plan_task("task-b", ["src/b.py"], owner_role="terra"),
            ]
        )

        frozen = workflow.validate_plan(plan, remediation_task())

        self.assertIsInstance(frozen, workflow.FrozenPlan)
        self.assertEqual("task-a", workflow.scope_owner_map(frozen)["src/a.py"])
        self.assertEqual("task-b", workflow.scope_owner_map(frozen)["src/b.py"])
        self.assertEqual(workflow.artifact_sha256(plan), frozen.plan_sha256)

    def test_parent_child_write_scopes_overlap(self):
        plan = valid_plan(
            tasks=[plan_task("a", ["src"]), plan_task("b", ["src/api.py"])]
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "SCOPE_OVERLAP"):
            workflow.validate_plan(plan, remediation_task(["src"]))

    def test_all_scope_kinds_reject_absolute_traversal_glob_and_empty_paths(self):
        for field in ("read_scope", "write_scope", "do_not_touch"):
            for path in ("/tmp/x", "../x", "src/*.py", ""):
                with self.subTest(field=field, path=path):
                    task = plan_task("a", ["src/a.py"])
                    task[field] = [path]
                    plan = valid_plan(tasks=[task])
                    with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
                        workflow.validate_plan(plan, remediation_task(["src"]))

    def test_write_scope_must_stay_within_parent_allowed_paths(self):
        plan = valid_plan(tasks=[plan_task("a", ["tests/test_a.py"])])

        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            workflow.validate_plan(plan, remediation_task(["src"]))

    def test_same_exact_scope_cannot_have_two_subtask_owners(self):
        plan = valid_plan(
            tasks=[plan_task("a", ["src/a.py"]), plan_task("b", ["src/a.py"])]
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "OWNER_CONFLICT"):
            workflow.validate_plan(plan, remediation_task())

    def test_subtask_cannot_write_its_own_do_not_touch_scope(self):
        plan = valid_plan(
            tasks=[plan_task("a", ["src/a.py"], do_not_touch=["src"])]
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            workflow.validate_plan(plan, remediation_task())


class PlanGraphValidationTest(unittest.TestCase):
    def test_cycle_is_rejected(self):
        plan = valid_plan(
            tasks=[plan_task("a", [], ["b"]), plan_task("b", [], ["a"])],
            stages=[["a"], ["b"]],
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_CYCLE"):
            workflow.validate_plan(plan, remediation_task(["src"]))

    def test_dependency_must_be_in_an_earlier_stage(self):
        plan = valid_plan(
            tasks=[plan_task("a"), plan_task("b", depends_on=["a"])],
            stages=[["a", "b"]],
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            workflow.validate_plan(plan, remediation_task(["src"]))

    def test_stages_must_cover_each_declared_task_once(self):
        plan = valid_plan(
            tasks=[plan_task("a"), plan_task("b")], stages=[["a"], ["a"]]
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "PLAN_INVALID"):
            workflow.validate_plan(plan, remediation_task(["src"]))


class ReadyBatchTest(unittest.TestCase):
    def setUp(self):
        self.frozen = workflow.validate_plan(
            valid_plan(
                tasks=[
                    plan_task("b", ["src/b.py"]),
                    plan_task("a", ["src/a.py"]),
                    plan_task("c", ["src/c.py"], ["a", "b"]),
                ],
                stages=[["b", "a"], ["c"]],
            ),
            remediation_task(),
        )

    def test_capacity_limits_sorted_current_stage_tasks(self):
        self.assertEqual(("a",), workflow.ready_batch(self.frozen, set(), set(), 1))
        self.assertEqual(("a", "b"), workflow.ready_batch(self.frozen, set(), set(), 2))
        self.assertEqual(("b",), workflow.ready_batch(self.frozen, set(), {"a"}, 2))

    def test_future_stage_waits_for_completed_prior_stage_dependencies(self):
        self.assertEqual(("b",), workflow.ready_batch(self.frozen, {"a"}, set(), 2))
        self.assertEqual(("c",), workflow.ready_batch(self.frozen, {"a", "b"}, set(), 2))

    def test_zero_capacity_returns_empty_and_invalid_capacity_fails_closed(self):
        self.assertEqual((), workflow.ready_batch(self.frozen, set(), set(), 0))
        for capacity in (-1, True, "1"):
            with self.subTest(capacity=capacity), self.assertRaisesRegex(
                workflow.WorkflowError, "CAPACITY_UNAVAILABLE"
            ):
                workflow.ready_batch(self.frozen, set(), set(), capacity)


class DispatchIdentityTest(unittest.TestCase):
    def setUp(self):
        self.task = remediation_task()
        self.plan = valid_plan(tasks=[plan_task("task-a", ["src/a.py"])])
        self.frozen = workflow.validate_plan(self.plan, self.task)
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = workflow.WorkflowStore(Path(self.temporary_directory.name) / "state")
        self.store.create_task(self.task)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_dispatch_id_hashes_exactly_the_five_canonical_fields(self):
        value = {
            "plan_sha256": "p" * 64,
            "task_sha256": "t" * 64,
            "subtask_id": "task-a",
            "attempt": 1,
            "candidate_commit": "c" * 40,
        }
        expected = hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

        self.assertEqual(
            expected,
            workflow.dispatch_id("p" * 64, "t" * 64, "task-a", 1, "c" * 40),
        )

    def test_same_dispatch_identity_is_not_launched_twice(self):
        identity = workflow.dispatch_id("p" * 64, "t" * 64, "task-a", 1, "c" * 40)
        payload = {"event_type": "DISPATCH_RECORDED", "subtask_id": "task-a", "attempt": 1}
        self.store.record_dispatch(self.task["task_id"], identity, payload)

        with self.assertRaisesRegex(workflow.WorkflowError, "DUPLICATE_DISPATCH"):
            self.store.record_dispatch(self.task["task_id"], identity, payload)

    def test_record_dispatch_binds_frozen_plan_owner_scope_and_candidate(self):
        identity = workflow.record_dispatch(
            self.store,
            self.task["task_id"],
            self.frozen,
            "task-a",
            1,
            "c" * 40,
        )

        records = [
            json.loads(line)
            for line in (
                self.store.root / self.task["task_id"] / "dispatches.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(identity, records[0]["dispatch_id"])
        self.assertEqual("task-a", records[0]["owner_task_id"])
        self.assertEqual("terra", records[0]["owner_role"])
        self.assertEqual(self.frozen.plan_sha256, records[0]["plan_sha256"])
        self.assertEqual(self.frozen.task_sha256, records[0]["task_sha256"])
        self.assertIn("scope_sha256", records[0])
        self.assertEqual("c" * 40, records[0]["candidate_commit"])

        with self.assertRaisesRegex(workflow.WorkflowError, "DUPLICATE_DISPATCH"):
            workflow.record_dispatch(
                self.store,
                self.task["task_id"],
                self.frozen,
                "task-a",
                1,
                "c" * 40,
            )

    def test_dispatch_candidate_must_match_the_frozen_parent_task(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "DISPATCH_IDENTITY_DRIFT"):
            workflow.record_dispatch(
                self.store,
                self.task["task_id"],
                self.frozen,
                "task-a",
                1,
                "d" * 40,
            )

    def test_dispatch_requires_a_nonempty_frozen_candidate_commit(self):
        task = remediation_task(candidate_commit=None)
        plan = valid_plan(tasks=[plan_task("task-a", ["src/a.py"])])
        frozen = workflow.validate_plan(plan, task)
        with tempfile.TemporaryDirectory() as temporary:
            store = workflow.WorkflowStore(Path(temporary) / "state")
            store.create_task(task)

            with self.assertRaisesRegex(workflow.WorkflowError, "DISPATCH_IDENTITY_DRIFT"):
                workflow.record_dispatch(
                    store,
                    task["task_id"],
                    frozen,
                    "task-a",
                    1,
                    "c" * 40,
                )

    def test_invalid_historical_dispatch_id_blocks_append_without_rewriting_ledger(self):
        ledger = self.store.root / self.task["task_id"] / "dispatches.jsonl"
        original = '{"dispatch_id":"INVALID-HISTORY"}\n'
        ledger.write_text(original, encoding="utf-8")
        identity = workflow.dispatch_id("p" * 64, "t" * 64, "task-a", 1, "c" * 40)

        with self.assertRaisesRegex(workflow.WorkflowError, "DISPATCH_IDENTITY_DRIFT"):
            self.store.record_dispatch(
                self.task["task_id"],
                identity,
                {"event_type": "DISPATCH_RECORDED", "subtask_id": "task-a", "attempt": 1},
            )

        self.assertEqual(original, ledger.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
