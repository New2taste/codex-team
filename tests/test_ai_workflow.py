import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow


ROOT = Path(__file__).resolve().parents[1]


class ContractFilesTest(unittest.TestCase):
    def test_role_models_and_efforts_are_pinned(self):
        with (ROOT / "config/ai_workflow.toml").open("rb") as handle:
            config = tomllib.load(handle)
        self.assertEqual(config["version"], "ai-workflow-1")
        self.assertEqual(
            (config["roles"]["luna"]["model"], config["roles"]["luna"]["reasoning_effort"]),
            ("gpt-5.6-luna", "max"),
        )
        self.assertEqual(
            config["roles"]["luna"]["allowed_statuses"],
            ["SUPPORTED", "PARTIALLY_SUPPORTED", "NOT_SUPPORTED", "BLOCKED"],
        )
        self.assertEqual(
            (config["roles"]["terra"]["model"], config["roles"]["terra"]["reasoning_effort"]),
            ("gpt-5.6-terra", "xhigh"),
        )
        self.assertFalse(config["policy"]["automatic_xhigh"])
        self.assertFalse(config["policy"]["automatic_merge"])
        self.assertFalse(config["policy"]["automatic_push"])

    def test_contract_versions_and_closed_sets_are_pinned(self):
        task_schema = json.loads((ROOT / "config/ai_workflow_task.schema.json").read_text())
        result_schema = json.loads((ROOT / "config/ai_workflow_result.schema.json").read_text())
        self.assertEqual(task_schema["properties"]["schema_version"]["const"], "ai-task-1")
        self.assertEqual(result_schema["properties"]["schema_version"]["const"], "ai-result-1")
        self.assertEqual(
            set(task_schema["properties"]["verification_level"]["enum"]),
            {"L0", "L1", "L2"},
        )


class TaskValidationTest(unittest.TestCase):
    def valid_task(self):
        return {
            "schema_version": "ai-task-1",
            "task_id": "AWF-20260803-001",
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

    def test_valid_task_passes(self):
        workflow.validate_task(self.valid_task())

    def test_unknown_field_is_rejected(self):
        task = self.valid_task()
        task["surprise"] = True
        with self.assertRaisesRegex(workflow.WorkflowError, "UNKNOWN_FIELD"):
            workflow.validate_task(task)

    def test_acceptance_requires_both_commits(self):
        task = self.valid_task()
        task["task_type"] = "ACCEPTANCE"
        with self.assertRaisesRegex(workflow.WorkflowError, "COMMIT_REQUIRED"):
            workflow.validate_task(task)


class StateMachineTest(unittest.TestCase):
    def test_normal_evidence_transition(self):
        self.assertEqual(
            workflow.next_state("TASK_VALIDATED", "EVIDENCE_RUNNING", owner_authorized=False),
            "EVIDENCE_RUNNING",
        )

    def test_owner_gate_cannot_be_crossed_automatically(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "OWNER_AUTHORIZATION_REQUIRED"):
            workflow.next_state("AWAITING_OWNER_DECISION", "APPROVED_FOR_EXECUTION", owner_authorized=False)

    def test_closed_is_owner_only(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "OWNER_AUTHORIZATION_REQUIRED"):
            workflow.next_state("AWAITING_OWNER_DECISION", "CLOSED", owner_authorized=False)


class WorkflowStoreTest(unittest.TestCase):
    def test_create_task_writes_canonical_json(self):
        with tempfile.TemporaryDirectory() as temp:
            store = workflow.WorkflowStore(Path(temp))
            path = store.create_task(TaskValidationTest().valid_task())
            self.assertEqual(json.loads(path.read_text())["task_id"], "AWF-20260803-001")

    def test_decisions_are_appended_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temp:
            store = workflow.WorkflowStore(Path(temp))
            store.create_task(TaskValidationTest().valid_task())
            store.record_decision("AWF-20260803-001", {"decision": "approve", "by": "owner"})
            store.record_decision("AWF-20260803-001", {"decision": "close", "by": "owner"})
            lines = (Path(temp) / "AWF-20260803-001/human-decisions.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line)["decision"] for line in lines], ["approve", "close"])

    def test_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as temp:
            store = workflow.WorkflowStore(Path(temp))
            store.create_task(TaskValidationTest().valid_task())
            with store.lock("AWF-20260803-001"):
                with self.assertRaisesRegex(workflow.WorkflowError, "TASK_ALREADY_RUNNING"):
                    with store.lock("AWF-20260803-001"):
                        pass


class FakeRunnerTest(unittest.TestCase):
    def test_luna_fake_result_never_claims_acceptance(self):
        result = workflow.FakeRunner().run("luna", TaskValidationTest().valid_task())
        self.assertEqual(result["role"], "luna")
        self.assertEqual(result["status"], "SUPPORTED")
        self.assertNotIn("ACCEPTED", result["status"])


class CodexCommandTest(unittest.TestCase):
    def test_luna_command_is_pinned_and_read_only(self):
        command = workflow.build_codex_command(
            "luna", ROOT, Path("result.json"), ROOT / "config/ai_workflow_result.schema.json"
        )
        self.assertIn("gpt-5.6-luna", command)
        self.assertIn('model_reasoning_effort="max"', command)
        self.assertIn("read-only", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("--agent", command)


class CodexRunnerTest(unittest.TestCase):
    def valid_task(self):
        return TaskValidationTest().valid_task()

    def valid_result(self, role="luna", status="SUPPORTED"):
        return {
            "schema_version": "ai-result-1",
            "role": role,
            "status": status,
            "summary": "Evidence supports the claim.",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "EVIDENCE_READY",
        }

    def test_business_secrets_are_not_forwarded(self):
        env = workflow.sanitized_environment(
            {
                "HOME": "/tmp/home",
                "PATH": "/usr/bin",
                "CODEX_HOME": "/tmp/codex",
                "TUSHARE_TOKEN": "secret",
                "OPENAI_API_KEY": "secret",
                "DB_PASSWORD": "secret",
            }
        )
        self.assertEqual(env["HOME"], "/tmp/home")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["CODEX_HOME"], "/tmp/codex")
        self.assertNotIn("TUSHARE_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("DB_PASSWORD", env)

    def test_role_status_cross_checks_reject_invalid_statuses(self):
        invalid = (
            ("luna", "ACCEPTANCE_RECOMMENDED"),
            ("terra", "SUPPORTED"),
            ("sol_reviewer", "IMPLEMENTED_CANDIDATE"),
        )
        for role, status in invalid:
            with self.subTest(role=role, status=status):
                with self.assertRaisesRegex(workflow.WorkflowError, "ROLE_STATUS_MISMATCH"):
                    workflow.validate_role_result(role, self.valid_result(role, status), set())

    def test_read_only_role_rejects_real_diff_even_when_result_declares_none(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_ROLE_MODIFIED_REPO"):
            workflow.validate_role_result(
                "luna", self.valid_result("luna"), {"forbidden/change.py"}
            )

    def test_result_changed_files_must_match_real_diff(self):
        result = self.valid_result("terra", "IMPLEMENTED_CANDIDATE")
        result["changed_files"] = ["declared.py"]
        with self.assertRaisesRegex(workflow.WorkflowError, "CHANGED_FILES_MISMATCH"):
            workflow.validate_role_result("terra", result, {"actual.py"})

    def test_result_rejects_incomplete_nested_schema_record(self):
        result = self.valid_result()
        result["claims"] = [{"id": "claim-1"}]
        with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_ROLE_RESULT"):
            workflow.validate_role_result("luna", result, set())

    def test_prompt_is_limited_to_task_contract_and_named_evidence(self):
        task = self.valid_task()
        contract = {"acceptance": "run unit tests"}
        with tempfile.TemporaryDirectory() as temp:
            evidence = Path(temp) / "evidence.txt"
            evidence.write_text("verified fact", encoding="utf-8")
            prompt = workflow.build_role_prompt("luna", task, contract, [evidence])
        self.assertIn("Handle only bounded tasks.", prompt)
        prompt_lines = prompt.splitlines()
        self.assertEqual(json.loads(prompt_lines[1].removeprefix("Task envelope: ")), task)
        self.assertEqual(json.loads(prompt_lines[2].removeprefix("Task contract: ")), contract)
        evidence_manifest = json.loads(prompt_lines[3].removeprefix("Named evidence: "))
        self.assertEqual(evidence_manifest[0]["path"], str(evidence))
        self.assertEqual(
            evidence_manifest[0]["sha256"],
            "6f9a5b7a0a9ebb03cde5ab869b864795326fb356563618a3ad0b2b0eb1a835bc",
        )
        self.assertIn(str(evidence), prompt)
        self.assertIn("only output ai-result-1 JSON", prompt)
        self.assertNotIn("registry/", prompt)
        self.assertNotIn("chat history", prompt)

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.subprocess.run")
    def test_run_codex_passes_sanitized_stdin_and_accepts_valid_output(self, run, _capture_repo):
        result = self.valid_result()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output_path = root / "luna-result.json"
            output_path.write_text(json.dumps(result), encoding="utf-8")
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=output_path,
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
            )
            run.return_value = mock.Mock(returncode=0, stdout='{"event":"done"}\n', stderr="")
            with mock.patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "secret", "TUSHARE_TOKEN": "secret", "PATH": "/usr/bin"},
                clear=True,
            ):
                actual = workflow.run_codex("luna", self.valid_task(), "task contract", paths)

            self.assertEqual(actual, result)
            self.assertEqual((root / "logs/luna-events.jsonl").read_text(), '{"event":"done"}\n')

        _, kwargs = run.call_args
        self.assertFalse(kwargs["shell"])
        self.assertEqual(kwargs["input"], "task contract")
        self.assertTrue(kwargs["text"])
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("TUSHARE_TOKEN", kwargs["env"])

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.subprocess.run")
    def test_run_codex_rejects_timeout_exit_and_invalid_json(self, run, _capture_repo):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=root / "luna-result.json",
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
            )
            run.side_effect = __import__("subprocess").TimeoutExpired("codex", 30)
            with self.assertRaisesRegex(workflow.WorkflowError, "CODEX_TIMEOUT"):
                workflow.run_codex("luna", self.valid_task(), "task contract", paths)

            run.side_effect = None
            run.return_value = mock.Mock(returncode=23, stdout="", stderr="failed")
            with self.assertRaisesRegex(workflow.WorkflowError, "CODEX_EXIT_NONZERO"):
                workflow.run_codex("luna", self.valid_task(), "task contract", paths)

            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            paths.output_path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(workflow.WorkflowError, "INVALID_ROLE_RESULT"):
                workflow.run_codex("luna", self.valid_task(), "task contract", paths)

    @mock.patch(
        "scripts.ai_workflow.capture_repo",
        return_value=workflow.RepoSnapshot("pinned-head", ()),
    )
    @mock.patch("scripts.ai_workflow.subprocess.run")
    def test_run_codex_redacts_secret_assignments_and_long_tokens_from_events(self, run, _capture_repo):
        result = self.valid_result()
        long_token = "Ab3d" * 32
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output_path = root / "luna-result.json"
            output_path.write_text(json.dumps(result), encoding="utf-8")
            paths = workflow.RunPaths(
                repo=ROOT,
                output_path=output_path,
                schema_path=ROOT / "config/ai_workflow_result.schema.json",
                logs_dir=root / "logs",
            )
            run.return_value = mock.Mock(
                returncode=0,
                stdout=f"TUSHARE_TOKEN=abc123 OPENAI_API_KEY=sk-test-value {long_token}",
                stderr="",
            )
            workflow.run_codex("luna", self.valid_task(), "task contract", paths)
            events = (root / "logs/luna-events.jsonl").read_text(encoding="utf-8")

        self.assertIn("[REDACTED]", events)
        self.assertIn("TUSHARE_TOKEN=[REDACTED]", events)
        self.assertIn("OPENAI_API_KEY=[REDACTED]", events)
        self.assertNotIn("abc123", events)
        self.assertNotIn("sk-test-value", events)
        self.assertNotIn(long_token, events)


class GitSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary_directory.name) / "repository"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Workflow Test")
        (self.repo / "tracked.txt").write_text("first\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "initial")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _git(self, *args):
        completed = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def _task(self):
        return {
            "schema_version": "ai-task-1",
            "task_id": "AWF-20260803-001",
            "task_type": "REMEDIATION",
            "objective": "Apply a bounded workflow change",
            "repository_root": str(self.repo),
            "source_worktree": None,
            "base_commit": self._git("rev-parse", "HEAD"),
            "candidate_commit": None,
            "authoritative_files": ["tracked.txt"],
            "allowed_write_paths": ["allowed/"],
            "forbidden_actions": ["merge", "push"],
            "risk_flags": [],
            "acceptance_commands": [],
            "verification_level": "L1",
            "human_gates": ["EXECUTION_APPROVAL"],
        }

    @staticmethod
    def _valid_role_result(role, status):
        return {
            "schema_version": "ai-result-1",
            "role": role,
            "status": status,
            "summary": "The bounded run completed.",
            "claims": [],
            "evidence": [],
            "counter_checks": [],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "EVIDENCE_READY",
        }

    def test_assert_pinned_rejects_a_repository_head_that_moved(self):
        snapshot = workflow.capture_repo(self.repo)

        (self.repo / "tracked.txt").write_text("second\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-m", "second")

        with self.assertRaisesRegex(workflow.WorkflowError, "HEAD_DRIFT"):
            workflow.assert_pinned(snapshot, self.repo)

    def test_changed_paths_reports_the_files_between_two_commits(self):
        base = self._git("rev-parse", "HEAD")
        (self.repo / "allowed").mkdir()
        (self.repo / "allowed/a.py").write_text("allowed\n", encoding="utf-8")
        (self.repo / "forbidden").mkdir()
        (self.repo / "forbidden/b.py").write_text("forbidden\n", encoding="utf-8")
        self._git("add", "allowed/a.py", "forbidden/b.py")
        self._git("commit", "-m", "changed paths")
        candidate = self._git("rev-parse", "HEAD")

        self.assertEqual(
            workflow.changed_paths(self.repo, base, candidate),
            {"allowed/a.py", "forbidden/b.py"},
        )

    def test_assert_allowed_changes_rejects_a_path_outside_the_allowed_prefix(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "OUT_OF_SCOPE_CHANGE"):
            workflow.assert_allowed_changes(
                {"allowed/a.py", "forbidden/b.py"},
                ["allowed/"],
            )

    @mock.patch("scripts.ai_workflow.subprocess.run")
    def test_git_uses_a_list_command_without_a_shell(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, stdout="pinned-head\n", stderr="")

        self.assertEqual(workflow.git(self.repo, "rev-parse", "HEAD"), "pinned-head")

        command, kwargs = run.call_args
        self.assertEqual(command[0], ["git", "-C", str(self.repo), "rev-parse", "HEAD"])
        self.assertFalse(kwargs["shell"])

    def test_read_only_luna_and_sol_runs_reject_real_repository_mutations(self):
        for role, status in (("luna", "SUPPORTED"), ("sol_reviewer", "ACCEPTANCE_RECOMMENDED")):
            with self.subTest(role=role):
                output_path = self.repo / f"{role}-result.json"
                output_path.write_text(
                    json.dumps(self._valid_role_result(role, status)), encoding="utf-8"
                )
                paths = workflow.RunPaths(
                    repo=self.repo,
                    output_path=output_path,
                    schema_path=ROOT / "config/ai_workflow_result.schema.json",
                    logs_dir=Path(self.temporary_directory.name) / "logs",
                )
                real_subprocess_run = subprocess.run

                def run_with_real_git(command, *args, **kwargs):
                    if command[0] == "git":
                        return real_subprocess_run(command, *args, **kwargs)
                    (self.repo / f"{role}-mutation.txt").write_text("changed\n", encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0, stdout="{\"event\": \"done\"}\n", stderr="")

                with mock.patch("scripts.ai_workflow.subprocess.run", side_effect=run_with_real_git):
                    with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_ROLE_MODIFIED_REPO"):
                        workflow.run_codex(role, self._task(), "bounded task", paths)

    def test_create_worktree_rejects_an_unauthorized_owner_before_running_git(self):
        task = self._task()
        with mock.patch("scripts.ai_workflow.subprocess.run") as run:
            with self.assertRaisesRegex(workflow.WorkflowError, "OWNER_AUTHORIZATION_REQUIRED"):
                workflow.create_worktree(task, owner_authorized=False)

        run.assert_not_called()

    def test_create_worktree_requires_an_execution_approval_record(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "APPROVED_FOR_EXECUTION_REQUIRED"):
            workflow.create_worktree(self._task(), owner_authorized=True)

    def test_create_worktree_uses_the_approved_branch_and_directory(self):
        task = self._task()
        decision_path = (
            self.repo
            / "data/state/ai-workflow"
            / task["task_id"]
            / "human-decisions.jsonl"
        )
        decision_path.parent.mkdir(parents=True)
        decision_path.write_text(
            json.dumps({"decision": "APPROVED_FOR_EXECUTION", "by": "owner"}) + "\n",
            encoding="utf-8",
        )

        worktree = workflow.create_worktree(task, owner_authorized=True)

        self.assertEqual(
            worktree,
            self.repo / ".codex-worktrees" / "awf-20260803-001",
        )
        self.assertEqual(self._git("-C", str(worktree), "branch", "--show-current"), "aiwf/awf-20260803-001")


if __name__ == "__main__":
    unittest.main()
