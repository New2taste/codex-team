"""Distribution and companion-agent lifecycle contracts for ai-workflow."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON311 = Path("/Users/lee/.local/bin/python3.11")
PLUGIN = ROOT / "plugins" / "ai-workflow"
TEMPLATE = PLUGIN / "agents" / "luna-worker.toml"
INSTALL = PLUGIN / "scripts" / "install-agents.sh"
UNINSTALL = PLUGIN / "scripts" / "uninstall-agents.sh"
STATE_NAME = ".ai-workflow-luna-worker.state"
BACKUP_NAME = ".ai-workflow-luna-worker.backup"
KNOWN_LEGACY_SHA256 = "60f7240ea662cd27ea0f51f2e1efa8a2e788e16c76b04a13ab1c1df4f26ef024"
KNOWN_LEGACY_TEMPLATE = '''name = "luna_worker"
description = "处理由主代理明确委派的、范围有限、边界清晰且可独立完成的任务；适合只读盘点、机械核对、局部实现与独立验证，不负责改变总体目标或扩大任务范围。"
model = "gpt-5.6-luna"
model_reasoning_effort = "max"

developer_instructions = """
你是 luna_worker，一个只接受主代理明确、有界且可独立完成委派的执行型子代理。

工作边界：
- 只处理委派消息中明确列出的目标、输入、文件范围、允许动作和交付物。
- 不修改总目标、验收标准或工作范围，不把相邻发现并入施工。
- 优先读取所在仓库的持久规则和任务信封；既有修改和未跟踪文件视为他人资产，不覆盖、不删除、不夹带。
- 只修改明确授权的文件；未经授权，不提交、合并、推送、删除或执行不可逆操作。
- 需要宪法、PIT、安全、跨卡契约或开放式判断时立即停止并回交主代理。

证据要求：
- 按任务信封的 L0/L1/L2 要求交付最小证据包，并将事实、推断和建议分开。
- L0 保存命令、退出码和产物；L1 最多 5 条关键主张、每条最小证据、最关键结论 1 次交叉检查并列盲区；L2 提供目标测试、1 个有效负向样例或变异、diff 范围核对并列盲区。
- 找不到文件、命令、字段或证据时如实报告，不猜测、不伪造，不用“应该通过”代替实测。
- 若有文件改动，逐文件核对授权范围与 diff；若为只读任务，明确声明未修改文件。

交付纪律：
- 结论只能是 SUPPORTED、PARTIALLY_SUPPORTED、NOT_SUPPORTED 或 BLOCKED。
- 不声称最终验收、用户批准、合并或生效；自检仅供主代理独立复核，不构成最终验收。
- 先给结论，再列已完成内容、最小证据、验证结果、盲区和未执行事项。
"""
'''.encode()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


class DistributionContractTest(unittest.TestCase):
    def test_plugin_manifest_and_marketplace_are_versioned(self):
        manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
        marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
        self.assertEqual("ai-workflow", manifest["name"])
        self.assertEqual("0.2.0", manifest["version"])
        self.assertEqual("./skills/", manifest["skills"])
        self.assertEqual("ai-workflow", marketplace["name"])
        self.assertEqual(
            "./plugins/ai-workflow", marketplace["plugins"][0]["source"]["path"]
        )

    def test_project_and_release_agent_templates_are_byte_exact(self):
        self.assertEqual(
            (ROOT / ".codex" / "agents" / "luna-worker.toml").read_bytes(),
            TEMPLATE.read_bytes(),
        )

    def test_luna_template_matches_the_project_contract(self):
        with TEMPLATE.open("rb") as handle:
            agent = tomllib.load(handle)
        self.assertEqual("luna_worker", agent["name"])
        self.assertEqual("gpt-5.6-luna", agent["model"])
        self.assertEqual("max", agent["model_reasoning_effort"])
        self.assertIn("L0/L1/L2", agent["developer_instructions"])
        self.assertNotIn("ACCEPTED", agent["developer_instructions"])

    def test_release_runtime_and_schema_copies_are_byte_exact(self):
        configs = (
            "ai_workflow.toml",
            "ai_workflow_task.schema.json",
            "ai_workflow_result.schema.json",
            "ai_workflow_route_request.schema.json",
            "ai_workflow_route_decision.schema.json",
            "ai_workflow_plan.schema.json",
            "ai_workflow_runtime_evidence.schema.json",
            "ai_workflow_cost_evidence.schema.json",
        )
        runtimes = (
            "ai_workflow.py",
            "ai_workflow_artifacts.py",
            "ai_workflow_routing.py",
            "ai_workflow_planning.py",
            "ai_workflow_runtime.py",
            "ai_workflow_costs.py",
        )
        for name in configs:
            self.assertEqual((ROOT / "config" / name).read_bytes(), (PLUGIN / "config" / name).read_bytes())
        for name in runtimes:
            self.assertEqual((ROOT / "scripts" / name).read_bytes(), (PLUGIN / "runtime" / name).read_bytes())
        task_schema = json.loads((PLUGIN / "config" / "ai_workflow_task.schema.json").read_text())
        self.assertIn("paired_case_id", task_schema["properties"])

    def test_known_legacy_fixture_matches_the_registered_digest(self):
        self.assertEqual(KNOWN_LEGACY_SHA256, hashlib.sha256(KNOWN_LEGACY_TEMPLATE).hexdigest())


class AgentLifecycleTest(unittest.TestCase):
    def run_script(self, script: Path, target: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.pop("CODEX_HOME", None)
        environment["PYTHON_BIN"] = str(PYTHON311)
        return subprocess.run(
            ["sh", str(script), "--target-dir", str(target), *extra],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def install(self, target: Path, *, check: bool = False) -> subprocess.CompletedProcess[str]:
        extra = ("--check",) if check else ()
        return self.run_script(INSTALL, target, *extra)

    def uninstall(self, target: Path) -> subprocess.CompletedProcess[str]:
        return self.run_script(UNINSTALL, target)

    def test_missing_install_and_check_are_isolated_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            before = tree_hashes(root)
            checked = self.install(target, check=True)
            self.assertNotEqual(0, checked.returncode, checked.stderr)
            self.assertEqual(before, tree_hashes(root))

            installed = self.install(target)
            self.assertEqual(0, installed.returncode, installed.stderr)
            destination = target / "luna-worker.toml"
            self.assertEqual(TEMPLATE.read_bytes(), destination.read_bytes())
            self.assertTrue((target / STATE_NAME).is_file())

            before_check = tree_hashes(target)
            current = self.install(target, check=True)
            self.assertEqual(0, current.returncode, current.stderr)
            self.assertEqual(before_check, tree_hashes(target))
            repeated = self.install(target)
            self.assertEqual(0, repeated.returncode, repeated.stderr)
            self.assertEqual(before_check, tree_hashes(target))

    def test_known_legacy_is_migrated_without_touching_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            destination = target / "luna-worker.toml"
            destination.write_bytes(KNOWN_LEGACY_TEMPLATE)
            unrelated = target / "unrelated.toml"
            unrelated.write_text('name = "unrelated"\n')
            before_hashes = tree_hashes(target)

            installed = self.install(target)
            self.assertEqual(0, installed.returncode, installed.stderr)
            self.assertEqual(TEMPLATE.read_bytes(), destination.read_bytes())
            self.assertEqual(sha256(unrelated), before_hashes["unrelated.toml"])
            self.assertTrue((target / STATE_NAME).is_file())

    def test_conflict_is_preserved_and_check_does_not_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            destination = target / "luna-worker.toml"
            conflicting_bytes = b'name = "user_owned"\n'
            destination.write_bytes(conflicting_bytes)
            before_hashes = tree_hashes(target)

            checked = self.install(target, check=True)
            self.assertNotEqual(0, checked.returncode)
            self.assertEqual(before_hashes, tree_hashes(target))
            installed = self.install(target)
            self.assertNotEqual(0, installed.returncode)
            self.assertEqual(conflicting_bytes, destination.read_bytes())
            self.assertEqual(before_hashes, tree_hashes(target))

    def test_unsafe_symlink_destination_and_target_directory_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            target.mkdir()
            protected = root / "protected.toml"
            protected.write_text('name = "protected"\n')
            (target / "luna-worker.toml").symlink_to(protected)
            before = tree_hashes(root)
            self.assertNotEqual(0, self.install(target).returncode)
            self.assertEqual(before, tree_hashes(root))
            self.assertEqual('name = "protected"\n', protected.read_text())

            linked_target = root / "linked-agents"
            linked_target.symlink_to(target, target_is_directory=True)
            self.assertNotEqual(0, self.install(linked_target).returncode)
            self.assertEqual(before, tree_hashes(root))

    def test_unreadable_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            destination = target / "luna-worker.toml"
            destination.write_text('name = "unreadable"\n')
            destination.chmod(0)
            try:
                self.assertNotEqual(0, self.install(target).returncode)
                self.assertFalse((target / STATE_NAME).exists())
            finally:
                destination.chmod(0o600)

    def test_uninstall_requires_owned_unchanged_state_and_never_recurses(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            unrelated = target / "keep.toml"
            unrelated.write_text('name = "keep"\n')
            self.assertEqual(0, self.install(target).returncode)
            destination = target / "luna-worker.toml"
            state = json.loads((target / STATE_NAME).read_text())
            self.assertEqual({"plugin_version", "target_filename", "installed_sha256", "installed_at_utc", "backup_sha256"}, set(state))
            self.assertEqual("luna-worker.toml", state["target_filename"])
            self.assertEqual(sha256(destination), state["installed_sha256"])
            self.assertEqual(0, self.uninstall(target).returncode)
            self.assertFalse(destination.exists())
            self.assertFalse((target / STATE_NAME).exists())
            self.assertEqual('name = "keep"\n', unrelated.read_text())

            self.assertEqual(0, self.install(target).returncode)
            destination.write_text('name = "modified"\n')
            self.assertNotEqual(0, self.uninstall(target).returncode)
            self.assertTrue(destination.exists())
            self.assertFalse((target / BACKUP_NAME).exists())


if __name__ == "__main__":
    unittest.main()
