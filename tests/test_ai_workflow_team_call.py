import hashlib
import json
import multiprocessing
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from scripts import ai_workflow as workflow
from scripts import ai_workflow_declarations as declarations
from scripts import ai_workflow_dispatch_policy as policy
from scripts import ai_workflow_preflight as preflight
from scripts import ai_workflow_team_call as team
from tests.test_ai_workflow import CodexRunnerTest, _RecordingPopen


ROOT = Path(__file__).resolve().parents[1]


def _concurrent_claim(state_root: str, started, results) -> None:
    call = team.parse_team_call("team call 检查当前工作区状态")
    intent = team.classify_team_call(call)

    def executor(receipt):
        started.set()
        time.sleep(0.3)
        return team.TeamCallRoute(task_id=None, result_sha256="b" * 64)

    try:
        receipt = team.TeamCallRegistry(Path(state_root)).execute_once(call, intent, executor)
        results.put(("receipt", receipt.call_id))
    except team.TeamCallError as exc:
        results.put(("error", exc.code))


class TeamCallContractTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = team.TeamCallRegistry(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_only_a_leading_team_call_directive_is_recognized(self):
        self.assertIsNone(team.parse_team_call("请解释 team call 的含义"))
        self.assertIsNone(team.parse_team_call("> team call 检查当前工作区状态"))
        self.assertEqual(
            "检查当前工作区状态",
            team.parse_team_call("  TeAm\tCaLl：检查当前工作区状态").objective,
        )

    def test_empty_and_malformed_leading_directives_have_stable_errors(self):
        for message, code in (
            ("team call", "TEAM_CALL_EMPTY"),
            ("team call: \t", "TEAM_CALL_EMPTY"),
            ("team call检查当前工作区状态", "TEAM_CALL_INVALID"),
            ("team call/检查当前工作区状态", "TEAM_CALL_INVALID"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(team.TeamCallError, code):
                    team.parse_team_call(message)

    def test_exact_l0_allowlist_never_accepts_user_shell(self):
        safe = team.classify_team_call(team.parse_team_call("team call 检查当前工作区状态"))
        self.assertEqual("DIRECT_L0", safe.disposition)
        self.assertEqual("workspace_status", safe.l0_action)
        self.assertEqual(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            team.L0_FIXED_ARGV[safe.l0_action],
        )
        self.assertNotIn("sh", team.L0_FIXED_ARGV[safe.l0_action])
        unsafe = team.parse_team_call("team call 检查当前工作区状态; rm -rf x")
        with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_UNSAFE_INPUT"):
            team.classify_team_call(unsafe)

    def test_full_verification_is_a_fixed_zero_model_action(self):
        intent = team.classify_team_call(
            team.parse_team_call("team call 运行完整验证")
        )

        self.assertEqual("DIRECT_L0", intent.disposition)
        self.assertEqual("workflow_verify", intent.l0_action)
        self.assertEqual(
            ("sh", "scripts/verify_all.sh"),
            team.L0_FIXED_ARGV[intent.l0_action],
        )

    def test_every_shell_metacharacter_is_rejected_before_routing(self):
        for character in (";", "|", "&", "$", "`", "\n", "\r", "\\"):
            with self.subTest(character=repr(character)):
                call = team.parse_team_call(f"team call 检查当前工作区状态{character}unsafe")
                with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_UNSAFE_INPUT"):
                    team.classify_team_call(call)

    def test_l1_requires_exact_safe_relative_file_grammar(self):
        direct = team.classify_team_call(team.parse_team_call("team call：核对文件 docs/guide.md"))
        self.assertEqual("DIRECT_L1", direct.disposition)
        self.assertEqual("docs/guide.md", direct.evidence_path)
        self.assertEqual(("READ_ONLY_FILE_EVIDENCE",), direct.risk_reasons)

        for message in (
            "team call 核对文件 ../secret.txt",
            "team call 核对文件 /etc/passwd",
            "team call 核对文件 docs/\x00secret.txt",
        ):
            with self.subTest(message=repr(message)):
                call = team.parse_team_call(message)
                with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_EVIDENCE_INVALID"):
                    team.classify_team_call(call)

        uncertain = team.classify_team_call(team.parse_team_call("team call 核对文件 README.md 并更新它"))
        self.assertEqual("PLAN_REQUIRED", uncertain.disposition)
        self.assertEqual(("PLAN_REQUIRED",), uncertain.risk_reasons)

    def test_call_id_binds_raw_request_hash_and_normalized_intake(self):
        raw_hash = hashlib.sha256(b"same raw request").hexdigest()
        first = team.TeamCall("same raw request", "检查当前工作区状态", raw_hash)
        second = team.TeamCall("same raw request", "为 README 增加安装示例", raw_hash)
        first_intent = team.classify_team_call(first)
        second_intent = team.classify_team_call(second)
        self.assertNotEqual(team.team_call_id(first, first_intent), team.team_call_id(second, second_intent))

    def test_same_call_claim_is_idempotent_but_tampered_ledger_blocks(self):
        call = team.parse_team_call("team call 检查当前工作区状态")
        intent = team.classify_team_call(call)
        calls = 0

        def executor(receipt):
            nonlocal calls
            calls += 1
            return team.TeamCallRoute(task_id=None, result_sha256="a" * 64)

        first = self.registry.execute_once(call, intent, executor)
        self.assertEqual(first, self.registry.execute_once(call, intent, executor))
        self.assertEqual(1, calls)
        (self.root / "team-calls.jsonl").write_text('{"call_id":"bad"}\n', encoding="utf-8")
        with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_LEDGER_INVALID"):
            self.registry.execute_once(call, intent, executor)

    def test_callback_error_records_terminal_blocked_route_before_reraising(self):
        call = team.parse_team_call("team call 检查当前工作区状态")
        intent = team.classify_team_call(call)
        executions = 0

        def fail_once(receipt):
            nonlocal executions
            executions += 1
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.registry.execute_once(call, intent, fail_once)

        rows = [json.loads(line) for line in (self.root / "team-calls.jsonl").read_text().splitlines()]
        self.assertEqual(["TEAM_CALL_RECEIVED", "TEAM_CALL_ROUTED"], [row["event"] for row in rows])
        self.assertEqual("BLOCKED", rows[-1]["route_status"])
        self.assertEqual(("FIXED_L0_ALLOWLIST",), tuple(rows[-1]["risk_reasons"]))
        self.assertIsNone(rows[-1]["result_sha256"])
        replay = self.registry.execute_once(call, intent, fail_once)
        self.assertEqual("BLOCKED", replay.disposition)
        self.assertIsNone(replay.task_id)
        self.assertIsNone(replay.result_sha256)
        self.assertEqual(1, executions)

    def test_valid_partial_history_blocks_every_call_from_starting(self):
        first_call = team.parse_team_call("team call 检查当前工作区状态")
        first_intent = team.classify_team_call(first_call)
        receipt = self.registry._new_receipt(first_call, first_intent)
        self.registry._append_received(receipt, first_call, first_intent)
        second_call = team.parse_team_call("team call 核对 plugin 根/镜像一致性")
        second_intent = team.classify_team_call(second_call)

        for call, intent in ((first_call, first_intent), (second_call, second_intent)):
            with self.subTest(call=call.objective):
                with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_ALREADY_RUNNING"):
                    self.registry.execute_once(
                        call,
                        intent,
                        lambda original: team.TeamCallRoute(task_id=None, result_sha256="c" * 64),
                    )

    def test_execute_once_rejects_a_manually_forged_non_directive_call(self):
        raw_message = "ordinary prose, never a directive"
        forged = team.TeamCall(
            raw_message,
            "检查当前工作区状态",
            hashlib.sha256(raw_message.encode("utf-8")).hexdigest(),
        )
        forged_intent = team.classify_team_call(forged)
        executions = []

        with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_INVALID"):
            self.registry.execute_once(
                forged,
                forged_intent,
                lambda receipt: executions.append(receipt) or team.TeamCallRoute(None, "f" * 64),
            )
        self.assertEqual([], executions)

    def test_callback_base_exceptions_record_terminal_blocked_routes(self):
        for exception_type in (SystemExit, KeyboardInterrupt):
            with self.subTest(exception_type=exception_type.__name__):
                root = self.root / exception_type.__name__
                registry = team.TeamCallRegistry(root)
                call = team.parse_team_call("team call 检查当前工作区状态")
                intent = team.classify_team_call(call)

                with self.assertRaises(exception_type):
                    registry.execute_once(
                        call,
                        intent,
                        lambda receipt: (_ for _ in ()).throw(exception_type()),
                    )

                rows = [json.loads(line) for line in (root / "team-calls.jsonl").read_text().splitlines()]
                self.assertEqual(["TEAM_CALL_RECEIVED", "TEAM_CALL_ROUTED"], [row["event"] for row in rows])
                self.assertEqual("BLOCKED", rows[-1]["route_status"])

    def test_invalid_utf8_ledger_is_a_stable_ledger_error(self):
        (self.root / "team-calls.jsonl").write_bytes(b"\xff\n")
        call = team.parse_team_call("team call 检查当前工作区状态")
        intent = team.classify_team_call(call)

        with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_LEDGER_INVALID"):
            self.registry.execute_once(
                call,
                intent,
                lambda receipt: team.TeamCallRoute(None, "a" * 64),
            )

    def test_interleaved_received_and_routed_histories_are_rejected(self):
        first_call = team.parse_team_call("team call 检查当前工作区状态")
        first_intent = team.classify_team_call(first_call)
        second_call = team.parse_team_call("team call 核对 plugin 根/镜像一致性")
        second_intent = team.classify_team_call(second_call)
        first_receipt = self.registry._new_receipt(first_call, first_intent)
        second_receipt = self.registry._new_receipt(second_call, second_intent)
        first_route = replace(first_receipt, result_sha256="1" * 64)
        second_route = replace(second_receipt, result_sha256="2" * 64)
        self.registry._append_received(first_receipt, first_call, first_intent)
        self.registry._append_received(second_receipt, second_call, second_intent)
        self.registry._append_event(
            self.registry._event("TEAM_CALL_ROUTED", first_route, first_call, first_intent, route_status="ROUTED")
        )
        self.registry._append_event(
            self.registry._event("TEAM_CALL_ROUTED", second_route, second_call, second_intent, route_status="ROUTED")
        )

        third_call = team.parse_team_call("team call 核对文件 docs/guide.md")
        third_intent = team.classify_team_call(third_call)
        with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_LEDGER_INVALID"):
            self.registry.execute_once(
                third_call,
                third_intent,
                lambda receipt: team.TeamCallRoute(None, "3" * 64),
            )

    def test_duplicate_json_member_names_are_rejected_before_shape_validation(self):
        call = team.parse_team_call("team call 检查当前工作区状态")
        intent = team.classify_team_call(call)
        receipt = self.registry._new_receipt(call, intent)
        event = self.registry._received_event(receipt, call, intent)
        row = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        duplicate = row.replace(
            '"event":"TEAM_CALL_RECEIVED"',
            '"event":"forged","event":"TEAM_CALL_RECEIVED"',
        )
        (self.root / "team-calls.jsonl").write_text(duplicate + "\n", encoding="utf-8")
        second_call = team.parse_team_call("team call 核对 plugin 根/镜像一致性")
        second_intent = team.classify_team_call(second_call)

        with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_LEDGER_INVALID"):
            self.registry.execute_once(
                second_call,
                second_intent,
                lambda original: team.TeamCallRoute(None, "4" * 64),
            )

    def test_identity_drift_for_existing_call_id_is_rejected(self):
        call = team.parse_team_call("team call 检查当前工作区状态")
        intent = team.classify_team_call(call)
        receipt = self.registry._new_receipt(call, intent)
        received = self.registry._received_event(receipt, call, intent)
        received["raw_request_sha256"] = "0" * 64
        (self.root / "team-calls.jsonl").write_text(
            json.dumps(received, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_IDENTITY_DRIFT"):
            self.registry.execute_once(
                call,
                intent,
                lambda original: team.TeamCallRoute(task_id=None, result_sha256="d" * 64),
            )

    def test_two_processes_contending_for_the_same_global_lock_start_one_executor(self):
        context = multiprocessing.get_context("spawn")
        started = context.Event()
        results = context.Queue()
        first = context.Process(target=_concurrent_claim, args=(str(self.root), started, results))
        second = context.Process(target=_concurrent_claim, args=(str(self.root), started, results))
        first.start()
        self.assertTrue(started.wait(timeout=5))
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        observed = {results.get(timeout=2), results.get(timeout=2)}
        self.assertEqual({("receipt", team.team_call_id(
            team.parse_team_call("team call 检查当前工作区状态"),
            team.classify_team_call(team.parse_team_call("team call 检查当前工作区状态")),
        )), ("error", "TEAM_CALL_ALREADY_RUNNING")}, observed)


class _TeamCallControllerFake:
    """A bounded controller fixture with no process or model launch."""

    def __init__(self):
        self.executed_argv = None
        self.executed_cwd = None
        self.execution_count = 0
        self.dispatch_count = 0
        self.l1_task = None
        self.l1_execution = None
        self.l1_role = None
        self.l1_result_override = None
        self.l0_result_override = None
        self.mutate_task = None
        self.write_path = None
        self.ignored_write_path = None
        self.untracked_write_path = None
        self.chmod_path = None
        self.commit_write_path = None
        self.git_control_write_path = None
        self.on_before_run_l1 = None
        self.started = None
        self.release = None

    @staticmethod
    def _luna_result():
        return {
            "schema_version": "ai-result-1",
            "role": "luna",
            "status": "SUPPORTED",
            "summary": "The named file was read without modification.",
            "claims": [
                {
                    "id": "claim-1",
                    "kind": "FACT",
                    "text": "The named fixture exists.",
                    "evidence_ids": ["evidence-1"],
                }
            ],
            "evidence": [
                {
                    "id": "evidence-1",
                    "type": "FILE",
                    "locator": "README.md",
                    "observation": "The fixture was read.",
                }
            ],
            "counter_checks": [
                {
                    "target_claim_id": "claim-1",
                    "method": "Read the fixture a second time.",
                    "result": "No contradiction was found.",
                }
            ],
            "changed_files": [],
            "blind_spots": [],
            "unresolved_questions": [],
            "recommended_next_state": "EVIDENCE_READY",
        }

    def run_l0(self, argv, cwd):
        self.execution_count += 1
        self.executed_argv = tuple(argv)
        self.executed_cwd = Path(cwd)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            self.release.wait(timeout=5)
        return self.l0_result_override or subprocess.CompletedProcess(
            argv, 0, stdout="clean\n", stderr=""
        )

    def run_l1(self, task, *, role):
        self.execution_count += 1
        self.l1_task = task
        self.l1_execution = task
        self.l1_role = role
        if self.on_before_run_l1 is not None:
            self.on_before_run_l1(task)
        if self.mutate_task is not None:
            self.mutate_task(task)
        if self.write_path is not None:
            Path(self.write_path).write_text("unauthorized\n", encoding="utf-8")
        if self.ignored_write_path is not None:
            Path(self.ignored_write_path).write_text("ignored unauthorized\n", encoding="utf-8")
        if self.untracked_write_path is not None:
            Path(self.untracked_write_path).write_text("untracked unauthorized\n", encoding="utf-8")
        if self.chmod_path is not None:
            Path(self.chmod_path).chmod(0o700)
        if self.commit_write_path is not None:
            Path(self.commit_write_path).write_text("committed unauthorized\n", encoding="utf-8")
            subprocess.run(("git", "add", Path(self.commit_write_path).name), cwd=Path(self.commit_write_path).parent,
                           check=True, capture_output=True, text=True)
            subprocess.run(("git", "commit", "-m", "controller mutation"), cwd=Path(self.commit_write_path).parent,
                           check=True, capture_output=True, text=True)
        if self.git_control_write_path is not None:
            Path(self.git_control_write_path).write_text("persistent controller mutation\n", encoding="utf-8")
        result = (
            _TeamCallControllerFake._luna_result()
            if self.l1_result_override is None
            else self.l1_result_override
        )
        return dict(result)


class TeamCallControllerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "state"
        self.repo = Path(self.temporary.name) / "repository"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "team-call@example.test")
        self._git("config", "user.name", "Team Call Test")
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "initial fixture")
        self.controller = workflow.TeamCallFakeController()
        _TeamCallControllerFake.__init__(self.controller)
        self.controller_patches = (
            mock.patch.object(
                workflow.TeamCallFakeController,
                "run_l0",
                new=_TeamCallControllerFake.run_l0,
            ),
            mock.patch.object(
                workflow.TeamCallFakeController,
                "run_l1",
                new=_TeamCallControllerFake.run_l1,
            ),
        )
        for patcher in self.controller_patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.controller_patches):
            patcher.stop()
        self.temporary.cleanup()

    def _git(self, *argv):
        return subprocess.run(
            ("git", *argv),
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _team_rows(self):
        return [
            json.loads(line)
            for line in (self.root / "team-calls.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    def test_l0_runs_fixed_git_status_once_and_returns_its_receipt(self):
        receipt = workflow.run_team_call(
            "team call 检查当前工作区状态",
            repository_root=self.repo,
            state_root=self.root,
            controller=self.controller,
        )

        self.assertEqual("DIRECT_L0", receipt.disposition)
        self.assertEqual(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            self.controller.executed_argv,
        )
        self.assertEqual(self.repo.resolve(), self.controller.executed_cwd)
        self.assertRegex(receipt.result_sha256, r"^[0-9a-f]{64}$")
        metadata = json.loads(
            (self.root / "team-call-results" / f"{receipt.call_id}.json").read_text(encoding="utf-8")
        )
        self.assertEqual("L0", metadata["kind"])
        self.assertEqual(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], metadata["argv"]
        )
        self.assertEqual(self.repo.resolve(), Path(metadata["cwd"]))
        self.assertEqual(
            receipt.result_sha256,
            hashlib.sha256(workflow._canonical_json(metadata).encode("utf-8")).hexdigest(),
        )
        repeated = workflow.run_team_call(
            "team call 检查当前工作区状态",
            repository_root=self.repo,
            state_root=self.root,
            controller=self.controller,
        )
        self.assertEqual(receipt, repeated)
        self.assertEqual(1, self.controller.execution_count)

    def test_explicit_l1_file_is_luna_only_and_cannot_write(self):
        receipt = workflow.run_team_call(
            "team call 核对文件 README.md",
            repository_root=self.repo,
            state_root=self.root,
            controller=self.controller,
        )

        self.assertEqual("DIRECT_L1", receipt.disposition)
        self.assertEqual("luna", self.controller.l1_role)
        self.assertEqual([], self.controller.l1_task["allowed_write_paths"])
        self.assertEqual("L1", self.controller.l1_task["verification_level"])
        self.assertEqual(["README.md"], self.controller.l1_task["authoritative_files"])
        self.assertEqual("PLAN", self.controller.l1_task["task_type"])
        self.assertTrue((self.root / receipt.task_id / "task.json").is_file())
        stored_task_path = self.root / receipt.task_id / "task.json"
        stored_task = json.loads(stored_task_path.read_text(encoding="utf-8"))
        expected_task_sha256 = hashlib.sha256(
            stored_task_path.read_bytes()
        ).hexdigest()
        self.assertEqual(expected_task_sha256, self.controller.l1_execution.task_sha256)
        self.assertEqual("luna", self.controller.l1_execution.role)
        self.assertEqual("team-call-fake", self.controller.l1_execution.execution_surface)
        self.assertEqual(
            hashlib.sha256(b"fixture\n").hexdigest(),
            self.controller.l1_execution.consumed_evidence_sha256,
        )

    def test_failed_l0_replay_is_a_blocked_receipt_without_second_execution(self):
        self.controller.l0_result_override = subprocess.CompletedProcess(
            ("git", "status"), 7, stdout="", stderr="failed"
        )

        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_L0_FAILED"):
            workflow.run_team_call(
                "team call 检查当前工作区状态",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )
        replay = workflow.run_team_call(
            "team call 检查当前工作区状态",
            repository_root=self.repo,
            state_root=self.root,
            controller=self.controller,
        )

        self.assertEqual("BLOCKED", replay.disposition)
        self.assertIsNone(replay.task_id)
        self.assertIsNone(replay.result_sha256)
        self.assertEqual(1, self.controller.execution_count)

    def test_l1_allocates_the_next_daily_task_id_while_the_registry_is_held(self):
        prefix = f"AWF-{datetime.now(timezone.utc):%Y%m%d}-"
        self.root.mkdir(parents=True)
        (self.root / f"{prefix}002").mkdir()
        (self.root / f"{prefix}007").mkdir()

        receipt = workflow.run_team_call(
            "team call 核对文件 README.md",
            repository_root=self.repo,
            state_root=self.root,
            controller=self.controller,
        )

        self.assertEqual(f"{prefix}008", receipt.task_id)

    def test_write_or_ambiguous_request_creates_no_dispatch_and_requires_plan(self):
        receipt = workflow.run_team_call(
            "team call 为 README 增加安装示例",
            repository_root=self.repo,
            state_root=self.root,
            controller=self.controller,
        )

        self.assertEqual("PLAN_REQUIRED", receipt.disposition)
        self.assertIsNone(receipt.task_id)
        self.assertIsNone(receipt.result_sha256)
        self.assertEqual(0, self.controller.execution_count)
        self.assertEqual(0, self.controller.dispatch_count)
        self.assertEqual("ROUTED", self._team_rows()[-1]["route_status"])

    def test_missing_repository_fails_before_controller_execution(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "REPOSITORY_NOT_FOUND"):
            workflow.run_team_call(
                "team call 检查当前工作区状态",
                repository_root=self.repo / "missing",
                state_root=self.root,
                controller=self.controller,
            )

        self.assertEqual(0, self.controller.execution_count)

    def test_l1_nonempty_changed_files_claim_records_blocked_route(self):
        self.controller.l1_result_override = {
            **_TeamCallControllerFake._luna_result(),
            "changed_files": ["README.md"],
        }

        with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_ROLE_MODIFIED_REPO"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_failed_l1_replay_is_a_blocked_receipt_without_second_execution(self):
        self.controller.l1_result_override = {
            **_TeamCallControllerFake._luna_result(),
            "changed_files": ["README.md"],
        }

        with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_ROLE_MODIFIED_REPO"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )
        replay = workflow.run_team_call(
            "team call 核对文件 README.md",
            repository_root=self.repo,
            state_root=self.root,
            controller=self.controller,
        )

        self.assertEqual("BLOCKED", replay.disposition)
        self.assertIsNone(replay.task_id)
        self.assertIsNone(replay.result_sha256)
        self.assertEqual(1, self.controller.execution_count)

    def test_l1_role_mismatch_records_blocked_route(self):
        self.controller.l1_result_override = {
            **_TeamCallControllerFake._luna_result(),
            "role": "terra",
        }

        with self.assertRaisesRegex(workflow.WorkflowError, "ROLE_MISMATCH"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_l1_actual_working_tree_diff_records_blocked_route(self):
        self.controller.write_path = self.repo / "README.md"

        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_REPOSITORY_CHANGED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_l1_ignored_in_repository_write_records_blocked_route(self):
        (self.repo / ".gitignore").write_text("ignored-output.txt\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-m", "ignore controller output")
        self.controller.ignored_write_path = self.repo / "ignored-output.txt"

        with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_FILESYSTEM_CHANGED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertTrue((self.repo / "ignored-output.txt").is_file())
        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_l1_untracked_in_repository_write_records_blocked_route(self):
        self.controller.untracked_write_path = self.repo / "untracked-output.txt"

        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_REPOSITORY_CHANGED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertTrue((self.repo / "untracked-output.txt").is_file())
        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_l1_metadata_only_change_records_blocked_route(self):
        (self.repo / ".gitignore").write_text("ignored-metadata.txt\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-m", "ignore metadata fixture")
        ignored = self.repo / "ignored-metadata.txt"
        ignored.write_text("fixture\n", encoding="utf-8")
        ignored.chmod(0o600)
        self.controller.chmod_path = ignored

        with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_FILESYSTEM_CHANGED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_l1_committed_write_records_blocked_route_even_when_tree_is_clean(self):
        head_before = self._git("rev-parse", "HEAD").stdout.strip()
        self.controller.commit_write_path = self.repo / "README.md"

        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_REPOSITORY_CHANGED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertNotEqual(head_before, self._git("rev-parse", "HEAD").stdout.strip())
        self.assertEqual("", self._git("status", "--porcelain=v1").stdout)
        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_l1_git_control_plane_write_records_blocked_route(self):
        git_dir = Path(self._git("rev-parse", "--absolute-git-dir").stdout.strip())
        self.controller.git_control_write_path = git_dir / "l1-controller-write"

        with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_FILESYSTEM_CHANGED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertTrue((git_dir / "l1-controller-write").is_file())
        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_l1_existing_git_control_file_content_change_is_blocked(self):
        git_dir = Path(self._git("rev-parse", "--absolute-git-dir").stdout.strip())
        control_file = git_dir / "description"
        self.assertTrue(control_file.is_file())
        original = control_file.read_text(encoding="utf-8")
        self.controller.on_before_run_l1 = lambda task: control_file.write_text(
            f"{original}persistent mutation\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(
            workflow.WorkflowError, "READ_ONLY_FILESYSTEM_CHANGED"
        ):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

    def test_l1_read_only_git_status_does_not_fail_on_transient_lock_metadata(self):
        self.controller.on_before_run_l1 = lambda task: self._git(
            "status", "--porcelain=v1", "--untracked-files=all"
        )

        route = workflow.run_team_call(
            "team call 核对文件 README.md",
            repository_root=self.repo,
            state_root=self.root,
            controller=self.controller,
        )

        self.assertEqual("DIRECT_L1", route.disposition)
        self.assertEqual("ROUTED", self._team_rows()[-1]["route_status"])

    def test_l1_linked_worktree_gitdir_and_common_gitdir_writes_are_blocked(self):
        linked = Path(self.temporary.name) / "linked-worktree"
        self._git("worktree", "add", "-b", "linked-test", str(linked))
        linked_gitdir = Path(
            subprocess.run(
                ("git", "rev-parse", "--absolute-git-dir"),
                cwd=linked,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        common_gitdir = Path(
            subprocess.run(
                ("git", "rev-parse", "--git-common-dir"),
                cwd=linked,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )

        def write_both_control_roots(task):
            (linked_gitdir / "l1-worktree-write").write_text("mutation\n", encoding="utf-8")
            (common_gitdir / "l1-common-write").write_text("mutation\n", encoding="utf-8")

        self.controller.on_before_run_l1 = write_both_control_roots
        with self.assertRaisesRegex(workflow.WorkflowError, "READ_ONLY_FILESYSTEM_CHANGED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=linked,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertTrue((linked_gitdir / "l1-worktree-write").is_file())
        self.assertTrue((common_gitdir / "l1-common-write").is_file())

    def test_l1_controller_cannot_widen_the_read_only_write_scope(self):
        self.controller.mutate_task = lambda task: task.__setitem__("allowed_write_paths", ["README.md"])

        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_WRITE_SCOPE_INVALID"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_l1_controller_cannot_change_the_persisted_task_identity(self):
        self.controller.mutate_task = lambda task: task.__setitem__("objective", "different scope")

        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_TASK_MUTATED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_l1_rejects_same_value_rewrite_of_the_exact_stored_task_bytes(self):
        def rewrite_task_with_different_bytes(execution):
            task_path = self.root / execution["task_id"] / "task.json"
            task_path.write_text(
                json.dumps(dict(execution), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        self.controller.on_before_run_l1 = rewrite_task_with_different_bytes

        with self.assertRaisesRegex(workflow.WorkflowError, "TASK_STORE_MISMATCH"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_l1_symlink_evidence_is_rejected_before_luna_execution(self):
        (self.repo / "target.md").write_text("target\n", encoding="utf-8")
        (self.repo / "linked.md").symlink_to("target.md")

        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_EVIDENCE_UNSAFE"):
            workflow.run_team_call(
                "team call 核对文件 linked.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertEqual(0, self.controller.execution_count)

    def test_l1_intermediate_symlink_evidence_is_rejected_before_luna_execution(self):
        (self.repo / "actual").mkdir()
        (self.repo / "actual" / "evidence.md").write_text("fixture\n", encoding="utf-8")
        (self.repo / "docs").symlink_to("actual", target_is_directory=True)

        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_EVIDENCE_UNSAFE"):
            workflow.run_team_call(
                "team call 核对文件 docs/evidence.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertEqual(0, self.controller.execution_count)

    def test_l1_evidence_swap_after_initial_validation_prevents_controller_execution(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "evidence.md").write_text("in repo\n", encoding="utf-8")
        self._git("add", "docs/evidence.md")
        self._git("commit", "-m", "add in-repo evidence")
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        (outside / "evidence.md").write_text("OUTSIDE_SECRET\n", encoding="utf-8")

        original_create_task = workflow.WorkflowStore.create_task

        def swap_after_initial_validation(store, task):
            (self.repo / "docs" / "evidence.md").unlink()
            (self.repo / "docs").rmdir()
            (self.repo / "docs").symlink_to(outside, target_is_directory=True)
            return original_create_task(store, task)

        with mock.patch.object(workflow.WorkflowStore, "create_task", new=swap_after_initial_validation):
            with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_EVIDENCE_UNSAFE"):
                workflow.run_team_call(
                    "team call 核对文件 docs/evidence.md",
                    repository_root=self.repo,
                    state_root=self.root,
                    controller=self.controller,
                )

        self.assertEqual(0, self.controller.execution_count)
        self.assertEqual("BLOCKED", self._team_rows()[-1]["route_status"])

    def test_l1_post_revalidation_swap_cannot_change_consumed_evidence(self):
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "evidence.md").write_text("IN_REPO\n", encoding="utf-8")
        self._git("add", "docs/evidence.md")
        self._git("commit", "-m", "add pinned evidence")
        outside = Path(self.temporary.name) / "outside-after-revalidation"
        outside.mkdir()
        (outside / "evidence.md").write_text("OUTSIDE_SECRET\n", encoding="utf-8")
        observed = []
        original_revalidate = workflow._revalidate_team_call_l1_evidence

        def swap_after_revalidation(repository, evidence_pin):
            current = original_revalidate(repository, evidence_pin)
            (self.repo / "docs" / "evidence.md").unlink()
            (self.repo / "docs").rmdir()
            (self.repo / "docs").symlink_to(outside, target_is_directory=True)
            return current

        def consume_evidence(task):
            pinned = getattr(task, "pinned_evidence_bytes", None)
            if pinned is not None:
                observed.append(pinned.decode("utf-8"))
            else:
                observed.append(
                    (Path(task["repository_root"]) / task["authoritative_files"][0]).read_text(
                        encoding="utf-8"
                    )
                )

        self.controller.on_before_run_l1 = consume_evidence
        with mock.patch.object(
            workflow, "_revalidate_team_call_l1_evidence", new=swap_after_revalidation
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_REPOSITORY_CHANGED"):
                workflow.run_team_call(
                    "team call 核对文件 docs/evidence.md",
                    repository_root=self.repo,
                    state_root=self.root,
                    controller=self.controller,
                )

        self.assertEqual(["IN_REPO\n"], observed)

    def test_direct_l1_rejects_a_subclass_forged_controller_capability(self):
        class ForgedController(workflow.TeamCallFakeController):
            def __init__(self):
                self.calls = 0

            def run_l0(self, argv, cwd):
                raise AssertionError("not an L0 request")

            def run_l1(self, task, *, role):
                self.calls += 1
                return _TeamCallControllerFake._luna_result()

        controller = ForgedController()
        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_CONTROLLER_UNTRUSTED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=controller,
            )

        self.assertEqual(0, controller.calls)

    def test_direct_l1_rejects_an_exact_instance_method_shadow_before_execution(self):
        shadow_calls = []
        outside = Path(self.temporary.name) / "outside-write"

        def shadowed_run_l1(task, *, role):
            shadow_calls.append((task["task_id"], role))
            outside.write_text("unauthorized\n", encoding="utf-8")
            return _TeamCallControllerFake._luna_result()

        self.controller.run_l1 = shadowed_run_l1

        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_CONTROLLER_UNTRUSTED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )

        self.assertEqual([], shadow_calls)
        self.assertFalse(outside.exists())
        self.assertFalse((self.root / "team-calls.jsonl").exists())

    @mock.patch("scripts.ai_workflow.run_codex")
    def test_authorized_production_l1_uses_an_immutable_evidence_snapshot(self, run_codex):
        run_codex.return_value = _TeamCallControllerFake._luna_result()
        sessions = Path(self.temporary.name) / "sessions"
        sessions.mkdir()
        controller = workflow.TeamCallProductionController(
            self.root,
            allow_live_model=True,
            runtime_sessions_dir=sessions,
        )

        receipt = workflow.run_team_call(
            "team call 核对文件 README.md",
            repository_root=self.repo,
            state_root=self.root,
            controller=controller,
        )

        self.assertEqual("DIRECT_L1", receipt.disposition)
        self.assertRegex(receipt.result_sha256, r"^[0-9a-f]{64}$")
        run_codex.assert_called_once()
        role, task_document, prompt, paths = run_codex.call_args.args
        self.assertEqual("luna", role)
        self.assertEqual(receipt.task_id, task_document["task_id"])
        self.assertEqual([], task_document["allowed_write_paths"])
        self.assertEqual(workflow.ROLE_CONFIG_PATH.parent / "ai_workflow_result.schema.json", paths.schema_path)
        snapshots = list((self.root / receipt.task_id / "team-call-evidence").iterdir())
        self.assertEqual(1, len(snapshots))
        self.assertEqual(b"fixture\n", snapshots[0].read_bytes())
        self.assertNotEqual(self.repo, snapshots[0].parent)
        self.assertIn(str(snapshots[0]), prompt)
        metadata = json.loads(
            (self.root / "team-call-results" / f"{receipt.call_id}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("luna", metadata["attestation"]["role"])
        self.assertEqual(workflow.CODEX_EXEC_ROLE_CONTRACT, metadata["attestation"]["execution_surface"])
        self.assertEqual(
            hashlib.sha256(b"fixture\n").hexdigest(),
            metadata["attestation"]["consumed_evidence_sha256"],
        )

    def test_l1_rejects_an_untrusted_controller_even_when_it_claims_not_live(self):
        class SpoofedLiveNonLunaController:
            is_live_model = False

            def __init__(self):
                self.calls = 0
                self.attempted_role = None

            def run_l1(self, task, *, role):
                self.calls += 1
                self.attempted_role = "terra"
                return _TeamCallControllerFake._luna_result()

        controller = SpoofedLiveNonLunaController()

        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_CONTROLLER_UNTRUSTED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=controller,
            )

        self.assertEqual(0, controller.calls)
        self.assertIsNone(controller.attempted_role)
        self.assertFalse((self.root / "team-calls.jsonl").exists())

    def test_l1_rejects_an_untrusted_controller_result_for_a_different_task(self):
        class DifferentTaskController:
            is_live_model = False

            def __init__(self):
                self.calls = 0
                self.other_task = {"task_id": "AWF-20260813-999", "role": "terra"}

            def run_l1(self, task, *, role):
                self.calls += 1
                return _TeamCallControllerFake._luna_result()

        controller = DifferentTaskController()

        with self.assertRaisesRegex(workflow.WorkflowError, "TEAM_CALL_CONTROLLER_UNTRUSTED"):
            workflow.run_team_call(
                "team call 核对文件 README.md",
                repository_root=self.repo,
                state_root=self.root,
                controller=controller,
            )

        self.assertEqual(0, controller.calls)

    def test_simultaneous_duplicate_calls_start_exactly_one_execution(self):
        self.controller.started = threading.Event()
        self.controller.release = threading.Event()
        outcome = []

        def invoke():
            try:
                outcome.append(
                    workflow.run_team_call(
                        "team call 检查当前工作区状态",
                        repository_root=self.repo,
                        state_root=self.root,
                        controller=self.controller,
                    )
                )
            except workflow.WorkflowError as exc:
                outcome.append(exc.code)

        first = threading.Thread(target=invoke)
        second = threading.Thread(target=invoke)
        first.start()
        self.assertTrue(self.controller.started.wait(timeout=5))
        second.start()
        second.join(timeout=5)
        self.controller.release.set()
        first.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(1, self.controller.execution_count)
        self.assertEqual(1, sum(isinstance(item, team.TeamCallReceipt) for item in outcome))
        self.assertIn("TEAM_CALL_ALREADY_RUNNING", outcome)

    def test_production_l0_uses_the_fixed_argv_without_a_shell(self):
        controller = workflow.TeamCallProductionController(self.root)
        argv = ("git", "status", "--porcelain=v1", "--untracked-files=all")
        expected = subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with mock.patch("scripts.ai_workflow.subprocess.run", return_value=expected) as run:
            self.assertIs(expected, controller.run_l0(argv, self.repo))

        run.assert_called_once_with(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            cwd=self.repo,
        )

    def test_direct_l0_never_reaches_run_codex_or_creates_a_task(self) -> None:
        with mock.patch.object(workflow, "run_codex") as run_codex:
            receipt = workflow.run_team_call(
                "team call 检查当前工作区状态",
                repository_root=self.repo,
                state_root=self.root,
                controller=self.controller,
            )
        self.assertEqual("DIRECT_L0", receipt.disposition)
        self.assertIsNone(receipt.task_id)
        run_codex.assert_not_called()
        self.assertFalse(any(path.is_dir() and path.name.startswith("AWF-") for path in self.root.iterdir()))

    def test_direct_l1_creates_declaration_and_rejects_after_deletion(self) -> None:
        (self.repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
        receipt = workflow.run_team_call(
            "team call 核对文件 README.md",
            repository_root=self.repo,
            state_root=self.root,
            controller=self.controller,
        )
        self.assertEqual("DIRECT_L1", receipt.disposition)
        task_id = receipt.task_id
        self.assertIsNotNone(task_id)
        declaration = self.root / str(task_id) / "route-declaration.json"
        self.assertTrue(declaration.is_file())
        declaration.unlink()
        store = workflow.WorkflowStore(self.root)
        task = workflow.load_task(self.root / str(task_id) / "task.json")
        paths = workflow.RunPaths(
            repo=self.repo,
            output_path=self.root / str(task_id) / "luna-result.json",
            schema_path=ROOT / "config/ai_workflow_result.schema.json",
            logs_dir=self.root / str(task_id) / "logs",
            state_root=self.root,
        )
        with (
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())),
            mock.patch.object(workflow.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(
                workflow.WorkflowError, "ROUTE_DECLARATION_MISSING|ROUTE_DECLARATION_CORRUPT"
            ):
                workflow.run_codex("luna", task, "task contract", paths)
        popen.assert_not_called()

    def _direct_l1_task(self):
        (self.repo / ".codex" / "sessions").mkdir(parents=True, exist_ok=True)
        receipt = workflow.run_team_call(
            "team call 核对文件 README.md",
            repository_root=self.repo,
            state_root=self.root,
            controller=self.controller,
        )
        self.assertEqual("DIRECT_L1", receipt.disposition)
        task_id = str(receipt.task_id)
        task = workflow.load_task(self.root / task_id / "task.json")
        paths = workflow.RunPaths(
            repo=self.repo,
            output_path=self.root / task_id / "luna-result.json",
            schema_path=ROOT / "config/ai_workflow_result.schema.json",
            logs_dir=self.root / task_id / "logs",
            state_root=self.root,
        )
        return task_id, task, paths

    def _l1_popen(self):
        result = CodexRunnerTest().valid_result()

        class Popen(_RecordingPopen):
            _result = result

        _RecordingPopen.reset()
        return Popen

    def _assert_direct_l1_rejects_before_spawn(self, code: str, role: str, mutate) -> None:
        task_id, task, paths = self._direct_l1_task()
        mutate(task_id)
        popen = self._l1_popen()
        with (
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(
                workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())
            ),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, code):
                workflow.run_codex(role, task, "task contract", paths)
        self.assertEqual([], popen.calls)

    def test_direct_l1_role_not_allowed_does_not_spawn(self) -> None:
        self._assert_direct_l1_rejects_before_spawn(
            "ROLE_NOT_ALLOWED", "luna", lambda task_id: None
        )

    def test_direct_l1_role_not_preflighted_does_not_spawn(self) -> None:
        task_id, task, paths = self._direct_l1_task()
        ledger = self.root / task_id / preflight.PREFLIGHT_LEDGER
        if ledger.is_file():
            ledger.unlink()
        declaration = declarations.load_route_declaration(
            workflow.WorkflowStore(self.root), task_id
        )
        self.assertIsNotNone(declaration)
        role = declaration.allowed_roles[0]
        popen = self._l1_popen()
        with (
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(
                workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())
            ),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROLE_NOT_PREFLIGHTED"):
                workflow.run_codex(role, task, "task contract", paths)
        self.assertEqual([], popen.calls)

    def test_direct_l1_budget_exceeded_does_not_spawn(self) -> None:
        task_id, task, paths = self._direct_l1_task()
        store = workflow.WorkflowStore(self.root)
        declaration = declarations.load_route_declaration(store, task_id)
        self.assertIsNotNone(declaration)
        path = store._require_task(task_id) / policy.DISPATCH_PERMIT_LEDGER
        lines: list[str] = []
        seq = 1
        for index in range(declaration.max_dispatches):
            permit_id = f"{index:064x}"
            for state in ("RESERVED", "STARTED"):
                lines.append(
                    json.dumps(
                        {
                            "schema_version": policy.DISPATCH_PERMIT_SCHEMA_VERSION,
                            "seq": seq,
                            "permit_id": permit_id,
                            "task_id": task_id,
                            "role": declaration.allowed_roles[0],
                            "state": state,
                            "reason": "",
                            "recorded_at_utc": "2026-08-28T00:00:00Z",
                        }
                    )
                )
                seq += 1
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        popen = self._l1_popen()
        with (
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(
                workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())
            ),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROUTE_BUDGET_EXCEEDED"):
                workflow.run_codex(
                    declaration.allowed_roles[0], task, "task contract", paths
                )
        self.assertEqual([], popen.calls)

    def test_direct_l1_preflights_active_roles_and_trims_inactive(self) -> None:
        task_id, task, paths = self._direct_l1_task()
        store = workflow.WorkflowStore(self.root)
        declaration = declarations.load_route_declaration(store, task_id)
        self.assertIsNotNone(declaration)
        assert declaration is not None
        ledger = self.root / task_id / preflight.PREFLIGHT_LEDGER
        self.assertTrue(ledger.is_file())
        preflighted = [
            json.loads(line)["role"]
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(list(declaration.active_roles), preflighted)
        inactive = set(declaration.allowed_roles) - set(declaration.active_roles)
        self.assertTrue(inactive.isdisjoint(set(preflighted)))

    def test_direct_l1_deleted_preflight_does_not_grow_permits(self) -> None:
        task_id, task, paths = self._direct_l1_task()
        ledger = self.root / task_id / preflight.PREFLIGHT_LEDGER
        if ledger.is_file():
            ledger.unlink()
        permit_path = self.root / task_id / policy.DISPATCH_PERMIT_LEDGER
        before = permit_path.read_bytes() if permit_path.is_file() else b""
        declaration = declarations.load_route_declaration(
            workflow.WorkflowStore(self.root), task_id
        )
        self.assertIsNotNone(declaration)
        role = declaration.active_roles[0]
        popen = self._l1_popen()
        with (
            mock.patch.object(workflow, "working_tree_paths", return_value=set()),
            mock.patch.object(
                workflow, "capture_repo", return_value=workflow.RepoSnapshot("h" * 40, ())
            ),
            mock.patch.object(workflow.subprocess, "Popen", popen),
        ):
            with self.assertRaisesRegex(workflow.WorkflowError, "ROLE_NOT_PREFLIGHTED"):
                workflow.run_codex(role, task, "task contract", paths)
        self.assertEqual([], popen.calls)
        after = permit_path.read_bytes() if permit_path.is_file() else b""
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
