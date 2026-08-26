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


class FullVerificationEntrypointTest(unittest.TestCase):
    def test_untracked_files_are_included_in_whitespace_checks(self):
        script = (ROOT / "scripts" / "verify_all.sh").read_text(encoding="utf-8")
        self.assertIn("git ls-files --others --exclude-standard -z", script)
        self.assertIn("git diff --no-index --check", script)


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


def canonical_template_bytes() -> bytes:
    """Return the immutable legacy migration payload, not an active Agent file."""

    return load_lifecycle_helper().CANONICAL_TEMPLATE_BYTES


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
    def test_native_luna_is_default_and_custom_templates_are_not_active(self):
        verifier = (PLUGIN / "scripts" / "verify.sh").read_text(encoding="utf-8")
        skill = (PLUGIN / "skills" / "orchestration" / "SKILL.md").read_text(
            encoding="utf-8"
        ).casefold()
        self.assertFalse((ROOT / ".codex" / "agents" / "luna-max.toml").exists())
        self.assertFalse((PLUGIN / "agents" / "luna-max.toml").exists())
        self.assertIn("native_subagent", skill)
        self.assertIn("gpt-5.6-luna", skill)
        self.assertIn("reasoning_effort=max", skill)
        self.assertNotIn("cmp -s \"$repository_root/.codex/agents/luna-max.toml\"", verifier)
        self.assertNotIn("require the exact custom agent name", skill)

    def test_plugin_manifest_and_marketplace_are_versioned(self):
        manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
        marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
        self.assertEqual("ai-workflow", manifest["name"])
        self.assertEqual("0.4.0", manifest["version"])
        self.assertEqual("./skills/", manifest["skills"])
        self.assertEqual("ai-workflow", marketplace["name"])
        self.assertEqual(
            "./plugins/ai-workflow", marketplace["plugins"][0]["source"]["path"]
        )

    def test_project_and_release_agent_templates_are_absent(self):
        self.assertFalse((ROOT / ".codex" / "agents" / "luna-max.toml").exists())
        self.assertFalse((PLUGIN / "agents" / "luna-max.toml").exists())
        self.assertFalse((ROOT / ".codex" / "agents" / LEGACY_TARGET_NAME).exists())
        self.assertFalse((PLUGIN / "agents" / LEGACY_TARGET_NAME).exists())

    def test_legacy_migration_payload_matches_the_historical_contract(self):
        lifecycle = LIFECYCLE_HELPER.read_text(encoding="utf-8")
        self.assertNotIn("CANONICAL_TEMPLATE_BYTES", lifecycle)
        self.assertNotIn('name = "luna_max"', lifecycle)

    def test_release_runtime_and_schema_copies_are_byte_exact(self):
        configs = (
            "ai_workflow.toml",
            "ai_workflow_task.schema.json",
            "ai_workflow_result.schema.json",
            "ai_workflow_route_request.schema.json",
            "ai_workflow_route_decision.schema.json",
            "ai_workflow_route_advice.schema.json",
            "ai_workflow_plan.schema.json",
            "ai_workflow_runtime_evidence.schema.json",
            "ai_workflow_cost_evidence.schema.json",
            "ai_workflow_scheduler.schema.json",
        )
        runtimes = (
            "ai_workflow.py",
            "ai_workflow_artifacts.py",
            "ai_workflow_routing.py",
            "ai_workflow_planning.py",
            "ai_workflow_runtime.py",
            "ai_workflow_costs.py",
            "ai_workflow_repairs.py",
            "ai_workflow_team_call.py",
            "ai_workflow_scheduler.py",
        )
        for name in configs:
            self.assertEqual((ROOT / "config" / name).read_bytes(), (PLUGIN / "config" / name).read_bytes())
        for name in runtimes:
            self.assertEqual((ROOT / "scripts" / name).read_bytes(), (PLUGIN / "runtime" / name).read_bytes())
        task_schema = json.loads((PLUGIN / "config" / "ai_workflow_task.schema.json").read_text())
        self.assertIn("paired_case_id", task_schema["properties"])

    def test_team_call_runtime_copy_and_published_contract_are_exact(self):
        """A release must publish the bounded Team Call contract unchanged."""

        self.assertEqual(
            (ROOT / "scripts" / "ai_workflow_team_call.py").read_bytes(),
            (PLUGIN / "runtime" / "ai_workflow_team_call.py").read_bytes(),
        )
        published = "\n".join(
            (
                (ROOT / "README.md").read_text(encoding="utf-8").casefold(),
                (PLUGIN / "skills" / "orchestration" / "SKILL.md")
                .read_text(encoding="utf-8")
                .casefold(),
            )
        )
        for grammar in (
            "team call <objective>",
            "team call: <objective>",
            "team call：<objective>",
        ):
            self.assertIn(grammar, published, grammar)
        for disposition in ("direct_l0", "direct_l1", "plan_required", "blocked"):
            self.assertIn(disposition, published, disposition)
        for required_boundary in (
            "single active worker",
            "l0 controller/no model",
            "l1 luna read-only",
            "plan fallback",
            "l0/l1/l2",
            "human owner gates",
            "auto-merge",
            "auto-push",
        ):
            self.assertIn(required_boundary, published, required_boundary)
        self.assertRegex(
            published,
            r"luna must never review, approve, or perform\s+final acceptance",
        )
        self.assertIn("does not promise parallel agents", published)

    def test_plugin_verifier_rejects_tampered_team_call_runtime_copy(self):
        """Changing only the copied Team Call module invalidates a release."""

        with tempfile.TemporaryDirectory() as temporary:
            release_root = Path(temporary) / "release"
            shutil.copytree(ROOT / ".codex", release_root / ".codex")
            shutil.copytree(ROOT / "config", release_root / "config")
            shutil.copytree(ROOT / "scripts", release_root / "scripts")
            shutil.copytree(PLUGIN, release_root / "plugins" / "ai-workflow")
            verifier = release_root / "plugins" / "ai-workflow" / "scripts" / "verify.sh"
            clean = subprocess.run(
                ["sh", str(verifier)],
                cwd=release_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, clean.returncode, clean.stderr)
            tampered = (
                release_root
                / "plugins"
                / "ai-workflow"
                / "runtime"
                / "ai_workflow_team_call.py"
            )
            tampered.write_text(
                tampered.read_text(encoding="utf-8") + "\n# tampered\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["sh", str(verifier)],
                cwd=release_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("runtime copy differs", result.stderr)

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

        self.assertIn("luna max", published)
        self.assertIn("native_subagent", published)
        self.assertIn("gpt-5.6-luna", published)
        self.assertNotIn("luna_worker", published)
        self.assertNotIn("luna-max.toml", published)

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
            "every task needs an independent terra xhigh adversarial review",
            "第二次 terra xhigh 失败后的冻结梯级",
        ):
            self.assertNotIn(stale_phrase, published, stale_phrase)

        for lifecycle_phrase in (
            "section_self_check_only",
            "intermediate engineering sections",
            "different sol-medium fixer",
            "different sol-medium recheck",
            "owner-authorized sol-xhigh terminal repair",
            "dual-key",
            "compact_prompts",
            "armed field projection",
            "do not participate in compact",
        ):
            self.assertIn(lifecycle_phrase, published, lifecycle_phrase)
        self.assertRegex(
            published,
            r"sol[- ]medium\s+final\s+acceptance",
            "Sol medium final acceptance",
        )
        self.assertRegex(
            published,
            r"without\s+task-level\s+review|无\s*task-level\s*review",
            "terminal repair has no task-level review",
        )

        root_policy = tomllib.loads((ROOT / "config" / "ai_workflow.toml").read_text())
        plugin_policy = tomllib.loads(
            (PLUGIN / "config" / "ai_workflow.toml").read_text()
        )
        for config in (root_policy, plugin_policy):
            self.assertEqual(1, config["policy"]["max_implementation_reworks"])
            self.assertEqual(
                {
                    "fixer_role": "sol_medium_reviewer",
                    "fixer_permission_profile": "assignment-scoped-write",
                    "fixer_distinct_from_acceptor": True,
                    "recheck_role": "sol_medium_reviewer",
                    "recheck_distinct_from_fixer": True,
                    "terminal_escalation_role": "sol_xhigh",
                    "terminal_review_required": False,
                },
                config["final_acceptance_rework"],
            )
            self.assertNotIn("repair", config)
            for name, role in config["roles"].items():
                if (
                    role["model"] == "gpt-5.6-sol"
                    and role["reasoning_effort"] in {"medium", "xhigh"}
                ):
                    with self.subTest(role=name):
                        self.assertIn("Do not over-design", role["instructions"])
                        self.assertIn("smallest change", role["instructions"])

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

    def test_published_default_defers_adversarial_review_until_final_acceptance(self):
        """The default has section self-checks and one bounded Sol rework ladder."""

        readme = (ROOT / "README.md").read_text(encoding="utf-8").casefold()
        skill = (PLUGIN / "skills" / "orchestration" / "SKILL.md").read_text(
            encoding="utf-8"
        ).casefold()
        published = "\n".join((readme, skill))
        for phrase in (
            "intermediate engineering sections",
            "section_self_check_only",
            "different sol-medium fixer",
            "different sol-medium recheck",
            "owner-authorized sol-xhigh terminal repair",
        ):
            self.assertIn(phrase, published, phrase)
        self.assertRegex(published, r"sol[- ]medium\s+final\s+acceptance")
        self.assertNotIn("every task needs an independent terra xhigh adversarial review", published)
        self.assertNotIn("第二次 terra xhigh 失败后的冻结梯级", published)

    def test_orchestration_metadata_has_no_legacy_agent_identifier(self):
        """Skill metadata must never publish the legacy custom-Agent name."""

        metadata = (PLUGIN / "skills" / "orchestration" / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("luna_worker", metadata.casefold())

    def test_sol_xhigh_terminal_repair_is_narrow_exception_to_construction_ban(self):
        """Published docs keep terminal repair as a narrow, owner-gated exception."""

        readme = (ROOT / "README.md").read_text(encoding="utf-8").casefold()
        skill = (PLUGIN / "skills" / "orchestration" / "SKILL.md").read_text(
            encoding="utf-8"
        ).casefold()
        published = "\n".join((readme, skill))
        self.assertIn("terminal repair 是一次性例外", readme)
        self.assertIn("不产生普通常驻施工权限", readme)
        self.assertRegex(skill, r"never starts\s+automatically")
        self.assertIn("without task-level review", skill)
        self.assertRegex(
            skill,
            re.compile(
                r"sol xhigh.*?terminal escalation.*?never starts\s+automatically",
                re.DOTALL,
            ),
        )
        self.assertNotIn("section 7.4", published)

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

    def test_legacy_migration_payload_digest_is_pinned(self):
        self.assertFalse(hasattr(load_lifecycle_helper(), "CANONICAL_TEMPLATE_BYTES"))

    def test_cleanup_only_never_creates_files_for_an_empty_user_directory(self):
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            before = filesystem_snapshot(target)
            result = subprocess.run(
                ["sh", str(INSTALL), "--target-dir", str(target)],
                cwd=ROOT,
                env={**os.environ, "PYTHON_BIN": str(PYTHON311)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(before, filesystem_snapshot(target))

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


class CleanupOnlyLifecycleTest(unittest.TestCase):
    def test_verified_canonical_cleanup_removes_owned_files(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            payload = b'historical canonical payload\n'
            (target / TARGET_NAME).write_bytes(payload)
            write_owned_state(target)
            state = json.loads((target / STATE_NAME).read_text())
            state["installed_sha256"] = hashlib.sha256(payload).hexdigest()
            (target / STATE_NAME).write_text(json.dumps(state) + "\n")
            with mock.patch.object(
                lifecycle,
                "CANONICAL_RELEASE_SHA256",
                hashlib.sha256(payload).hexdigest(),
            ):
                self.assertEqual(0, lifecycle.cleanup(target))
            self.assertFalse((target / TARGET_NAME).exists())
            self.assertFalse((target / STATE_NAME).exists())
            self.assertFalse((target / BACKUP_NAME).exists())
            self.assertTrue(
                any(path.name.startswith(".ai-workflow-cleanup-") for path in target.iterdir())
            )

    def test_verified_legacy_cleanup_removes_known_historical_file(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            prepare_known_legacy(target)
            self.assertEqual(0, lifecycle.cleanup(target))
            self.assertFalse((target / LEGACY_TARGET_NAME).exists())

    def test_verified_canonical_cleanup_restores_user_backup(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            target.mkdir()
            payload = b"historical canonical payload\n"
            backup = b"original user backup\n"
            (target / TARGET_NAME).write_bytes(payload)
            (target / BACKUP_NAME).write_bytes(backup)
            write_owned_state(target, hashlib.sha256(backup).hexdigest())
            state = json.loads((target / STATE_NAME).read_text())
            state["installed_sha256"] = hashlib.sha256(payload).hexdigest()
            (target / STATE_NAME).write_text(json.dumps(state) + "\n")
            with mock.patch.object(
                lifecycle,
                "CANONICAL_RELEASE_SHA256",
                hashlib.sha256(payload).hexdigest(),
            ):
                self.assertEqual(0, lifecycle.cleanup(target))
            self.assertEqual(backup, (target / TARGET_NAME).read_bytes())
            self.assertFalse((target / STATE_NAME).exists())
            self.assertFalse((target / BACKUP_NAME).exists())

    def test_tampered_mixed_and_symlink_inputs_fail_closed(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            for case in ("tampered", "mixed", "symlink"):
                target = root / case
                target.mkdir()
                if case == "tampered":
                    (target / LEGACY_TARGET_NAME).write_bytes(b"user owned\n")
                elif case == "mixed":
                    (target / LEGACY_TARGET_NAME).write_bytes(KNOWN_LEGACY_TEMPLATE)
                    (target / TARGET_NAME).write_bytes(b"canonical\n")
                else:
                    protected = root / "protected"
                    protected.write_bytes(b"protected\n")
                    (target / LEGACY_TARGET_NAME).symlink_to(protected)
                before = filesystem_snapshot(root)
                with self.subTest(case=case):
                    self.assertEqual(1, lifecycle.cleanup(target))
                    self.assertEqual(before, filesystem_snapshot(root))

    def test_identity_change_before_cleanup_fails_closed(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            target = Path(temporary) / "agents"
            prepare_known_legacy(target)
            original = lifecycle._identity
            calls = 0

            def changed(directory, name):
                nonlocal calls
                value = original(directory, name)
                if name == LEGACY_TARGET_NAME and value is not None:
                    calls += 1
                    if calls > 1:
                        return value[0], value[1] + 1, value[2]
                return value

            with mock.patch.object(lifecycle, "_identity", side_effect=changed):
                self.assertEqual(1, lifecycle.cleanup(target))
            self.assertTrue((target / LEGACY_TARGET_NAME).exists())

    def test_unmanaged_cleanup_preserves_replacement_racing_the_retirement(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            prepare_known_legacy(target)
            replacement = b"user replacement during unlink\n"
            original_rename = lifecycle.os.rename
            injected = False

            def race_rename(source, destination, *arguments, **keywords):
                nonlocal injected
                if source == LEGACY_TARGET_NAME and not injected:
                    injected = True
                    staged = root / "replacement"
                    staged.write_bytes(replacement)
                    os.replace(staged, target / LEGACY_TARGET_NAME)
                return original_rename(source, destination, *arguments, **keywords)

            with mock.patch.object(lifecycle.os, "rename", side_effect=race_rename):
                self.assertEqual(1, lifecycle.cleanup(target))
            self.assertEqual(replacement, (target / LEGACY_TARGET_NAME).read_bytes())

    def test_backup_restore_preserves_replacement_racing_the_rename(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            target.mkdir()
            payload = b"historical canonical payload\n"
            backup = b"original user backup\n"
            replacement = b"replacement backup during restore\n"
            (target / TARGET_NAME).write_bytes(payload)
            (target / BACKUP_NAME).write_bytes(backup)
            write_owned_state(target, hashlib.sha256(backup).hexdigest())
            state = json.loads((target / STATE_NAME).read_text())
            state["installed_sha256"] = hashlib.sha256(payload).hexdigest()
            (target / STATE_NAME).write_text(json.dumps(state) + "\n")
            original_rename = lifecycle.os.rename
            injected = False

            def race_rename(source, destination, *arguments, **keywords):
                nonlocal injected
                if source == BACKUP_NAME and not injected:
                    injected = True
                    staged = root / "replacement"
                    staged.write_bytes(replacement)
                    os.replace(staged, target / BACKUP_NAME)
                return original_rename(source, destination, *arguments, **keywords)

            with (
                mock.patch.object(
                    lifecycle,
                    "CANONICAL_RELEASE_SHA256",
                    hashlib.sha256(payload).hexdigest(),
                ),
                mock.patch.object(lifecycle.os, "rename", side_effect=race_rename),
            ):
                self.assertEqual(1, lifecycle.cleanup(target))
            self.assertEqual(replacement, (target / BACKUP_NAME).read_bytes())
            self.assertEqual(payload, (target / TARGET_NAME).read_bytes())

    def test_cleanup_preserves_state_replacement_racing_retirement(self):
        lifecycle = load_lifecycle_helper()
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "agents"
            target.mkdir()
            payload = b"historical canonical payload\n"
            replacement = b'{"user":"replacement state"}\n'
            (target / TARGET_NAME).write_bytes(payload)
            write_owned_state(target)
            state = json.loads((target / STATE_NAME).read_text())
            state["installed_sha256"] = hashlib.sha256(payload).hexdigest()
            (target / STATE_NAME).write_text(json.dumps(state) + "\n")
            original_rename = lifecycle.os.rename
            injected = False

            def race_rename(source, destination, *arguments, **keywords):
                nonlocal injected
                if source == STATE_NAME and not injected:
                    injected = True
                    staged = root / "replacement"
                    staged.write_bytes(replacement)
                    os.replace(staged, target / STATE_NAME)
                return original_rename(source, destination, *arguments, **keywords)

            with (
                mock.patch.object(
                    lifecycle,
                    "CANONICAL_RELEASE_SHA256",
                    hashlib.sha256(payload).hexdigest(),
                ),
                mock.patch.object(lifecycle.os, "rename", side_effect=race_rename),
            ):
                self.assertEqual(1, lifecycle.cleanup(target))
            self.assertEqual(replacement, (target / STATE_NAME).read_bytes())
            self.assertEqual(payload, (target / TARGET_NAME).read_bytes())

    def test_final_discard_never_unlinks_target_state_or_backup_replacement(self):
        lifecycle = load_lifecycle_helper()
        for selected in (TARGET_NAME, STATE_NAME, BACKUP_NAME):
            with self.subTest(selected=selected), temporary_directory() as temporary:
                root = Path(temporary)
                target = root / "agents"
                target.mkdir()
                payload = b"historical canonical payload\n"
                backup = b"original user backup\n"
                replacement = f"replacement for {selected}\n".encode()
                (target / TARGET_NAME).write_bytes(payload)
                (target / BACKUP_NAME).write_bytes(backup)
                write_owned_state(target, hashlib.sha256(backup).hexdigest())
                state = json.loads((target / STATE_NAME).read_text())
                state["installed_sha256"] = hashlib.sha256(payload).hexdigest()
                (target / STATE_NAME).write_text(json.dumps(state) + "\n")
                selected_digest = hashlib.sha256(
                    (target / selected).read_bytes()
                ).hexdigest()
                original_rename = lifecycle.os.rename
                injected = False

                def race_rename(source, destination, *arguments, **keywords):
                    nonlocal injected
                    directory = keywords.get("src_dir_fd")
                    if source == "payload" and directory is not None and not injected:
                        identity = lifecycle._identity(directory, "payload")
                        if identity is not None and identity[2] == selected_digest:
                            injected = True
                            descriptor = os.open(
                                "replacement",
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                                dir_fd=directory,
                            )
                            try:
                                os.write(descriptor, replacement)
                            finally:
                                os.close(descriptor)
                            os.replace(
                                "replacement",
                                "payload",
                                src_dir_fd=directory,
                                dst_dir_fd=directory,
                            )
                    return original_rename(source, destination, *arguments, **keywords)

                with (
                    mock.patch.object(
                        lifecycle,
                        "CANONICAL_RELEASE_SHA256",
                        hashlib.sha256(payload).hexdigest(),
                    ),
                    mock.patch.object(lifecycle.os, "rename", side_effect=race_rename),
                ):
                    self.assertEqual(1, lifecycle.cleanup(target))
                retained = tuple(
                    entry["content"]
                    for entry in filesystem_snapshot(root).values()
                    if entry["type"] == "regular"
                )
                self.assertIn(replacement, retained)


class LifecycleVerifierTest(unittest.TestCase):
    def _copied_release(self, temporary: str) -> Path:
        release = Path(temporary) / "release"
        shutil.copytree(ROOT / ".codex", release / ".codex")
        shutil.copytree(ROOT / "config", release / "config")
        shutil.copytree(ROOT / "scripts", release / "scripts")
        shutil.copytree(PLUGIN, release / "plugins" / "ai-workflow")
        return release

    def _verify(
        self, release: Path, *, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(release / "plugins" / "ai-workflow" / "scripts" / "verify.sh")],
            cwd=release,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_verifier_requires_all_cleanup_lifecycle_scripts(self):
        for name in ("agent_lifecycle.py", "install-agents.sh", "uninstall-agents.sh"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                release = self._copied_release(temporary)
                (release / "plugins" / "ai-workflow" / "scripts" / name).unlink()
                self.assertNotEqual(0, self._verify(release).returncode)

    def test_verifier_rejects_invalid_cleanup_lifecycle_syntax(self):
        mutations = {
            "agent_lifecycle.py": "def broken(:\n",
            "install-agents.sh": "if then\n",
            "uninstall-agents.sh": "if then\n",
        }
        for name, content in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                release = self._copied_release(temporary)
                (release / "plugins" / "ai-workflow" / "scripts" / name).write_text(content)
                self.assertNotEqual(0, self._verify(release).returncode)

    def test_verifier_rejects_missing_python_syntax_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            release = self._copied_release(temporary)
            environment = dict(os.environ)
            environment["PYTHON_BIN"] = str(release / "missing-python")
            self.assertNotEqual(
                0, self._verify(release, environment=environment).returncode
            )



if __name__ == "__main__":
    unittest.main()
