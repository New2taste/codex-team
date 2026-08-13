import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "runtime"
INSPECTOR = ROOT / "plugins" / "ai-workflow" / "scripts" / "inspect-agent-runtime.sh"
THREAD_ID = "019fc73c-4d40-7c20-a82a-c5a9ae078bcf"


def runtime_expected(*, surface="NATIVE_SUBAGENT", **overrides):
    value = {
        "attempt_id": "runtime-attempt-1",
        "requested_role": "luna",
        "execution_surface": surface,
        "agent_type": "luna_max" if surface == "NATIVE_SUBAGENT" else None,
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "sandbox_policy": "read-only",
        "permission_profile": "read-only",
        "cwd": str(ROOT),
        "evidence_source": "NATIVE_METADATA"
        if surface == "NATIVE_SUBAGENT"
        else "LOCAL_ROLLOUT",
        "hard_read_only": True,
    }
    value.update(overrides)
    return value


def runtime_observation(*, surface="NATIVE_SUBAGENT", **overrides):
    value = {
        "execution_surface": surface,
        "agent_type": "luna_max" if surface == "NATIVE_SUBAGENT" else None,
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
        "sandbox_policy": "read-only",
        "permission_profile": "read-only",
        "cwd": str(ROOT),
        "evidence_source": "NATIVE_METADATA"
        if surface == "NATIVE_SUBAGENT"
        else "LOCAL_ROLLOUT",
    }
    value.update(overrides)
    return value


def valid_task():
    return {
        "schema_version": "ai-task-1",
        "task_id": "AWF-20260803-401",
        "task_type": "PLAN",
        "objective": "record runtime evidence",
        "repository_root": str(ROOT),
        "source_worktree": None,
        "base_commit": None,
        "candidate_commit": None,
        "authoritative_files": ["README.md"],
        "allowed_write_paths": [],
        "forbidden_actions": ["merge"],
        "risk_flags": [],
        "acceptance_commands": [],
        "verification_level": "L1",
        "human_gates": ["PLAN_APPROVAL"],
    }


def blocked_luna_result():
    return {
        "schema_version": "ai-result-1",
        "role": "luna",
        "status": "BLOCKED",
        "summary": "Runtime evidence is available.",
        "claims": [],
        "evidence": [],
        "counter_checks": [],
        "changed_files": [],
        "blind_spots": [],
        "unresolved_questions": [],
        "recommended_next_state": "BLOCKED",
    }


def write_exec_rollout(sessions: Path, *, model="gpt-5.6-luna", agent_type=None):
    sessions.mkdir(parents=True, exist_ok=True)
    rollout = {
        "thread_id": THREAD_ID,
        "agent_type": agent_type,
        "model": model,
        "reasoning_effort": "max",
        "sandbox_policy": "read-only",
        "permission_profile": "read-only",
        "cwd": str(ROOT),
        "prompt": "PROMPT_SECRET",
        "environment": "ENV_SECRET",
        "token": "TOKEN_SECRET",
    }
    (sessions / f"rollout-{THREAD_ID}").write_text(json.dumps(rollout), encoding="utf-8")


class RuntimeIdentityTest(unittest.TestCase):
    def test_each_pinned_terra_os_role_has_a_verifiable_runtime_identity(self):
        for role in (
            "luna_construction",
            "sol_medium_supervisor",
            "terra_xhigh",
            "sol_medium_reviewer",
            "sol_xhigh_planner",
        ):
            with self.subTest(role=role):
                pinned = workflow._load_role_config(role)
                expected = runtime_expected(
                    surface="CODEX_EXEC_ROLE_CONTRACT",
                    requested_role=role,
                    model=pinned["model"],
                    reasoning_effort=pinned["reasoning_effort"],
                    sandbox_policy=pinned["sandbox"],
                    permission_profile=pinned["sandbox"],
                )
                observed = runtime_observation(
                    surface="CODEX_EXEC_ROLE_CONTRACT",
                    model=pinned["model"],
                    reasoning_effort=pinned["reasoning_effort"],
                    sandbox_policy=pinned["sandbox"],
                    permission_profile=pinned["sandbox"],
                )

                evidence = workflow.verify_runtime_identity(expected, observed)

                self.assertEqual("VERIFIED", evidence.verification_status)
                self.assertEqual(role, evidence.requested_role)

    def test_every_native_identity_field_is_required(self):
        # Removing any one identity fact must stop native-agent verification.
        for field in (
            "agent_type",
            "model",
            "reasoning_effort",
            "sandbox_policy",
            "permission_profile",
            "cwd",
        ):
            observed = runtime_observation()
            observed[field] = None
            with self.subTest(field=field), self.assertRaisesRegex(
                workflow.WorkflowError, "RUNTIME_IDENTITY_MISSING"
            ):
                workflow.verify_runtime_identity(runtime_expected(), observed)

    def test_exec_surface_must_not_claim_a_custom_agent_type(self):
        observed = runtime_observation(
            surface="CODEX_EXEC_ROLE_CONTRACT", agent_type="luna_max"
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_IDENTITY_CONFLICT"):
            workflow.verify_runtime_identity(
                runtime_expected(surface="CODEX_EXEC_ROLE_CONTRACT"), observed
            )

    def test_exec_evidence_has_null_agent_type_and_never_claims_native_luna(self):
        evidence = workflow.verify_runtime_identity(
            runtime_expected(surface="CODEX_EXEC_ROLE_CONTRACT"),
            runtime_observation(surface="CODEX_EXEC_ROLE_CONTRACT"),
        )
        self.assertEqual("CODEX_EXEC_ROLE_CONTRACT", evidence.execution_surface)
        self.assertIsNone(evidence.observed_agent_type)
        self.assertEqual("luna", evidence.requested_role)

    def test_model_effort_and_cwd_are_exact_identity_matches(self):
        for field, value in (
            ("model", "gpt-5.6-sol"),
            ("reasoning_effort", "xhigh"),
            ("cwd", "/different/worktree"),
        ):
            observed = runtime_observation(**{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(
                workflow.WorkflowError, "RUNTIME_IDENTITY_CONFLICT"
            ):
                workflow.verify_runtime_identity(runtime_expected(), observed)

    def test_permissions_can_only_narrow_from_the_requested_contract(self):
        expected = runtime_expected(
            sandbox_policy="workspace-write", permission_profile="workspace-write"
        )
        narrowed = runtime_observation(
            sandbox_policy="read-only", permission_profile="read-only"
        )
        self.assertEqual(
            "VERIFIED", workflow.verify_runtime_identity(expected, narrowed).verification_status
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_PERMISSION_MISMATCH"):
            workflow.verify_runtime_identity(
                runtime_expected(),
                runtime_observation(
                    sandbox_policy="workspace-write",
                    permission_profile="workspace-write",
                ),
            )

    def test_broadened_reviewer_needs_opt_in_prompt_guard_and_unchanged_snapshots(self):
        expected = runtime_expected(requested_role="sol_reviewer", hard_read_only=False)
        guarded = runtime_observation(
            sandbox_policy="workspace-write",
            permission_profile="workspace-write",
            controller_prompt_forbids_writes=True,
            before_repository_snapshot=workflow.RuntimeRepositorySnapshot("a", ()),
            after_repository_snapshot=workflow.RuntimeRepositorySnapshot("a", ()),
            before_artifact_snapshot=workflow.RuntimeArtifactSnapshot(
                (("task.json", "a" * 64),)
            ),
            after_artifact_snapshot=workflow.RuntimeArtifactSnapshot(
                (("task.json", "a" * 64),)
            ),
        )
        self.assertEqual(
            "VERIFIED", workflow.verify_runtime_identity(expected, guarded).verification_status
        )
        guarded["after_artifact_snapshot"] = {"task": "changed"}
        with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_PERMISSION_MISMATCH"):
            workflow.verify_runtime_identity(expected, guarded)

    def test_conflicting_public_and_rollout_values_fail_closed(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_IDENTITY_CONFLICT"):
            workflow.merge_runtime_observations(
                runtime_observation(model="gpt-5.6-luna"),
                runtime_observation(model="gpt-5.6-sol"),
            )

    def test_conflicting_canonical_and_observed_aliases_fail_closed(self):
        observed = runtime_observation(observed_model="gpt-5.6-sol")
        with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_IDENTITY_CONFLICT"):
            workflow.verify_runtime_identity(runtime_expected(), observed)

    def test_exec_rejects_a_nonnull_observed_agent_type_alias(self):
        observed = runtime_observation(
            surface="CODEX_EXEC_ROLE_CONTRACT",
            observed_agent_type="luna_max",
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_IDENTITY_CONFLICT"):
            workflow.verify_runtime_identity(
                runtime_expected(surface="CODEX_EXEC_ROLE_CONTRACT"), observed
            )

    def test_native_metadata_and_rollout_can_complete_one_consistent_identity(self):
        native = {
            "execution_surface": "NATIVE_SUBAGENT",
            "agent_type": "luna_max",
            "model": "gpt-5.6-luna",
            "sandbox_policy": "read-only",
            "permission_profile": "read-only",
            "cwd": str(ROOT),
            "evidence_source": "NATIVE_METADATA",
        }
        rollout = {
            "execution_surface": "NATIVE_SUBAGENT",
            "model": "gpt-5.6-luna",
            "reasoning_effort": "max",
            "evidence_source": "LOCAL_ROLLOUT",
        }
        merged = workflow.merge_runtime_observations(native, rollout)
        evidence = workflow.verify_runtime_identity(runtime_expected(), merged)
        self.assertEqual("LOCAL_ROLLOUT", evidence.evidence_source)
        self.assertEqual(
            ("LOCAL_ROLLOUT", "NATIVE_METADATA"), merged.evidence_sources
        )

    def test_native_luna_worker_alias_is_rejected(self):
        observed = runtime_observation(agent_type="luna_worker")
        with self.assertRaisesRegex(
            workflow.WorkflowError, "RUNTIME_IDENTITY_CONFLICT"
        ):
            workflow.verify_runtime_identity(runtime_expected(), observed)

    def test_broadened_exception_is_limited_to_read_only_reviewer_with_real_snapshots(self):
        expected = runtime_expected(
            requested_role="terra",
            hard_read_only=False,
        )
        observed = runtime_observation(
            sandbox_policy="workspace-write",
            permission_profile="workspace-write",
            prompt_forbids_writes=True,
            before_repository_snapshot={},
            after_repository_snapshot={},
            before_artifact_snapshot={},
            after_artifact_snapshot={},
        )
        with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_PERMISSION_MISMATCH"):
            workflow.verify_runtime_identity(expected, observed)


class RuntimeUsageTest(unittest.TestCase):
    def test_turn_completed_usage_is_recorded_without_estimation(self):
        events = [
            {"type": "thread.started", "thread_id": THREAD_ID},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 3,
                    "output_tokens": 2,
                },
            },
        ]
        self.assertEqual(
            {"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 2},
            workflow.extract_codex_usage(events),
        )

    def test_missing_or_non_turn_usage_stays_null(self):
        self.assertEqual(
            {"input_tokens": None, "cached_input_tokens": None, "output_tokens": None},
            workflow.extract_codex_usage(
                [
                    {"type": "thread.started", "usage": {"input_tokens": 999}},
                    {"type": "turn.completed"},
                ]
            ),
        )

    def test_write_runtime_evidence_is_append_only_and_rejects_attempt_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = workflow.WorkflowStore(Path(temporary) / "state")
            task = valid_task()
            store.create_task(task)
            evidence = workflow.verify_runtime_identity(
                runtime_expected(), runtime_observation()
            )
            path = workflow.write_runtime_evidence(store, task["task_id"], evidence)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([evidence.to_dict()], records)
            with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_EVIDENCE_STALE"):
                workflow.write_runtime_evidence(store, task["task_id"], evidence)

    def test_write_runtime_evidence_rejects_a_native_source_for_exec(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = workflow.WorkflowStore(Path(temporary) / "state")
            task = valid_task()
            store.create_task(task)
            evidence = workflow.verify_runtime_identity(
                runtime_expected(surface="CODEX_EXEC_ROLE_CONTRACT"),
                runtime_observation(surface="CODEX_EXEC_ROLE_CONTRACT"),
            ).to_dict()
            evidence["evidence_source"] = "NATIVE_METADATA"
            with self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_IDENTITY_CONFLICT"):
                workflow.write_runtime_evidence(store, task["task_id"], evidence)


class RuntimeInspectorTest(unittest.TestCase):
    def run_inspector(self, sessions: Path, thread_id=THREAD_ID):
        return subprocess.run(
            ["sh", str(INSPECTOR), "--sessions-dir", str(sessions), thread_id],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_allowlisted_inspection_never_leaks_sensitive_fixture_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            shutil.copytree(FIXTURES / "one", sessions)
            completed = self.run_inspector(sessions)
        self.assertEqual(0, completed.returncode, completed.stderr)
        observation = json.loads(completed.stdout)
        self.assertEqual(
            {
                "thread_id",
                "agent_type",
                "model",
                "reasoning_effort",
                "sandbox_policy",
                "permission_profile",
                "cwd",
            },
            set(observation),
        )
        self.assertEqual(THREAD_ID, observation["thread_id"])
        combined_output = completed.stdout + completed.stderr
        for sentinel in ("PROMPT_SECRET", "ENV_SECRET", "TOKEN_SECRET"):
            self.assertNotIn(sentinel, combined_output)

    def test_inspector_rejects_missing_multiple_and_conflicting_rollouts_without_secrets(self):
        for fixture in (
            "zero",
            "two",
            "conflict",
            "missing",
            "nonstring-duplicate",
            "nonregular",
        ):
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as temporary:
                sessions = Path(temporary) / "sessions"
                shutil.copytree(FIXTURES / fixture, sessions)
                completed = self.run_inspector(sessions)
                self.assertNotEqual(0, completed.returncode)
                combined_output = completed.stdout + completed.stderr
                for sentinel in ("PROMPT_SECRET", "ENV_SECRET", "TOKEN_SECRET"):
                    self.assertNotIn(sentinel, combined_output)

    def test_inspector_rejects_relative_roots_and_invalid_thread_ids(self):
        relative = self.run_inspector(Path("relative-sessions"))
        invalid = self.run_inspector(FIXTURES / "one", "not-a-uuid")
        self.assertNotEqual(0, relative.returncode)
        self.assertNotEqual(0, invalid.returncode)

    def test_inspector_allows_null_agent_type_for_an_exec_role_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            write_exec_rollout(sessions)
            completed = self.run_inspector(sessions)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIsNone(json.loads(completed.stdout)["agent_type"])

    def test_inspector_rejects_a_same_suffix_symlink_without_leaking_its_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            sessions.mkdir()
            target = Path(temporary) / "PROMPT_SECRET_ENV_SECRET_TOKEN_SECRET.json"
            target.write_text('{"prompt":"PROMPT_SECRET"}', encoding="utf-8")
            (sessions / f"rollout-{THREAD_ID}").symlink_to(target)
            completed = self.run_inspector(sessions)
        self.assertNotEqual(0, completed.returncode)
        self.assertNotIn("PROMPT_SECRET", completed.stdout + completed.stderr)


class RuntimeLiveIntegrationTest(unittest.TestCase):
    def _run_paths(self, temporary: str, state_root: Path, sessions: Path | None):
        return workflow.RunPaths(
            repo=ROOT,
            output_path=Path(temporary) / "luna-result.json",
            schema_path=ROOT / "config" / "ai_workflow_result.schema.json",
            logs_dir=Path(temporary) / "logs",
            state_root=state_root,
            runtime_evidence_required=True,
            runtime_sessions_dir=sessions,
        )

    @staticmethod
    def _write_codex_result(command, *args, **kwargs):
        attempt_output = Path(command[command.index("-o") + 1])
        attempt_output.write_text(json.dumps(blocked_luna_result()), encoding="utf-8")
        events = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": THREAD_ID}),
                json.dumps({"type": "turn.completed"}),
            )
        )
        return subprocess.CompletedProcess(command, 0, stdout=events + "\n", stderr="")

    def test_live_rejects_independent_rollout_identity_conflict_before_promotion(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            store = workflow.WorkflowStore(state_root)
            task = valid_task()
            store.create_task(task)
            sessions = Path(temporary) / "sessions"
            write_exec_rollout(sessions, model="gpt-5.6-sol")
            paths = self._run_paths(temporary, state_root, sessions)
            with (
                mock.patch(
                    "scripts.ai_workflow.capture_repo",
                    return_value=workflow.RepoSnapshot("pinned", ()),
                ),
                mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set()),
                mock.patch(
                    "scripts.ai_workflow.subprocess.run",
                    side_effect=self._write_codex_result,
                ),
                self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_IDENTITY_CONFLICT"),
            ):
                workflow.run_codex("luna", task, "Read only.", paths)
            self.assertFalse(paths.output_path.exists())

    def test_runtime_sessions_directory_preflight_blocks_codex_launch(self):
        # Removing the preflight would let every case call the model before
        # runtime evidence can be verified.
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            store = workflow.WorkflowStore(state_root)
            task = valid_task()
            store.create_task(task)
            not_a_directory = Path(temporary) / "not-a-directory"
            not_a_directory.write_text("not a sessions directory", encoding="utf-8")
            cases = (
                (None, "RUNTIME_EVIDENCE_MISSING"),
                (Path("relative-runtime-sessions"), "RUNTIME_EVIDENCE_INVALID"),
                (Path(temporary) / "absent-sessions", "RUNTIME_EVIDENCE_INVALID"),
                (not_a_directory, "RUNTIME_EVIDENCE_INVALID"),
            )
            for sessions, code in cases:
                with self.subTest(sessions=sessions):
                    paths = self._run_paths(temporary, state_root, sessions)
                    with (
                        mock.patch(
                            "scripts.ai_workflow.capture_repo",
                            return_value=workflow.RepoSnapshot("pinned", ()),
                        ),
                        mock.patch(
                            "scripts.ai_workflow.working_tree_paths", return_value=set()
                        ),
                        mock.patch("scripts.ai_workflow.subprocess.run") as model_launch,
                        self.assertRaisesRegex(workflow.WorkflowError, code),
                    ):
                        workflow.run_codex("luna", task, "Read only.", paths)
                    model_launch.assert_not_called()

    def test_live_invalidates_a_prior_canonical_result_when_runtime_evidence_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            store = workflow.WorkflowStore(state_root)
            task = valid_task()
            store.create_task(task)
            sessions = Path(temporary) / "sessions"
            sessions.mkdir()
            paths = self._run_paths(temporary, state_root, sessions)
            paths.output_path.write_text(json.dumps(blocked_luna_result()), encoding="utf-8")
            with (
                mock.patch(
                    "scripts.ai_workflow.capture_repo",
                    return_value=workflow.RepoSnapshot("pinned", ()),
                ),
                mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set()),
                mock.patch(
                    "scripts.ai_workflow.subprocess.run",
                    side_effect=self._write_codex_result,
                ),
                self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_EVIDENCE_MISSING"),
            ):
                workflow.run_codex("luna", task, "Read only.", paths)
            self.assertFalse(paths.output_path.exists())

    def test_live_execution_requires_fresh_thread_evidence_before_canonical_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            store = workflow.WorkflowStore(state_root)
            task = valid_task()
            store.create_task(task)
            sessions = Path(temporary) / "sessions"
            write_exec_rollout(sessions)
            output_path = Path(temporary) / "luna-result.json"
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=output_path,
                schema_path=ROOT / "config" / "ai_workflow_result.schema.json",
                logs_dir=Path(temporary) / "logs",
                state_root=state_root,
                runtime_evidence_required=True,
                runtime_sessions_dir=sessions,
            )

            def write_result(command, *args, **kwargs):
                attempt_output = Path(command[command.index("-o") + 1])
                attempt_output.write_text(json.dumps(blocked_luna_result()), encoding="utf-8")
                events = "\n".join(
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
                return subprocess.CompletedProcess(command, 0, stdout=events + "\n", stderr="")

            with (
                mock.patch(
                    "scripts.ai_workflow.capture_repo",
                    return_value=workflow.RepoSnapshot("pinned", ()),
                ),
                mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set()),
                mock.patch("scripts.ai_workflow.subprocess.run", side_effect=write_result),
            ):
                self.assertEqual(
                    blocked_luna_result(),
                    workflow.run_codex("luna", task, "Read only.", paths),
                )

            self.assertTrue(output_path.is_file())
            runtime_event = json.loads(
                (state_root / task["task_id"] / "events.jsonl").read_text().splitlines()[-1]
            )
            self.assertEqual("CODEX_EXEC_ROLE_CONTRACT", runtime_event["execution_surface"])
            self.assertEqual(THREAD_ID, runtime_event["thread_id"])
            self.assertEqual(10, runtime_event["usage"]["input_tokens"])

    def test_missing_thread_evidence_cannot_promote_a_fresh_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            store = workflow.WorkflowStore(state_root)
            task = valid_task()
            store.create_task(task)
            output_path = Path(temporary) / "luna-result.json"
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=output_path,
                schema_path=ROOT / "config" / "ai_workflow_result.schema.json",
                logs_dir=Path(temporary) / "logs",
                state_root=state_root,
                runtime_evidence_required=True,
            )

            def write_result_without_thread(command, *args, **kwargs):
                attempt_output = Path(command[command.index("-o") + 1])
                attempt_output.write_text(json.dumps(blocked_luna_result()), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout='{"type":"turn.completed"}\n', stderr="")

            with (
                mock.patch(
                    "scripts.ai_workflow.capture_repo",
                    return_value=workflow.RepoSnapshot("pinned", ()),
                ),
                mock.patch("scripts.ai_workflow.working_tree_paths", return_value=set()),
                mock.patch(
                    "scripts.ai_workflow.subprocess.run",
                    side_effect=write_result_without_thread,
                ),
                self.assertRaisesRegex(workflow.WorkflowError, "RUNTIME_EVIDENCE_MISSING"),
            ):
                workflow.run_codex("luna", task, "Read only.", paths)
            self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
