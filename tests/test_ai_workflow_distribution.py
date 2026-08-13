"""Distribution and companion-agent lifecycle contracts for ai-workflow."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PYTHON311 = Path("/Users/lee/.local/bin/python3.11")
PLUGIN = ROOT / "plugins" / "ai-workflow"
TEMPLATE = PLUGIN / "agents" / "luna-max.toml"
INSTALL = PLUGIN / "scripts" / "install-agents.sh"
UNINSTALL = PLUGIN / "scripts" / "uninstall-agents.sh"
LIFECYCLE_HELPER = PLUGIN / "scripts" / "agent_lifecycle.py"
TARGET_NAME = "luna-max.toml"
STATE_NAME = ".ai-workflow-luna-max.state"
BACKUP_NAME = ".ai-workflow-luna-max.backup"
LEGACY_TARGET_NAME = "luna-worker.toml"
LEGACY_STATE_NAME = ".ai-workflow-luna-worker.state"
LEGACY_BACKUP_NAME = ".ai-workflow-luna-worker.backup"
KNOWN_LEGACY_SHA256 = "60f7240ea662cd27ea0f51f2e1efa8a2e788e16c76b04a13ab1c1df4f26ef024"
CANONICAL_TEMPLATE_SHA256 = "6237649deb278392111355490a9c71c00be66388c6fb25435694d00eb6f18bbb"
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
LEGACY_BYTES = KNOWN_LEGACY_TEMPLATE


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def filesystem_snapshot(root: Path) -> dict[str, dict[str, object]]:
    """Capture every entry type and content without following symlinks."""

    if not root.exists() and not root.is_symlink():
        return {}
    result: dict[str, dict[str, object]] = {}

    def visit(path: Path, name: str) -> None:
        status = path.lstat()
        if path.is_symlink():
            result[name] = {"type": "symlink", "target": os.readlink(path)}
            return
        if path.is_dir():
            result[name] = {"type": "directory", "mode": status.st_mode & 0o777}
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                visit(child, f"{name}/{child.name}" if name else child.name)
            return
        if path.is_file():
            try:
                content = path.read_bytes()
            except OSError as exc:
                result[name] = {
                    "type": "regular-unreadable",
                    "mode": status.st_mode & 0o777,
                    "error": type(exc).__name__,
                }
            else:
                result[name] = {
                    "type": "regular",
                    "mode": status.st_mode & 0o777,
                    "content": content,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            return
        result[name] = {"type": "other", "mode": status.st_mode & 0o777}

    visit(root, "")
    return result


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        name: value["sha256"]
        for name, value in filesystem_snapshot(root).items()
        if value["type"] == "regular"
    }


def load_lifecycle_helper():
    spec = importlib.util.spec_from_file_location("ai_workflow_lifecycle", LIFECYCLE_HELPER)
    if spec is None or spec.loader is None:
        raise AssertionError("lifecycle helper is not importable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_owned_state(target: Path, backup_sha256: str | None = None) -> None:
    (target / STATE_NAME).write_text(
        json.dumps(
            {
                "plugin_version": "0.2.0",
                "target_filename": TARGET_NAME,
                "installed_sha256": CANONICAL_TEMPLATE_SHA256,
                "installed_at_utc": "2026-08-08T06:38:00Z",
                "backup_sha256": backup_sha256,
            },
            separators=(",", ":"),
        )
        + "\n"
    )


def prepare_known_legacy(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / LEGACY_TARGET_NAME).write_bytes(KNOWN_LEGACY_TEMPLATE)


def write_verified_legacy_state(target: Path, backup_sha256: str | None = None) -> None:
    (target / LEGACY_STATE_NAME).write_text(
        json.dumps(
            {
                "plugin_version": "0.2.0",
                "target_filename": LEGACY_TARGET_NAME,
                "installed_sha256": KNOWN_LEGACY_SHA256,
                "installed_at_utc": "2026-08-08T06:38:00Z",
                "backup_sha256": backup_sha256,
            },
            separators=(",", ":"),
        )
        + "\n"
    )


def write_verified_legacy_install(target: Path, backup: bytes | None = None) -> None:
    prepare_known_legacy(target)
    backup_sha256 = None
    if backup is not None:
        (target / LEGACY_BACKUP_NAME).write_bytes(backup)
        backup_sha256 = hashlib.sha256(backup).hexdigest()
    write_verified_legacy_state(target, backup_sha256)


def raise_on_state_hook(exception_type: type[BaseException], point: str):
    def hook(observed: str) -> None:
        if observed == point:
            raise exception_type("injected")

    return hook


def raise_on_missing_state(exception_type: type[BaseException]):
    return raise_on_state_hook(exception_type, "install.before_publish_missing_state")


def raise_on_known_legacy_state(exception_type: type[BaseException]):
    return raise_on_state_hook(
        exception_type, "install.before_publish_legacy_state"
    )


def raise_after_missing_state_publish(exception_type: type[BaseException]):
    return raise_on_state_hook(exception_type, "install.after_publish_missing_state")


def raise_after_known_legacy_state_publish(exception_type: type[BaseException]):
    return raise_on_state_hook(
        exception_type, "install.after_publish_legacy_state"
    )


def raise_from_state_publish(lifecycle, exception_type: type[BaseException]):
    original_publish = lifecycle._publish_no_clobber

    def publish(*arguments):
        if arguments[-1] == STATE_NAME:
            raise exception_type("injected")
        return original_publish(*arguments)

    return mock.patch.object(lifecycle, "_publish_no_clobber", side_effect=publish)


def patch_after_first_snapshot(lifecycle, name: str, replacement) -> mock._patch:
    helper_name = (
        "_read_regular_identity"
        if hasattr(lifecycle, "_read_regular_identity")
        else "_read_regular"
    )
    original = getattr(lifecycle, helper_name)
    injected = False

    def read(directory: int, observed_name: str):
        nonlocal injected
        result = original(directory, observed_name)
        if observed_name == name and not injected:
            injected = True
            replacement()
        return result

    return mock.patch.object(lifecycle, helper_name, side_effect=read)


def temporary_directory():
    """Create below the physical system temp root on macOS and Linux."""

    return tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())


def open_fd_count() -> int:
    for candidate in (Path("/dev/fd"), Path("/proc/self/fd")):
        if candidate.is_dir():
            return len(os.listdir(candidate))
    raise unittest.SkipTest("open descriptor inventory is unavailable")


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
        root_template = ROOT / ".codex" / "agents" / "luna-max.toml"
        self.assertTrue(root_template.is_file())
        self.assertTrue(TEMPLATE.is_file())
        self.assertEqual(
            root_template.read_bytes(),
            TEMPLATE.read_bytes(),
        )
        self.assertFalse((ROOT / ".codex" / "agents" / LEGACY_TARGET_NAME).exists())
        self.assertFalse((PLUGIN / "agents" / LEGACY_TARGET_NAME).exists())

    def test_luna_template_matches_the_project_contract(self):
        self.assertTrue(TEMPLATE.is_file())
        with TEMPLATE.open("rb") as handle:
            agent = tomllib.load(handle)
        self.assertEqual("luna_max", agent["name"])
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
            "ai_workflow_repairs.py",
        )
        for name in configs:
            self.assertEqual((ROOT / "config" / name).read_bytes(), (PLUGIN / "config" / name).read_bytes())
        for name in runtimes:
            self.assertEqual((ROOT / "scripts" / name).read_bytes(), (PLUGIN / "runtime" / name).read_bytes())
        task_schema = json.loads((PLUGIN / "config" / "ai_workflow_task.schema.json").read_text())
        self.assertIn("paired_case_id", task_schema["properties"])

    def test_published_role_and_lifecycle_language_has_no_legacy_allocation(self):
        """The distributed docs expose the frozen role/lifecycle contract."""

        readme = (ROOT / "README.md").read_text(encoding="utf-8").casefold()
        skill = (PLUGIN / "skills" / "orchestration" / "SKILL.md").read_text(
            encoding="utf-8"
        ).casefold()
        metadata = (PLUGIN / "skills" / "orchestration" / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        ).casefold()
        published = "\n".join((readme, skill, metadata))

        self.assertIn("luna_max", published)
        self.assertIn("luna max", published)
        self.assertNotRegex(published, r"(?:require|invoke|select).*luna_worker")

        for source_name, source in (("README", readme), ("orchestration skill", skill)):
            legacy_lines = tuple(
                line for line in source.splitlines() if "luna_worker" in line
            )
            self.assertTrue(
                legacy_lines,
                f"{source_name} must retain one explicit installer migration mention",
            )
            for line in legacy_lines:
                self.assertRegex(
                    line,
                    r"(?:migration|迁移)",
                    f"legacy identifier outside migration language: {line}",
                )
                self.assertNotRegex(
                    line,
                    r"(?:require|invoke|select|生成|调用|选择|角色)",
                    f"legacy identifier presented as an execution role: {line}",
                )

        self.assertNotRegex(
            published,
            r"luna_worker[^\n]{0,180}(?:execution|agent|role|invoke|select|require|调用|选择|生成)",
        )

        for phrase in (
            "luna max",
            "frozen envelope",
            "mechanical",
            "distribution",
            "terra xhigh",
            "complex construction",
            "independent",
            "adversarial review",
            "sol medium",
            "final",
            "acceptance",
            "sol xhigh",
            "escalation",
            "terra medium",
            "sol high",
            "no default role",
        ):
            self.assertIn(phrase, published, phrase)

        for stale_phrase in (
            "验收预审",
            "开放式、高风险语义任务直接转 sol medium",
            "必须由 sol medium 先冻结规格",
            "sol medium 无法在闭集选项中稳定裁定",
            "1 次同角色实现返工",
            "两轮 terra xhigh owner-repair/review",
            "第一次独立 terra review 后修复，第二次独立 terra review 再验证",
        ):
            self.assertNotIn(stale_phrase, published, stale_phrase)

        for lifecycle_phrase in (
            "复杂或高风险语义任务默认转 terra xhigh",
            "owner-authorized sol xhigh 规划",
            "初次提交由独立 terra xhigh adversarial review",
            "若 rework",
            "luna max 或 terra xhigh",
            "原 owner 在原 envelope 内第一次返工",
            "第二次提交由另一独立 terra xhigh review",
            "仍失败才可",
            "第二次 terra xhigh 失败后的冻结梯级",
            "scoped sol-medium repair + different sol-medium peer",
            "sol-xhigh terminal repair",
            "无 task-level review",
        ):
            self.assertIn(lifecycle_phrase, published, lifecycle_phrase)

        root_policy = tomllib.loads((ROOT / "config" / "ai_workflow.toml").read_text())
        plugin_policy = tomllib.loads(
            (PLUGIN / "config" / "ai_workflow.toml").read_text()
        )
        for config in (root_policy, plugin_policy):
            self.assertEqual(2, config["policy"]["max_implementation_reworks"])

        self.assertNotIn("execution os remains terra-led", published)
        self.assertNotIn("luna is only a low-cost bounded tool process", published)
        self.assertNotIn("luna max 卡面预审", published)
        self.assertNotIn("luna max pre-review", published)
        self.assertRegex(
            published,
            r"luna[^\n]{0,220}(?:never|must not|does not)[^\n]{0,80}(?:review|accept)",
        )

        root_config = tomllib.loads((ROOT / "config" / "ai_workflow.toml").read_text())
        plugin_config = tomllib.loads(
            (PLUGIN / "config" / "ai_workflow.toml").read_text()
        )
        for config in (root_config, plugin_config):
            role_names = set(config["roles"])
            self.assertNotIn("terra_medium", role_names)
            self.assertNotIn("sol_high", role_names)
            self.assertIn("luna_construction", role_names)
            self.assertIn("terra_xhigh_reviewer", role_names)
            self.assertIn("sol_medium_reviewer", role_names)
            self.assertIn("sol_xhigh_planner", role_names)

    def test_sol_xhigh_terminal_repair_is_narrow_exception_to_construction_ban(self):
        """Section 7.4 forbids ordinary construction without erasing the terminal repair."""

        readme = (ROOT / "README.md").read_text(encoding="utf-8").casefold()
        match = re.search(
            r"^### 7\.4 sol xhigh[ \t]*$\n(?P<body>.*?)(?=^### |\Z)",
            readme,
            flags=re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(match, "README section 7.4 must remain published")
        sol_xhigh_contract = match.group("body").strip()
        clauses = tuple(
            clause.strip()
            for clause in re.split(r"[\n。；：]+", sol_xhigh_contract)
            if clause.strip()
        )

        self.assertIn(
            "不得自动启动或承担普通、常驻 construction",
            sol_xhigh_contract,
        )
        self.assertIn(
            "distinct sol-medium peer 对 scoped fallback 给出 rework 后",
            sol_xhigh_contract,
        )
        self.assertIn(
            "owner-authorized、assignment-scoped、一次性的 terminal repair",
            sol_xhigh_contract,
        )
        self.assertIn("该 terminal repair 无 task-level review", sol_xhigh_contract)
        self.assertIn(
            "不得据此泛化为普通 sol-xhigh construction",
            sol_xhigh_contract,
        )
        self.assertNotIn("不得自动启动、施工", sol_xhigh_contract)

        positive_construction_authorization = re.compile(
            r"(?:构成施工例外|(?:也)?可(?:以)?(?:承担|执行|负责)?|"
            r"允许|授权|获准|有权|负责|承担|"
            r"\b(?:may|can|allowed to|authorized to|responsible for)\b)"
        )
        construction_authorizations = tuple(
            clause
            for clause in clauses
            if re.search(r"(?:construction|施工)", clause)
            and positive_construction_authorization.search(clause)
            and not re.search(r"(?:不得|禁止|不允许|不可|不能|无权)", clause)
        )
        self.assertEqual(
            1,
            len(construction_authorizations),
            "section 7.4 must not grant a second construction exception",
        )
        sole_construction_exception = construction_authorizations[0]
        self.assertTrue(
            sole_construction_exception.startswith("只有"),
            "the terminal repair must remain the only construction exception",
        )
        self.assertIn("上述 terminal repair", sole_construction_exception)
        self.assertIn("构成施工例外", sole_construction_exception)

        ordinary_construction_clauses = tuple(
            clause
            for clause in clauses
            if re.search(r"(?:普通|常驻)", clause)
            and re.search(r"(?:construction|施工)", clause)
        )
        for clause in ordinary_construction_clauses:
            with self.subTest(ordinary_construction_clause=clause):
                self.assertRegex(
                    clause,
                    r"(?:不得|禁止|不允许|不可|不能|无权)",
                    "ordinary Sol-xhigh construction must remain prohibited",
                )

    def test_plugin_verifier_rejects_a_tampered_mirrored_runtime_copy(self):
        """A copied release must fail verification when one mirror is changed."""

        with tempfile.TemporaryDirectory() as temporary:
            release_root = Path(temporary) / "release"
            shutil.copytree(ROOT / ".codex", release_root / ".codex")
            shutil.copytree(ROOT / "config", release_root / "config")
            shutil.copytree(ROOT / "scripts", release_root / "scripts")
            shutil.copytree(ROOT / "plugins" / "ai-workflow", release_root / "plugins" / "ai-workflow")
            verifier = release_root / "plugins" / "ai-workflow" / "scripts" / "verify.sh"

            clean = subprocess.run(
                ["sh", str(verifier)],
                cwd=release_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, clean.returncode, clean.stderr)

            mirrored = release_root / "plugins" / "ai-workflow" / "runtime" / "ai_workflow.py"
            mirrored.write_bytes(mirrored.read_bytes() + b"\n# tampered mirror\n")
            tampered = subprocess.run(
                ["sh", str(verifier)],
                cwd=release_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(0, tampered.returncode)

    def test_known_legacy_fixture_matches_the_registered_digest(self):
        self.assertEqual(KNOWN_LEGACY_SHA256, hashlib.sha256(KNOWN_LEGACY_TEMPLATE).hexdigest())

    def test_release_template_digest_is_pinned(self):
        self.assertTrue(TEMPLATE.is_file())
        self.assertEqual(CANONICAL_TEMPLATE_SHA256, sha256(TEMPLATE))

    def test_plugin_verifier_rejects_a_legacy_agent_template_in_a_copied_release(self):
        """A release must not ship the old template alongside the canonical one."""

        with tempfile.TemporaryDirectory() as temporary:
            release_root = Path(temporary) / "release"
            shutil.copytree(ROOT / ".codex", release_root / ".codex")
            shutil.copytree(ROOT / "config", release_root / "config")
            shutil.copytree(ROOT / "scripts", release_root / "scripts")
            shutil.copytree(
                ROOT / "plugins" / "ai-workflow",
                release_root / "plugins" / "ai-workflow",
            )
            legacy = release_root / "plugins" / "ai-workflow" / "agents" / LEGACY_TARGET_NAME
            legacy.write_bytes(KNOWN_LEGACY_TEMPLATE)

            result = subprocess.run(
                ["sh", str(release_root / "plugins" / "ai-workflow" / "scripts" / "verify.sh")],
                cwd=release_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertNotEqual(0, result.returncode, result.stderr)

    def test_plugin_verifier_rejects_a_dangling_plugin_legacy_template(self):
        """A dangling old Plugin template is still a shipped legacy entry."""

        with tempfile.TemporaryDirectory() as temporary:
            release_root = Path(temporary) / "release"
            shutil.copytree(ROOT / ".codex", release_root / ".codex")
            shutil.copytree(ROOT / "config", release_root / "config")
            shutil.copytree(ROOT / "scripts", release_root / "scripts")
            shutil.copytree(
                ROOT / "plugins" / "ai-workflow",
                release_root / "plugins" / "ai-workflow",
            )
            legacy = release_root / "plugins" / "ai-workflow" / "agents" / LEGACY_TARGET_NAME
            legacy.symlink_to("missing-luna-worker.toml")
            self.assertTrue(legacy.is_symlink())
            self.assertFalse(legacy.exists())

            result = subprocess.run(
                ["sh", str(release_root / "plugins" / "ai-workflow" / "scripts" / "verify.sh")],
                cwd=release_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(0, result.returncode, result.stderr)

    def test_plugin_verifier_rejects_a_dangling_root_legacy_template(self):
        """A dangling old root mirror is still a shipped legacy entry."""

        with tempfile.TemporaryDirectory() as temporary:
            release_root = Path(temporary) / "release"
            shutil.copytree(ROOT / ".codex", release_root / ".codex")
            shutil.copytree(ROOT / "config", release_root / "config")
            shutil.copytree(ROOT / "scripts", release_root / "scripts")
            shutil.copytree(
                ROOT / "plugins" / "ai-workflow",
                release_root / "plugins" / "ai-workflow",
            )
            legacy = release_root / ".codex" / "agents" / LEGACY_TARGET_NAME
            legacy.symlink_to("missing-luna-worker.toml")
            self.assertTrue(legacy.is_symlink())
            self.assertFalse(legacy.exists())

            result = subprocess.run(
                ["sh", str(release_root / "plugins" / "ai-workflow" / "scripts" / "verify.sh")],
                cwd=release_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(0, result.returncode, result.stderr)


class AgentLifecycleTest(unittest.TestCase):
    def run_wrapper(
        self,
        script: Path,
        *arguments: str,
        home: Path,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.pop("CODEX_HOME", None)
        environment["HOME"] = str(home)
        environment["PYTHON_BIN"] = str(PYTHON311)
        return subprocess.run(
            ["sh", str(script), *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

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

    def test_verified_luna_worker_install_migrates_to_luna_max_atomically(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            write_verified_legacy_install(target)

            installed = self.install(target)

            self.assertEqual(0, installed.returncode, installed.stderr)
            self.assertFalse((target / LEGACY_TARGET_NAME).exists())
            self.assertFalse((target / LEGACY_STATE_NAME).exists())
            self.assertFalse((target / LEGACY_BACKUP_NAME).exists())
            self.assertEqual(TEMPLATE.read_bytes(), (target / TARGET_NAME).read_bytes())
            state = json.loads((target / STATE_NAME).read_text())
            self.assertEqual(TARGET_NAME, state["target_filename"])
            self.assertEqual(CANONICAL_TEMPLATE_SHA256, state["installed_sha256"])

    def test_verified_unmanaged_luna_worker_install_migrates_to_luna_max(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            prepare_known_legacy(target)

            installed = self.install(target)

            self.assertEqual(0, installed.returncode, installed.stderr)
            self.assertFalse((target / LEGACY_TARGET_NAME).exists())
            self.assertEqual(TEMPLATE.read_bytes(), (target / TARGET_NAME).read_bytes())
            self.assertTrue((target / STATE_NAME).is_file())

    def test_verified_legacy_backup_migrates_and_uninstall_restores_it(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            backup = b'name = "user_backup"\n'
            write_verified_legacy_install(target, backup)

            self.assertEqual(0, self.install(target).returncode)
            self.assertEqual(backup, (target / BACKUP_NAME).read_bytes())
            self.assertEqual(0, self.uninstall(target).returncode)
            self.assertEqual(backup, (target / TARGET_NAME).read_bytes())
            self.assertFalse((target / STATE_NAME).exists())
            self.assertFalse((target / BACKUP_NAME).exists())

    def test_legacy_and_canonical_entries_fail_closed(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            write_verified_legacy_install(target)
            canonical = target / TARGET_NAME
            canonical.write_bytes(b'name = "user_owned"\n')
            before = filesystem_snapshot(target)

            result = self.install(target)

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(before, filesystem_snapshot(target))

    def test_uninstall_rejects_a_complete_canonical_install_with_legacy_entries(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            self.assertEqual(0, self.install(target).returncode)
            (target / LEGACY_TARGET_NAME).write_bytes(KNOWN_LEGACY_TEMPLATE)
            before = filesystem_snapshot(target)

            result = self.uninstall(target)

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(before, filesystem_snapshot(target))

    def test_unverified_or_symlinked_legacy_input_is_preserved(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            for name in ("unverified", "symlink"):
                with self.subTest(name=name):
                    target = root / name / "agents"
                    target.mkdir(parents=True)
                    if name == "symlink":
                        (root / "protected.toml").write_bytes(b"protected\n")
                        (target / LEGACY_TARGET_NAME).symlink_to(root / "protected.toml")
                    else:
                        (target / LEGACY_TARGET_NAME).write_bytes(b"user owned\n")
                    before = filesystem_snapshot(root)
                    self.assertNotEqual(0, self.install(target).returncode)
                    self.assertEqual(before, filesystem_snapshot(root))

    def test_invalid_legacy_state_or_backup_is_preserved(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            for name in ("state", "backup"):
                with self.subTest(name=name):
                    target = root / name / "agents"
                    write_verified_legacy_install(target, b'name = "legacy_backup"\n')
                    if name == "state":
                        (target / LEGACY_STATE_NAME).write_bytes(b'{"owner":"user"}\n')
                    else:
                        (target / LEGACY_BACKUP_NAME).write_bytes(b"tampered backup\n")
                    before = filesystem_snapshot(target)

                    result = self.install(target)

                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual(before, filesystem_snapshot(target))
                    self.assertFalse((target / TARGET_NAME).exists())
                    self.assertFalse((target / STATE_NAME).exists())

    def test_legacy_check_is_read_only_and_not_current(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            write_verified_legacy_install(target)
            before = filesystem_snapshot(target)

            checked = self.install(target, check=True)

            self.assertNotEqual(0, checked.returncode)
            self.assertEqual(before, filesystem_snapshot(target))

    def test_legacy_migration_state_failure_restores_every_original_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            write_verified_legacy_install(target, b'name = "legacy_backup"\n')
            before = filesystem_snapshot(target)

            result = lifecycle.install(
                target,
                hook=raise_on_state_hook(OSError, "install.before_publish_legacy_state"),
            )

            self.assertEqual(1, result)
            self.assertEqual(before, filesystem_snapshot(target))
            self.assertFalse((target / TARGET_NAME).exists())

    def test_legacy_migration_rejects_a_same_digest_replacement_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            prepare_known_legacy(target)
            replacement_inode: int | None = None

            def replace_after_snapshot() -> None:
                nonlocal replacement_inode
                replacement = root / "same-digest-legacy-replacement"
                replacement.write_bytes(KNOWN_LEGACY_TEMPLATE)
                replacement_inode = replacement.stat().st_ino
                os.replace(replacement, target / LEGACY_TARGET_NAME)

            with patch_after_first_snapshot(
                lifecycle, LEGACY_TARGET_NAME, replace_after_snapshot
            ):
                self.assertEqual(1, lifecycle.install(target))

            self.assertIsNotNone(replacement_inode)
            self.assertEqual(replacement_inode, (target / LEGACY_TARGET_NAME).stat().st_ino)
            self.assertFalse((target / TARGET_NAME).exists())

    def test_missing_install_and_check_are_isolated_and_idempotent(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            before = tree_hashes(root)
            checked = self.install(target, check=True)
            self.assertNotEqual(0, checked.returncode, checked.stderr)
            self.assertEqual(before, tree_hashes(root))

            installed = self.install(target)
            self.assertEqual(0, installed.returncode, installed.stderr)
            destination = target / TARGET_NAME
            self.assertEqual(TEMPLATE.read_bytes(), destination.read_bytes())
            self.assertTrue((target / STATE_NAME).is_file())
            state = json.loads((target / STATE_NAME).read_text())
            self.assertEqual(sha256(destination), state["installed_sha256"])
            self.assertRegex(
                state["installed_at_utc"],
                r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
            )

            before_check = tree_hashes(target)
            current = self.install(target, check=True)
            self.assertEqual(0, current.returncode, current.stderr)
            self.assertEqual(before_check, tree_hashes(target))
            repeated = self.install(target)
            self.assertEqual(0, repeated.returncode, repeated.stderr)
            self.assertEqual(before_check, tree_hashes(target))

    def test_known_legacy_is_migrated_without_touching_unrelated_files(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            destination = target / LEGACY_TARGET_NAME
            destination.write_bytes(KNOWN_LEGACY_TEMPLATE)
            unrelated = target / "unrelated.toml"
            unrelated.write_text('name = "unrelated"\n')
            before_hashes = tree_hashes(target)

            installed = self.install(target)
            self.assertEqual(0, installed.returncode, installed.stderr)
            self.assertFalse(destination.exists())
            self.assertEqual(TEMPLATE.read_bytes(), (target / TARGET_NAME).read_bytes())
            self.assertEqual(sha256(unrelated), before_hashes["unrelated.toml"])
            self.assertTrue((target / STATE_NAME).is_file())

    def test_conflict_is_preserved_and_check_does_not_write(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            destination = target / TARGET_NAME
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
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            target.mkdir()
            protected = root / "protected.toml"
            protected.write_text('name = "protected"\n')
            (target / TARGET_NAME).symlink_to(protected)
            before = tree_hashes(root)
            self.assertNotEqual(0, self.install(target).returncode)
            self.assertEqual(before, tree_hashes(root))
            self.assertEqual('name = "protected"\n', protected.read_text())

            linked_target = root / "linked-agents"
            linked_target.symlink_to(target, target_is_directory=True)
            self.assertNotEqual(0, self.install(linked_target).returncode)
            self.assertEqual(before, tree_hashes(root))

    def test_unreadable_destination_is_preserved(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            destination = target / TARGET_NAME
            destination.write_text('name = "unreadable"\n')
            destination.chmod(0)
            try:
                self.assertNotEqual(0, self.install(target).returncode)
                self.assertFalse((target / STATE_NAME).exists())
            finally:
                destination.chmod(0o600)

    def test_check_preserves_complete_filesystem_snapshot_for_every_class(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            cases: dict[str, Path] = {}

            cases["missing"] = root / "missing" / "agents"

            current = root / "current" / "agents"
            self.assertEqual(0, self.install(current).returncode)
            cases["current"] = current

            legacy = root / "legacy" / "agents"
            legacy.mkdir(parents=True)
            (legacy / LEGACY_TARGET_NAME).write_bytes(KNOWN_LEGACY_TEMPLATE)
            cases["known_legacy"] = legacy

            conflict = root / "conflict" / "agents"
            conflict.mkdir(parents=True)
            (conflict / TARGET_NAME).write_bytes(b'user = "owned"\n')
            cases["conflict"] = conflict

            unsafe = root / "unsafe" / "agents"
            unsafe.mkdir(parents=True)
            protected = root / "unsafe" / "protected.toml"
            protected.write_bytes(b"protected\n")
            (unsafe / TARGET_NAME).symlink_to(protected)
            cases["unsafe"] = unsafe

            unreadable = root / "unreadable" / "agents"
            unreadable.mkdir(parents=True)
            unreadable_file = unreadable / TARGET_NAME
            unreadable_file.write_bytes(b"unreadable\n")
            unreadable_file.chmod(0)
            cases["unreadable"] = unreadable
            try:
                for name, target in cases.items():
                    with self.subTest(name=name):
                        before = filesystem_snapshot(root)
                        checked = self.install(target, check=True)
                        self.assertEqual(name == "current", checked.returncode == 0)
                        self.assertEqual(before, filesystem_snapshot(root))
            finally:
                unreadable_file.chmod(0o600)

    def test_rejects_symlink_ancestor_for_install_and_uninstall(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            real_parent = root / "real-parent"
            target = real_parent / "agents"
            link_parent = root / "link-parent"
            real_parent.mkdir()
            link_parent.symlink_to(real_parent, target_is_directory=True)
            linked_target = link_parent / "agents"

            before_install = filesystem_snapshot(root)
            self.assertNotEqual(0, self.install(linked_target).returncode)
            self.assertEqual(before_install, filesystem_snapshot(root))

            self.assertEqual(0, self.install(target).returncode)
            before_uninstall = filesystem_snapshot(root)
            self.assertNotEqual(0, self.uninstall(linked_target).returncode)
            self.assertEqual(before_uninstall, filesystem_snapshot(root))

    def test_rejects_a_toml_valid_template_with_the_wrong_digest_before_mutation(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            original = TEMPLATE.read_bytes()
            try:
                TEMPLATE.write_bytes(original + b"\n")
                before = filesystem_snapshot(Path(temporary))
                result = self.install(target)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(before, filesystem_snapshot(Path(temporary)))
            finally:
                TEMPLATE.write_bytes(original)

    def test_missing_install_does_not_clobber_a_file_created_after_preflight(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            destination = target / TARGET_NAME
            user_bytes = b'name = "created_after_preflight"\n'

            def race(point: str) -> None:
                if point == "install.before_publish_missing":
                    target.mkdir(exist_ok=True)
                    replacement = root / "user-replacement.toml"
                    replacement.write_bytes(user_bytes)
                    os.replace(replacement, destination)

            self.assertNotEqual(0, lifecycle.install(target, hook=race))
            self.assertEqual(user_bytes, destination.read_bytes())
            self.assertFalse((target / STATE_NAME).exists())

    def test_wrappers_reject_an_explicit_empty_target_without_touching_defaults(self):
        operations = (
            ("install", INSTALL, ("--target-dir", "")),
            ("check", INSTALL, ("--target-dir", "", "--check")),
            ("uninstall", UNINSTALL, ("--target-dir", "")),
        )
        with temporary_directory() as temporary:
            root = Path(temporary)
            for name, script, arguments in operations:
                with self.subTest(operation=name):
                    home = root / name / "home"
                    default_target = home / ".codex" / "agents"
                    default_target.mkdir(parents=True)
                    (default_target / TARGET_NAME).write_bytes(TEMPLATE.read_bytes())
                    write_owned_state(default_target)
                    before = filesystem_snapshot(home)

                    result = self.run_wrapper(script, *arguments, home=home)

                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual(before, filesystem_snapshot(home))

    def test_missing_install_rolls_back_its_agent_when_state_publish_races(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            raced_state = b'{"owner":"user"}\n'

            def race(point: str) -> None:
                if point == "install.before_publish_missing_state":
                    (target / STATE_NAME).write_bytes(raced_state)

            self.assertNotEqual(0, lifecycle.install(target, hook=race))
            self.assertFalse((target / TARGET_NAME).exists())
            self.assertEqual(raced_state, (target / STATE_NAME).read_bytes())

    def test_missing_install_preserves_raced_agent_bytes_when_state_publish_races(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            destination = target / TARGET_NAME
            raced_state = b'{"owner":"user"}\n'
            user_bytes = b'name = "modified_during_state_race"\n'

            def race(point: str) -> None:
                if point == "install.before_publish_missing_state":
                    (target / STATE_NAME).write_bytes(raced_state)
                    replacement = root / "user-replacement.toml"
                    replacement.write_bytes(user_bytes)
                    os.replace(replacement, destination)

            self.assertNotEqual(0, lifecycle.install(target, hook=race))
            recoverable = []
            if destination.exists():
                recoverable.append(destination.read_bytes())
            recoverable.extend(
                payload.read_bytes()
                for payload in target.glob(".ai-workflow-tombstone-*/payload")
            )
            self.assertIn(user_bytes, recoverable)
            self.assertEqual(raced_state, (target / STATE_NAME).read_bytes())

    def test_missing_install_preserves_a_same_digest_replacement_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            destination = target / TARGET_NAME
            replacement_inode: int | None = None

            def race(point: str) -> None:
                nonlocal replacement_inode
                if point == "install.before_publish_missing_state":
                    (target / STATE_NAME).write_bytes(b'{"owner":"user"}\n')
                    replacement = root / "same-digest-replacement.toml"
                    replacement.write_bytes(TEMPLATE.read_bytes())
                    replacement_inode = replacement.stat().st_ino
                    os.replace(replacement, destination)

            self.assertNotEqual(0, lifecycle.install(target, hook=race))
            self.assertIsNotNone(replacement_inode)
            recoverable_inodes = []
            if destination.exists():
                recoverable_inodes.append(destination.stat().st_ino)
            recoverable_inodes.extend(
                payload.stat().st_ino
                for payload in target.glob(".ai-workflow-tombstone-*/payload")
            )
            self.assertIn(replacement_inode, recoverable_inodes)

    def test_missing_state_hook_oserror_rolls_back_agent_and_state(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"

            self.assertEqual(
                1, lifecycle.install(target, hook=raise_on_missing_state(OSError))
            )

            self.assertFalse((target / TARGET_NAME).exists())
            self.assertFalse((target / STATE_NAME).exists())

    def test_missing_state_hook_runtimeerror_rolls_back_then_reraises(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"

            with self.assertRaisesRegex(RuntimeError, "injected"):
                lifecycle.install(target, hook=raise_on_missing_state(RuntimeError))

            self.assertFalse((target / TARGET_NAME).exists())
            self.assertFalse((target / STATE_NAME).exists())

    def test_missing_state_publish_oserror_rolls_back(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"

            with raise_from_state_publish(lifecycle, OSError):
                self.assertEqual(1, lifecycle.install(target))

            self.assertFalse((target / TARGET_NAME).exists())
            self.assertFalse((target / STATE_NAME).exists())

    def test_missing_post_link_state_exception_rolls_back_then_reraises(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"

            with self.assertRaisesRegex(RuntimeError, "injected"):
                lifecycle.install(
                    target, hook=raise_after_missing_state_publish(RuntimeError)
                )

            self.assertFalse((target / TARGET_NAME).exists())
            self.assertFalse((target / STATE_NAME).exists())

    def test_known_legacy_state_failure_restores_legacy_without_new_state(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            prepare_known_legacy(target)
            legacy_inode = (target / LEGACY_TARGET_NAME).stat().st_ino

            with raise_from_state_publish(lifecycle, OSError):
                self.assertEqual(1, lifecycle.install(target))

            self.assertEqual(LEGACY_BYTES, (target / LEGACY_TARGET_NAME).read_bytes())
            self.assertEqual(legacy_inode, (target / LEGACY_TARGET_NAME).stat().st_ino)
            self.assertFalse((target / STATE_NAME).exists())

    def test_known_legacy_state_hook_runtimeerror_restores_then_reraises(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            prepare_known_legacy(target)
            legacy_inode = (target / LEGACY_TARGET_NAME).stat().st_ino

            with self.assertRaisesRegex(RuntimeError, "injected"):
                lifecycle.install(
                    target, hook=raise_on_known_legacy_state(RuntimeError)
                )

            self.assertEqual(LEGACY_BYTES, (target / LEGACY_TARGET_NAME).read_bytes())
            self.assertEqual(legacy_inode, (target / LEGACY_TARGET_NAME).stat().st_ino)
            self.assertFalse((target / STATE_NAME).exists())

    def test_known_legacy_state_publish_race_restores_legacy(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            prepare_known_legacy(target)
            legacy_inode = (target / LEGACY_TARGET_NAME).stat().st_ino
            user_state = b'{"owner":"user"}\n'

            def race(point: str) -> None:
                if point == "install.before_publish_legacy_state":
                    (target / STATE_NAME).write_bytes(user_state)

            self.assertEqual(1, lifecycle.install(target, hook=race))
            self.assertEqual(legacy_inode, (target / LEGACY_TARGET_NAME).stat().st_ino)
            self.assertEqual(user_state, (target / STATE_NAME).read_bytes())

    def test_known_legacy_state_failure_preserves_user_agent_and_legacy_tombstone(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            prepare_known_legacy(target)
            legacy_inode = (target / LEGACY_TARGET_NAME).stat().st_ino
            user_bytes = b'name = "user_replacement"\n'

            def replace_then_raise(point: str) -> None:
                if point == "install.before_publish_legacy_state":
                    replacement = root / "user-replacement.toml"
                    replacement.write_bytes(user_bytes)
                    os.replace(replacement, target / LEGACY_TARGET_NAME)
                    raise OSError("injected")

            self.assertEqual(1, lifecycle.install(target, hook=replace_then_raise))
            self.assertEqual(user_bytes, (target / LEGACY_TARGET_NAME).read_bytes())
            self.assertIn(
                legacy_inode,
                [
                    payload.stat().st_ino
                    for payload in target.glob(".ai-workflow-tombstone-*/payload")
                ],
            )
            self.assertFalse((target / STATE_NAME).exists())

    def test_known_legacy_state_failure_preserves_same_digest_replacement_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            prepare_known_legacy(target)
            legacy_inode = (target / LEGACY_TARGET_NAME).stat().st_ino
            replacement_inode: int | None = None

            def replace_then_raise(point: str) -> None:
                nonlocal replacement_inode
                if point == "install.before_publish_legacy_state":
                    replacement = root / "same-digest-replacement.toml"
                    replacement.write_bytes(TEMPLATE.read_bytes())
                    replacement_inode = replacement.stat().st_ino
                    os.replace(replacement, target / LEGACY_TARGET_NAME)
                    raise OSError("injected")

            self.assertEqual(1, lifecycle.install(target, hook=replace_then_raise))
            self.assertEqual(replacement_inode, (target / LEGACY_TARGET_NAME).stat().st_ino)
            self.assertIn(
                legacy_inode,
                [
                    payload.stat().st_ino
                    for payload in target.glob(".ai-workflow-tombstone-*/payload")
                ],
            )
            self.assertFalse((target / STATE_NAME).exists())

    def test_post_link_state_replacement_is_preserved_on_exception(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            replacement_inode: int | None = None

            def replace_then_raise(point: str) -> None:
                nonlocal replacement_inode
                if point == "install.after_publish_missing_state":
                    replacement = root / "replacement-state"
                    replacement.write_bytes((target / STATE_NAME).read_bytes())
                    replacement_inode = replacement.stat().st_ino
                    os.replace(replacement, target / STATE_NAME)
                    raise RuntimeError("injected")

            with self.assertRaisesRegex(RuntimeError, "injected"):
                lifecycle.install(target, hook=replace_then_raise)

            self.assertIsNotNone(replacement_inode)
            self.assertEqual(replacement_inode, (target / STATE_NAME).stat().st_ino)
            self.assertFalse((target / TARGET_NAME).exists())

    def test_known_legacy_post_link_runtimeerror_restores_then_reraises(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            prepare_known_legacy(target)
            legacy_inode = (target / LEGACY_TARGET_NAME).stat().st_ino

            with self.assertRaisesRegex(RuntimeError, "injected"):
                lifecycle.install(
                    target,
                    hook=raise_after_known_legacy_state_publish(RuntimeError),
                )

            self.assertEqual(legacy_inode, (target / LEGACY_TARGET_NAME).stat().st_ino)
            self.assertFalse((target / STATE_NAME).exists())

    def test_staged_cleanup_oserror_does_not_mask_original_runtimeerror(self):
        lifecycle = load_lifecycle_helper()
        original_unlink = lifecycle.os.unlink

        def fail_staged_state_cleanup(name, *arguments, **keywords):
            if str(name).startswith(".ai-workflow-state-"):
                raise OSError("cleanup failed")
            return original_unlink(name, *arguments, **keywords)

        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            with mock.patch.object(
                lifecycle.os, "unlink", side_effect=fail_staged_state_cleanup
            ):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    lifecycle.install(
                        target, hook=raise_on_missing_state(RuntimeError)
                    )

    def test_failed_state_transactions_close_directory_descriptors(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            before = open_fd_count()
            for index in range(16):
                missing = root / "missing" / str(index) / "agents"
                self.assertEqual(
                    1,
                    lifecycle.install(
                        missing, hook=raise_on_missing_state(OSError)
                    ),
                )
                legacy = root / "legacy" / str(index) / "agents"
                prepare_known_legacy(legacy)
                self.assertEqual(
                    1,
                    lifecycle.install(
                        legacy, hook=raise_on_known_legacy_state(OSError)
                    ),
                )
            after = open_fd_count()
            self.assertLessEqual(after, before + 2)

    def test_agent_publish_rejects_and_preserves_a_replaced_staging_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            replacement_inode: int | None = None

            def replace_staging_agent(point: str) -> None:
                nonlocal replacement_inode
                if point == "install.before_publish_missing":
                    staged_agent, = target.glob(".ai-workflow-agent-*")
                    replacement = Path(temporary) / "replacement-agent"
                    replacement.write_bytes(b'name = "user"\n')
                    replacement_inode = replacement.stat().st_ino
                    os.replace(replacement, staged_agent)

            self.assertEqual(1, lifecycle.install(target, hook=replace_staging_agent))
            self.assertIsNotNone(replacement_inode)
            self.assertIn(
                replacement_inode,
                [entry.stat().st_ino for entry in target.iterdir()],
            )
            self.assertFalse((target / TARGET_NAME).exists())
            self.assertFalse((target / STATE_NAME).exists())

    def test_state_publish_rejects_and_preserves_a_replaced_staging_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            replacement_inode: int | None = None

            def replace_staging_state(point: str) -> None:
                nonlocal replacement_inode
                if point == "install.before_publish_missing_state":
                    staged_state, = target.glob(".ai-workflow-state-*")
                    replacement = Path(temporary) / "replacement-state"
                    replacement.write_bytes(b'{"owner":"user"}\n')
                    replacement_inode = replacement.stat().st_ino
                    os.replace(replacement, staged_state)

            self.assertEqual(1, lifecycle.install(target, hook=replace_staging_state))
            self.assertIsNotNone(replacement_inode)
            self.assertIn(
                replacement_inode,
                [entry.stat().st_ino for entry in target.iterdir()],
            )
            self.assertFalse((target / TARGET_NAME).exists())
            self.assertFalse((target / STATE_NAME).exists())

    def test_final_cleanup_preserves_a_replaced_staging_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            replacement_inode: int | None = None

            def replace_staging_agent(point: str) -> None:
                nonlocal replacement_inode
                if point == "install.after_publish_missing_state":
                    staged_agent, = target.glob(".ai-workflow-agent-*")
                    replacement = Path(temporary) / "replacement-agent"
                    replacement.write_bytes(b'name = "user"\n')
                    replacement_inode = replacement.stat().st_ino
                    os.replace(replacement, staged_agent)

            self.assertEqual(0, lifecycle.install(target, hook=replace_staging_agent))
            self.assertIsNotNone(replacement_inode)
            self.assertIn(
                replacement_inode,
                [entry.stat().st_ino for entry in target.iterdir()],
            )

    def test_directory_close_oserror_does_not_mask_transaction_outcome(self):
        lifecycle = load_lifecycle_helper()
        original_close = lifecycle.os.close
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            prepare_known_legacy(target)
            target_status = target.stat()

            def close_then_fail_for_target(descriptor: int) -> None:
                status = os.fstat(descriptor)
                original_close(descriptor)
                if (status.st_dev, status.st_ino) == (
                    target_status.st_dev,
                    target_status.st_ino,
                ):
                    raise OSError("close failed")

            with mock.patch.object(
                lifecycle.os, "close", side_effect=close_then_fail_for_target
            ):
                self.assertEqual(
                    1,
                    lifecycle.install(
                        target, hook=raise_on_known_legacy_state(OSError)
                    ),
                )

    def test_post_link_identity_error_rolls_back_linked_state(self):
        lifecycle = load_lifecycle_helper()
        original_identity = lifecycle._file_identity

        def fail_state_destination_identity(directory: int, name: str):
            if name == STATE_NAME:
                raise OSError("identity failed")
            return original_identity(directory, name)

        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            with mock.patch.object(
                lifecycle,
                "_file_identity",
                side_effect=fail_state_destination_identity,
            ):
                self.assertEqual(1, lifecycle.install(target))

            self.assertFalse((target / TARGET_NAME).exists())
            self.assertFalse((target / STATE_NAME).exists())

    def test_known_legacy_agent_staging_race_restores_legacy_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            prepare_known_legacy(target)
            legacy_inode = (target / LEGACY_TARGET_NAME).stat().st_ino
            replacement_inode: int | None = None

            def replace_staging_agent(point: str) -> None:
                nonlocal replacement_inode
                if point == "install.before_retire_legacy":
                    staged_agent, = target.glob(".ai-workflow-agent-*")
                    replacement = root / "replacement-agent"
                    replacement.write_bytes(b'name = "user"\n')
                    replacement_inode = replacement.stat().st_ino
                    os.replace(replacement, staged_agent)

            self.assertEqual(1, lifecycle.install(target, hook=replace_staging_agent))
            self.assertEqual(legacy_inode, (target / LEGACY_TARGET_NAME).stat().st_ino)
            self.assertIsNotNone(replacement_inode)
            self.assertIn(
                replacement_inode,
                [entry.stat().st_ino for entry in target.iterdir()],
            )

    def test_missing_state_failure_preserves_a_different_byte_agent_replacement(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            destination = target / TARGET_NAME
            user_bytes = b'name = "user_replacement"\n'

            def replace_then_raise(point: str) -> None:
                if point == "install.after_publish_missing_state":
                    replacement = root / "different-byte-replacement.toml"
                    replacement.write_bytes(user_bytes)
                    os.replace(replacement, destination)
                    raise OSError("injected")

            self.assertEqual(1, lifecycle.install(target, hook=replace_then_raise))
            self.assertEqual(user_bytes, destination.read_bytes())
            self.assertFalse((target / STATE_NAME).exists())

    def test_missing_state_failure_preserves_same_digest_replacement_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            destination = target / TARGET_NAME
            replacement_inode: int | None = None

            def replace_then_raise(point: str) -> None:
                nonlocal replacement_inode
                if point == "install.after_publish_missing_state":
                    replacement = root / "same-digest-replacement.toml"
                    replacement.write_bytes(TEMPLATE.read_bytes())
                    replacement_inode = replacement.stat().st_ino
                    os.replace(replacement, destination)
                    raise OSError("injected")

            self.assertEqual(1, lifecycle.install(target, hook=replace_then_raise))
            self.assertIsNotNone(replacement_inode)
            self.assertEqual(replacement_inode, destination.stat().st_ino)
            self.assertFalse((target / STATE_NAME).exists())

    def test_missing_check_closes_its_directory_descriptor(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "missing" / "agents"
            before = open_fd_count()
            for _ in range(16):
                self.assertNotEqual(0, lifecycle.install(target, check=True))
            after = open_fd_count()
            self.assertLessEqual(after, before + 2)

    def test_retained_tombstone_closes_its_descriptor(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            directory = lifecycle._open_target_directory(target, create=False)
            self.assertIsNotNone(directory)
            original_discard = lifecycle._discard_verified_tombstone
            lifecycle._discard_verified_tombstone = lambda *_: False
            try:
                before = open_fd_count()
                for index in range(16):
                    name = f"retained-{index}"
                    content = f"retained {index}".encode("utf-8")
                    (target / name).write_bytes(content)
                    self.assertFalse(
                        lifecycle._retire_and_discard(
                            directory, name, hashlib.sha256(content).hexdigest()
                        )
                    )
                after = open_fd_count()
            finally:
                lifecycle._discard_verified_tombstone = original_discard
                os.close(directory)
            self.assertLessEqual(after, before + 2)

    def test_final_discard_preserves_same_digest_replacement_payload(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            target.mkdir()
            owned = target / "owned"
            content = b"same digest payload\n"
            owned.write_bytes(content)
            owned_status = owned.stat()
            expected_identity = (
                owned_status.st_dev,
                owned_status.st_ino,
                hashlib.sha256(content).hexdigest(),
            )
            replacement_inode: int | None = None
            directory = lifecycle._open_target_directory(target, create=False)
            self.assertIsNotNone(directory)
            original_discard = lifecycle._discard_verified_tombstone

            def replace_payload_then_discard(*arguments):
                nonlocal replacement_inode
                tombstone = arguments[2]
                replacement = root / "replacement-payload"
                replacement.write_bytes(content)
                replacement_inode = replacement.stat().st_ino
                os.rename(replacement, "payload", dst_dir_fd=tombstone)
                return original_discard(*arguments)

            try:
                with mock.patch.object(
                    lifecycle,
                    "_discard_verified_tombstone",
                    side_effect=replace_payload_then_discard,
                ):
                    self.assertFalse(
                        lifecycle._retire_and_discard(
                            directory,
                            "owned",
                            hashlib.sha256(content).hexdigest(),
                            expected_identity=expected_identity,
                        )
                    )
            finally:
                os.close(directory)

            self.assertIsNotNone(replacement_inode)
            recoverable_inodes = []
            if owned.exists():
                recoverable_inodes.append(owned.stat().st_ino)
            recoverable_inodes.extend(
                payload.stat().st_ino
                for payload in target.glob(".ai-workflow-tombstone-*/payload")
            )
            self.assertIn(replacement_inode, recoverable_inodes)

    def test_known_legacy_install_closes_final_discard_failure_descriptors(self):
        lifecycle = load_lifecycle_helper()
        original_discard = lifecycle._discard_verified_tombstone
        lifecycle._discard_verified_tombstone = lambda *_: False
        try:
            with temporary_directory() as temporary:
                root = Path(temporary)
                before = open_fd_count()
                for index in range(16):
                    target = root / str(index) / "agents"
                    target.mkdir(parents=True)
                    (target / LEGACY_TARGET_NAME).write_bytes(KNOWN_LEGACY_TEMPLATE)
                    self.assertNotEqual(0, lifecycle.install(target))
                after = open_fd_count()
        finally:
            lifecycle._discard_verified_tombstone = original_discard
        self.assertLessEqual(after, before + 2)

    def test_uninstall_closes_final_discard_failure_descriptors(self):
        lifecycle = load_lifecycle_helper()
        original_discard = lifecycle._discard_verified_tombstone

        def fail_only_final_agent_discard(*arguments):
            if arguments[-1][2] == CANONICAL_TEMPLATE_SHA256:
                return False
            return original_discard(*arguments)

        lifecycle._discard_verified_tombstone = fail_only_final_agent_discard
        try:
            with temporary_directory() as temporary:
                root = Path(temporary)
                before = open_fd_count()
                for index in range(16):
                    target = root / str(index) / "agents"
                    target.mkdir(parents=True)
                    (target / TARGET_NAME).write_bytes(TEMPLATE.read_bytes())
                    write_owned_state(target)
                    self.assertNotEqual(0, lifecycle.uninstall(target))
                after = open_fd_count()
        finally:
            lifecycle._discard_verified_tombstone = original_discard
        self.assertLessEqual(after, before + 2)

    def test_temporary_directory_uses_the_physical_system_temp_root(self):
        physical_system_temp = Path(tempfile.gettempdir()).resolve()
        with tempfile.TemporaryDirectory(dir=physical_system_temp) as parent:
            alternate_temp = Path(parent) / "linux-tmp"
            alternate_temp.mkdir()
            with mock.patch.object(tempfile, "gettempdir", return_value=str(alternate_temp)):
                with temporary_directory() as created:
                    created_path = Path(created)
                    self.assertEqual(alternate_temp, created_path.parent)
                    self.assertEqual(created_path, created_path.resolve())

    def test_known_legacy_install_preserves_a_raced_user_replacement(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            target.mkdir()
            destination = target / LEGACY_TARGET_NAME
            destination.write_bytes(KNOWN_LEGACY_TEMPLATE)
            user_bytes = b'name = "replaced_after_preflight"\n'

            def race(point: str) -> None:
                if point == "install.before_retire_legacy":
                    replacement = root / "user-replacement.toml"
                    replacement.write_bytes(user_bytes)
                    os.replace(replacement, destination)

            self.assertNotEqual(0, lifecycle.install(target, hook=race))
            self.assertEqual(user_bytes, destination.read_bytes())
            self.assertFalse((target / STATE_NAME).exists())

    def test_known_legacy_install_preserves_same_digest_hook_replacement_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            prepare_known_legacy(target)
            replacement_inode: int | None = None

            def race(point: str) -> None:
                nonlocal replacement_inode
                if point == "install.before_retire_legacy":
                    replacement = root / "same-digest-legacy"
                    replacement.write_bytes(KNOWN_LEGACY_TEMPLATE)
                    replacement_inode = replacement.stat().st_ino
                    os.replace(replacement, target / LEGACY_TARGET_NAME)

            self.assertEqual(1, lifecycle.install(target, hook=race))
            self.assertIsNotNone(replacement_inode)
            recoverable_inodes = [
                entry.stat().st_ino
                for entry in target.iterdir()
                if entry.is_file()
            ]
            recoverable_inodes.extend(
                payload.stat().st_ino
                for payload in target.glob(".ai-workflow-tombstone-*/payload")
            )
            self.assertIn(replacement_inode, recoverable_inodes)

    def test_known_legacy_install_binds_its_classification_snapshot_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            prepare_known_legacy(target)
            replacement_inode: int | None = None

            def replace_after_snapshot() -> None:
                nonlocal replacement_inode
                replacement = root / "same-digest-classification-race"
                replacement.write_bytes(KNOWN_LEGACY_TEMPLATE)
                replacement_inode = replacement.stat().st_ino
                os.replace(replacement, target / LEGACY_TARGET_NAME)

            with patch_after_first_snapshot(
                lifecycle, LEGACY_TARGET_NAME, replace_after_snapshot
            ):
                self.assertEqual(1, lifecycle.install(target))

            self.assertIsNotNone(replacement_inode)
            recoverable_inodes = [
                entry.stat().st_ino
                for entry in target.iterdir()
                if entry.is_file()
            ]
            recoverable_inodes.extend(
                payload.stat().st_ino
                for payload in target.glob(".ai-workflow-tombstone-*/payload")
            )
            self.assertIn(replacement_inode, recoverable_inodes)

    def test_uninstall_preserves_a_raced_user_replacement(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            target.mkdir()
            destination = target / TARGET_NAME
            destination.write_bytes(TEMPLATE.read_bytes())
            write_owned_state(target)
            user_bytes = b'name = "raced_uninstall_user"\n'

            def race(point: str) -> None:
                if point == "uninstall.before_retire_current":
                    replacement = root / "user-replacement.toml"
                    replacement.write_bytes(user_bytes)
                    os.replace(replacement, destination)

            self.assertNotEqual(0, lifecycle.uninstall(target, hook=race))
            self.assertEqual(user_bytes, destination.read_bytes())
            self.assertTrue((target / STATE_NAME).exists())

    def test_uninstall_preserves_same_digest_target_hook_replacement_inode(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            target.mkdir()
            destination = target / TARGET_NAME
            destination.write_bytes(TEMPLATE.read_bytes())
            write_owned_state(target)
            replacement_inode: int | None = None

            def race(point: str) -> None:
                nonlocal replacement_inode
                if point == "uninstall.before_retire_current":
                    replacement = root / "same-digest-current"
                    replacement.write_bytes(TEMPLATE.read_bytes())
                    replacement_inode = replacement.stat().st_ino
                    os.replace(replacement, destination)

            self.assertEqual(1, lifecycle.uninstall(target, hook=race))
            self.assertIsNotNone(replacement_inode)
            recoverable_inodes = [
                entry.stat().st_ino
                for entry in target.iterdir()
                if entry.is_file()
            ]
            recoverable_inodes.extend(
                payload.stat().st_ino
                for payload in target.glob(".ai-workflow-tombstone-*/payload")
            )
            self.assertIn(replacement_inode, recoverable_inodes)

    def test_uninstall_preserves_same_digest_state_or_backup_replacement_inode(self):
        for raced_name in (STATE_NAME, BACKUP_NAME):
            with self.subTest(raced_name=raced_name):
                lifecycle = load_lifecycle_helper()
                with temporary_directory() as temporary:
                    root = Path(temporary)
                    target = root / "agents"
                    target.mkdir()
                    (target / TARGET_NAME).write_bytes(TEMPLATE.read_bytes())
                    backup_bytes = b'name = "backup"\n'
                    backup_sha = None
                    if raced_name == BACKUP_NAME:
                        (target / BACKUP_NAME).write_bytes(backup_bytes)
                        backup_sha = sha256(target / BACKUP_NAME)
                    write_owned_state(target, backup_sha)
                    raced_bytes = (target / raced_name).read_bytes()
                    replacement_inode: int | None = None
                    original_retire = lifecycle._retire_and_discard

                    def replace_then_retire(
                        directory, name, expected_sha, **keywords
                    ):
                        nonlocal replacement_inode
                        if name == raced_name and replacement_inode is None:
                            replacement = root / f"replacement-{raced_name}"
                            replacement.write_bytes(raced_bytes)
                            replacement_inode = replacement.stat().st_ino
                            os.replace(replacement, target / raced_name)
                        return original_retire(
                            directory, name, expected_sha, **keywords
                        )

                    with mock.patch.object(
                        lifecycle,
                        "_retire_and_discard",
                        side_effect=replace_then_retire,
                    ):
                        self.assertEqual(1, lifecycle.uninstall(target))

                    self.assertIsNotNone(replacement_inode)
                    recoverable_inodes = [
                        entry.stat().st_ino
                        for entry in target.iterdir()
                        if entry.is_file()
                    ]
                    recoverable_inodes.extend(
                        payload.stat().st_ino
                        for payload in target.glob(
                            ".ai-workflow-tombstone-*/payload"
                        )
                    )
                    self.assertIn(replacement_inode, recoverable_inodes)

    def test_uninstall_binds_each_initial_file_snapshot_inode(self):
        for raced_name in (TARGET_NAME, STATE_NAME, BACKUP_NAME):
            with self.subTest(raced_name=raced_name):
                lifecycle = load_lifecycle_helper()
                with temporary_directory() as temporary:
                    root = Path(temporary)
                    target = root / "agents"
                    target.mkdir()
                    (target / TARGET_NAME).write_bytes(TEMPLATE.read_bytes())
                    backup_sha = None
                    if raced_name == BACKUP_NAME:
                        (target / BACKUP_NAME).write_bytes(b'name = "backup"\n')
                        backup_sha = sha256(target / BACKUP_NAME)
                    write_owned_state(target, backup_sha)
                    raced_bytes = (target / raced_name).read_bytes()
                    replacement_inode: int | None = None

                    def replace_after_snapshot() -> None:
                        nonlocal replacement_inode
                        replacement = root / f"snapshot-race-{raced_name}"
                        replacement.write_bytes(raced_bytes)
                        replacement_inode = replacement.stat().st_ino
                        os.replace(replacement, target / raced_name)

                    with patch_after_first_snapshot(
                        lifecycle, raced_name, replace_after_snapshot
                    ):
                        self.assertEqual(1, lifecycle.uninstall(target))

                    self.assertIsNotNone(replacement_inode)
                    recoverable_inodes = [
                        entry.stat().st_ino
                        for entry in target.iterdir()
                        if entry.is_file()
                    ]
                    recoverable_inodes.extend(
                        payload.stat().st_ino
                        for payload in target.glob(
                            ".ai-workflow-tombstone-*/payload"
                        )
                    )
                    self.assertIn(replacement_inode, recoverable_inodes)

    def test_owned_backup_is_restored_only_when_current_and_backup_hashes_match(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            destination = target / TARGET_NAME
            backup = target / BACKUP_NAME
            backup_bytes = b'name = "known_legacy_backup"\n'
            destination.write_bytes(TEMPLATE.read_bytes())
            backup.write_bytes(backup_bytes)
            write_owned_state(target, sha256(backup))
            self.assertEqual(0, lifecycle.uninstall(target))
            self.assertEqual(backup_bytes, destination.read_bytes())
            self.assertFalse(backup.exists())
            self.assertFalse((target / STATE_NAME).exists())

    def test_tampered_owned_backup_or_current_is_preserved(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            for name, tamper in (("backup", "backup"), ("current", "current")):
                with self.subTest(name=name):
                    target = root / name / "agents"
                    target.mkdir(parents=True)
                    destination = target / TARGET_NAME
                    backup = target / BACKUP_NAME
                    destination.write_bytes(TEMPLATE.read_bytes())
                    backup.write_bytes(b'name = "known_legacy_backup"\n')
                    write_owned_state(target, sha256(backup))
                    if tamper == "backup":
                        backup.write_bytes(b'name = "tampered_backup"\n')
                    else:
                        destination.write_bytes(b'name = "tampered_current"\n')
                    before = filesystem_snapshot(target)
                    self.assertNotEqual(0, lifecycle.uninstall(target))
                    self.assertEqual(before, filesystem_snapshot(target))

    def test_backup_restore_does_not_clobber_a_raced_user_replacement(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            target.mkdir()
            destination = target / TARGET_NAME
            backup = target / BACKUP_NAME
            destination.write_bytes(TEMPLATE.read_bytes())
            backup.write_bytes(b'name = "known_legacy_backup"\n')
            write_owned_state(target, sha256(backup))
            user_bytes = b'name = "raced_restore_user"\n'

            def race(point: str) -> None:
                if point == "uninstall.before_retire_current":
                    replacement = root / "user-replacement.toml"
                    replacement.write_bytes(user_bytes)
                    os.replace(replacement, destination)

            self.assertNotEqual(0, lifecycle.uninstall(target, hook=race))
            self.assertEqual(user_bytes, destination.read_bytes())
            self.assertTrue(backup.exists())
            self.assertTrue((target / STATE_NAME).exists())

    def test_uninstall_requires_owned_unchanged_state_and_never_recurses(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            unrelated = target / "keep.toml"
            unrelated.write_text('name = "keep"\n')
            self.assertEqual(0, self.install(target).returncode)
            destination = target / TARGET_NAME
            state = json.loads((target / STATE_NAME).read_text())
            self.assertEqual({"plugin_version", "target_filename", "installed_sha256", "installed_at_utc", "backup_sha256"}, set(state))
            self.assertEqual(TARGET_NAME, state["target_filename"])
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
