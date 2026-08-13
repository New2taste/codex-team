import hashlib
import json
import multiprocessing
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from scripts import ai_workflow_team_call as team


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

        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.registry.execute_once(call, intent, lambda receipt: (_ for _ in ()).throw(RuntimeError("boom")))

        rows = [json.loads(line) for line in (self.root / "team-calls.jsonl").read_text().splitlines()]
        self.assertEqual(["TEAM_CALL_RECEIVED", "TEAM_CALL_ROUTED"], [row["event"] for row in rows])
        self.assertEqual("BLOCKED", rows[-1]["route_status"])
        self.assertEqual(("FIXED_L0_ALLOWLIST",), tuple(rows[-1]["risk_reasons"]))
        self.assertIsNone(rows[-1]["result_sha256"])

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


if __name__ == "__main__":
    unittest.main()
