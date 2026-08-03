# GPT 多模型协作工作流实验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成一个全局、跨项目、可关闭和可审计的半自动 AI 编排器，用 Luna Max、Terra xhigh、Sol medium/xhigh 完成方案规划、工程验收和整改闭环实验。

**Architecture:** 以一个标准库 Python CLI 作确定性编排核心，由 TOML 登记角色与路由，由 JSON Schema 固定任务和模型输出契约，由 git 快照、append-only JSONL 及人工闸门防止越权。真实模型运行通过不使用 shell 的 `codex exec` 子进程实现，单元测试默认使用假 runner，不消耗额度。

**Tech Stack:** Python 3.11+ 标准库（`argparse`、`dataclasses`、`enum`、`fcntl`、`hashlib`、`json`、`pathlib`、`subprocess`、`tomllib`、`unittest`）、Git、Codex CLI 0.146.0-alpha.9.2 或兼容后续版本。

## Global Constraints

- 模型选角固定为 Luna Max、Terra xhigh、Sol medium 和 Sol xhigh；工作量比例不得进入路由逻辑。
- 方案批准、整改授权、Sol xhigh 调用、最终验收、宪法变更、merge 和 push 必须人工决策。
- 禁止 `--dangerously-bypass-approvals-and-sandbox`、`shell=True`、自动删除 worktree、自动 merge 及自动 push。
- 不增加第三方运行依赖；测试使用 `python3 -m unittest`。
- 运行状态只写入 `data/state/ai-workflow/`，该目录必须 gitignore。
- 业务项目密钥不传入模型子进程；日志不记录环境变量值和完整原始数据。
- 一个 worktree 只有一个写入角色；验收对象必须固定 base/candidate commit。
- Luna 不得输出最终验收结论；Sol 输出也只能进入人工决策闸门。
- 每个任务最多 1 次技术重试、1 次同角色实现返工和 1 次跨模型升级。
- 任何实现不得修改 `/Users/lee/Documents/择时信号灯🚥/`。

---

## File Map

| 路径 | 唯一职责 |
|---|---|
| `README.md` | 已批准的唯一设计规格和项目入口 |
| `.gitignore` | 排除运行状态、字节码缓存和本地系统文件 |
| `config/ai_workflow.toml` | 角色、模型、推理档、权限、重试与路由闭集 |
| `config/ai_workflow_task.schema.json` | 任务信封对外 JSON Schema |
| `config/ai_workflow_result.schema.json` | 全角色共用的结构化输出 JSON Schema |
| `scripts/ai_workflow.py` | CLI、任务校验、状态机、路由、runner、git 安全、账本和报告 |
| `tests/test_ai_workflow.py` | 合法集及补集、状态迁移、权限、runner、git 和端到端假运行测试 |
| `data/state/ai-workflow/` | 本地运行状态；不进 Git |
| `docs/superpowers/plans/2026-08-03-ai-workflow-experiment.md` | 本实施计划 |

### Task 1: 固定配置、Schema 与项目卫生线

**Owner:** `luna_worker`

**Files:**
- Create: `.gitignore`
- Create: `config/ai_workflow.toml`
- Create: `config/ai_workflow_task.schema.json`
- Create: `config/ai_workflow_result.schema.json`
- Create: `tests/test_ai_workflow.py`

**Interfaces:**
- Consumes: `README.md` 第 3、4、6、7、8、9、10、11、12 节。
- Produces: `ai-workflow-1`、`ai-task-1`、`ai-result-1` 三个固定版本；后续任务只允许读取这三个值。

- [ ] **Step 1: 先写配置和 schema 闭集的失败测试**

```python
import json
import tomllib
import unittest
from pathlib import Path

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
```

- [ ] **Step 2: 运行测试确认因文件不存在而失败**

Run: `python3 -m unittest tests.test_ai_workflow.ContractFilesTest -v`  
Expected: `FileNotFoundError` for `config/ai_workflow.toml`.

- [ ] **Step 3: 创建最小配置**

```toml
version = "ai-workflow-1"

[policy]
max_technical_retries = 1
max_implementation_reworks = 1
max_cross_model_escalations = 1
automatic_xhigh = false
automatic_merge = false
automatic_push = false

[roles.luna]
model = "gpt-5.6-luna"
reasoning_effort = "max"
sandbox = "read-only"
allowed_statuses = ["SUPPORTED", "PARTIALLY_SUPPORTED", "NOT_SUPPORTED", "BLOCKED"]
instructions = """Handle only bounded tasks. Do not redefine scope or acceptance. Use the requested L0/L1/L2 evidence level. Never claim final acceptance."""

[roles.terra]
model = "gpt-5.6-terra"
reasoning_effort = "xhigh"
sandbox = "workspace-write"
allowed_statuses = ["IMPLEMENTED_CANDIDATE", "NEEDS_CLARIFICATION", "BLOCKED"]
instructions = """Own implementation only inside the authorized worktree and paths. Stop on semantic ambiguity. Never merge, push, or self-approve."""

[roles.sol_planner]
model = "gpt-5.6-sol"
reasoning_effort = "medium"
sandbox = "read-only"
allowed_statuses = ["PLAN_READY", "NEEDS_OWNER_DECISION", "INSUFFICIENT_EVIDENCE"]
instructions = """Convert evidence into a bounded plan with frozen invariants and decidable acceptance criteria. Do not implement."""

[roles.sol_reviewer]
model = "gpt-5.6-sol"
reasoning_effort = "medium"
sandbox = "read-only"
allowed_statuses = ["ACCEPTANCE_RECOMMENDED", "ACCEPTANCE_WITH_NOTES_RECOMMENDED", "REWORK_RECOMMENDED", "REJECT_RECOMMENDED", "ESCALATION_PROPOSED"]
instructions = """Review the pinned candidate against the task contract and evidence. Recommend a verdict but never enact owner decisions."""

[roles.sol_xhigh]
model = "gpt-5.6-sol"
reasoning_effort = "xhigh"
sandbox = "read-only"
requires_owner_approval = true
allowed_statuses = ["OPTION_A", "OPTION_B", "OPTION_C", "INSUFFICIENT_EVIDENCE"]
instructions = """Decide only the closed-set dispute in the authorized case file. Do not implement or invent an unlisted option."""
```

- [ ] **Step 4: 创建严格 JSON Schema**

Task schema 必须定义 `additionalProperties: false`，要求全部核心字段，并固定：

```json
{
  "schema_version": "ai-task-1",
  "task_type": "PLAN | ACCEPTANCE | REMEDIATION",
  "verification_level": "L0 | L1 | L2"
}
```

字段类型固定为：`task_id/objective/repository_root` 为非空字符串；`source_worktree/base_commit/candidate_commit` 为字符串或 `null`；`authoritative_files/allowed_write_paths/forbidden_actions/risk_flags/acceptance_commands/human_gates` 为不含重复项的字符串数组。`human_gates` 枚举固定为 `PLAN_APPROVAL`、`EXECUTION_APPROVAL`、`FINAL_ACCEPTANCE`、`XHIGH_APPROVAL`、`MERGE`、`PUSH`。`risk_flags` 只允许 README 第 9 节的九个值。

Result schema 要求 `role`、`status`、`summary`、`claims`、`evidence`、`counter_checks`、`changed_files`、`blind_spots`、`unresolved_questions` 和 `recommended_next_state`；`status` 枚举为所有角色合法值的并集，角与状态的交叉约束由运行时校验。`claims` 元素固定为 `{id, kind, text, evidence_ids}`，`kind` 仅 `FACT/INFERENCE/RECOMMENDATION`；`evidence` 元素固定为 `{id, type, locator, observation}`，`type` 仅 `FILE/COMMAND/HASH/TEST`；`counter_checks` 元素固定为 `{target_claim_id, method, result}`。所有对象均 `additionalProperties: false`。

- [ ] **Step 5: 创建 `.gitignore`**

```gitignore
.DS_Store
__pycache__/
*.py[cod]
data/state/ai-workflow/
```

- [ ] **Step 6: 重跑契约测试**

Run: `python3 -m unittest tests.test_ai_workflow.ContractFilesTest -v`  
Expected: all tests pass.

- [ ] **Step 7: 扫描禁止的自动化开关**

Run: `rg -n 'automatic_(xhigh|merge|push)\s*=\s*true|dangerously-bypass' config tests`  
Expected: no matches.

- [ ] **Step 8: 提交契约骨架**

```bash
git add .gitignore config tests/test_ai_workflow.py
git commit -m "feat: define workflow contracts"
```

### Task 2: 任务校验器与状态机

**Owner:** `luna_worker`

**Files:**
- Create: `scripts/ai_workflow.py`
- Modify: `tests/test_ai_workflow.py`

**Interfaces:**
- Produces: `load_task(path: Path) -> dict[str, object]`、`validate_task(task: Mapping[str, object]) -> None`、`next_state(current: str, target: str, *, owner_authorized: bool) -> str`。
- Error type: `WorkflowError(code: str, message: str)`，CLI 出错时只输出 `code` 和简短 `message`。

- [ ] **Step 1: 写合法集与补集测试**

```python
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
            "human_gates": ["PLAN_APPROVAL"]
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
```

- [ ] **Step 2: 写状态机非法跳转测试**

```python
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
```

- [ ] **Step 3: 运行新测试确认缺少模块**

Run: `python3 -m unittest tests.test_ai_workflow.TaskValidationTest tests.test_ai_workflow.StateMachineTest -v`  
Expected: import or attribute failure for `scripts.ai_workflow`.

- [ ] **Step 4: 实现最小校验器**

```python
TASK_FIELDS = frozenset({
    "schema_version", "task_id", "task_type", "objective", "repository_root",
    "source_worktree", "base_commit", "candidate_commit", "authoritative_files",
    "allowed_write_paths", "forbidden_actions", "risk_flags",
    "acceptance_commands", "verification_level", "human_gates",
})
TASK_TYPES = frozenset({"PLAN", "ACCEPTANCE", "REMEDIATION"})
VERIFICATION_LEVELS = frozenset({"L0", "L1", "L2"})


class WorkflowError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
```

`validate_task` 必须检查字段精确相等、版本、枚举、`task_id` 正则 `^AWF-[0-9]{8}-[0-9]{3,}$`、权威文件非空、ACCEPTANCE 的双 commit 及 REMEDIATION 的非空可写路径。

- [ ] **Step 5: 实现显式状态转移表**

```python
OWNER_ONLY_STATES = frozenset({
    "APPROVED_FOR_EXECUTION", "REWORK_AUTHORIZED",
    "ESCALATION_AUTHORIZED", "CLOSED",
})
TRANSITIONS = {
    "DRAFT": frozenset({"TASK_VALIDATED", "ABORTED"}),
    "TASK_VALIDATED": frozenset({"EVIDENCE_RUNNING", "BLOCKED", "ABORTED"}),
    "EVIDENCE_RUNNING": frozenset({"EVIDENCE_READY", "BLOCKED", "ABORTED"}),
    "EVIDENCE_READY": frozenset({"PLAN_OR_REVIEW_RUNNING", "BLOCKED", "ABORTED"}),
    "PLAN_OR_REVIEW_RUNNING": frozenset({"PLAN_READY", "REVIEW_READY", "BLOCKED", "ESCALATION_PROPOSED"}),
    "PLAN_READY": frozenset({"AWAITING_OWNER_DECISION"}),
    "REVIEW_READY": frozenset({"AWAITING_OWNER_DECISION"}),
    "AWAITING_OWNER_DECISION": frozenset({
        "APPROVED_FOR_EXECUTION", "REWORK_AUTHORIZED", "ESCALATION_AUTHORIZED",
        "DEFERRED", "CLOSED", "ABORTED",
    }),
}
```

- [ ] **Step 6: 运行测试**

Run: `python3 -m unittest tests.test_ai_workflow.TaskValidationTest tests.test_ai_workflow.StateMachineTest -v`  
Expected: all pass.

- [ ] **Step 7: 提交校验器与状态机**

```bash
git add scripts/ai_workflow.py tests/test_ai_workflow.py
git commit -m "feat: validate workflow tasks and states"
```

### Task 3: 本地任务存储、人工决策与假 Runner

**Owner:** `luna_worker`

**Files:**
- Modify: `scripts/ai_workflow.py`
- Modify: `tests/test_ai_workflow.py`

**Interfaces:**
- Produces: `WorkflowStore(root: Path)`、`create_task(task: dict) -> Path`、`append_event(task_id: str, event: dict) -> None`、`record_decision(task_id: str, decision: dict) -> None`、`FakeRunner.run(role: str, task: dict) -> dict`。
- Runtime root: `<global-project>/data/state/ai-workflow/<task_id>/`.

- [ ] **Step 1: 写原子创建与 append-only 决策测试**

```python
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
```

- [ ] **Step 2: 写锁和重复运行测试**

`WorkflowStore.lock(task_id)` 使用 `fcntl.flock(..., LOCK_EX | LOCK_NB)`；第二个锁必须抛 `TASK_ALREADY_RUNNING`。

- [ ] **Step 3: 写 FakeRunner 角色状态测试**

```python
class FakeRunnerTest(unittest.TestCase):
    def test_luna_fake_result_never_claims_acceptance(self):
        result = workflow.FakeRunner().run("luna", TaskValidationTest().valid_task())
        self.assertEqual(result["role"], "luna")
        self.assertEqual(result["status"], "SUPPORTED")
        self.assertNotIn("ACCEPTED", result["status"])
```

- [ ] **Step 4: 运行测试确认新接口未实现**

Run: `python3 -m unittest tests.test_ai_workflow.WorkflowStoreTest tests.test_ai_workflow.FakeRunnerTest -v`  
Expected: attribute failures.

- [ ] **Step 5: 实现原子写入与追加账本**

`atomic_write_json` 必须在同目录使用临时文件、`flush`、`os.fsync` 和 `os.replace`。`append_jsonl` 以单行紧凑 JSON 追加，`flush` 后 `fsync`，不提供修改或删除 API。

- [ ] **Step 6: 实现 CLI 骨架**

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("new", "validate", "run", "status", "decide", "resume", "abort", "report"):
        sub.add_parser(name)
    return parser
```

Task 3 只要求 `new`、`validate`、`status`、`decide` 和带 `--runner fake` 的 `run` 可用；其他子命令必须明确返回 `NOT_IMPLEMENTED_IN_CURRENT_STAGE`，不得静默成功。

- [ ] **Step 7: 运行 Task 1–3 全部测试**

Run: `python3 -m unittest -v`  
Expected: all pass.

- [ ] **Step 8: 运行语法检查**

Run: `python3 -m compileall -q scripts tests`  
Expected: exit 0.

- [ ] **Step 9: 提交 Luna 工段候选**

```bash
git add scripts/ai_workflow.py tests/test_ai_workflow.py
git commit -m "feat: add local workflow skeleton"
```

### Task 4: Luna 工段独立复核闸门

**Owner:** 主控 Sol（只读复核）

**Files:**
- Review: `config/ai_workflow.toml`
- Review: `config/ai_workflow_task.schema.json`
- Review: `config/ai_workflow_result.schema.json`
- Review: `scripts/ai_workflow.py`
- Review: `tests/test_ai_workflow.py`

**Interfaces:**
- Consumes: Task 1–3 候选 commit。
- Produces: 人工可读的 `ACCEPTANCE_RECOMMENDED` 或 `REWORK_RECOMMENDED`；不修改代码。

- [ ] **Step 1: 固定候选提交和工作树**

Run: `git status --short && git rev-parse HEAD`  
Expected: empty status followed by one commit SHA.

- [ ] **Step 2: 亲手跑全部单元测试与编译检查**

Run: `python3 -m unittest -v && python3 -m compileall -q scripts tests`  
Expected: exit 0.

- [ ] **Step 3: 执行四个短命双向变异**

1. 把 `automatic_xhigh = false` 改为 `true`，契约测试必须红；
2. 把 Luna 合法状态中加入 `ACCEPTED`，闭集测试必须红；
3. 删除 `OWNER_ONLY_STATES` 中的 `CLOSED`，状态机测试必须红；
4. 使第二个任务锁静默成功，重入测试必须红。

每次变异前确认基线绿，变异后观察指定测试转红，然后还原并确认工作树干净。

- [ ] **Step 4: 核对 Luna L0/L1/L2 实现边界**

Run: `rg -n 'ACCEPTED|REJECTED|MERGED|EFFECTIVE' scripts config`  
Expected: any match must belong to Sol status union or explicit rejection tests; Luna role mapping must contain zero such values.

- [ ] **Step 5: 出具闸门结论**

若发现任一越权、空转测试或变异存活，停在 Task 4 返工；不得进入真实 runner。

### Task 5: 真实 Codex Runner 与输出契约

**Owner:** Terra xhigh

**Files:**
- Modify: `scripts/ai_workflow.py`
- Modify: `tests/test_ai_workflow.py`

**Interfaces:**
- Produces: `build_role_prompt(role: str, task: Mapping[str, object], contract: Mapping[str, object], evidence_paths: Sequence[Path]) -> str`、`build_codex_command(role: str, repo: Path, output_path: Path, schema_path: Path) -> list[str]`、`sanitized_environment(source: Mapping[str, str]) -> dict[str, str]`、`run_codex(role: str, task: dict, prompt: str, paths: RunPaths) -> dict`、`validate_role_result(role: str, result: Mapping[str, object], changed_files: set[str]) -> None`。

- [ ] **Step 1: 写命令列表测试**

```python
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
```

- [ ] **Step 2: 写环境脱敏测试**

```python
def test_business_secrets_are_not_forwarded(self):
    env = workflow.sanitized_environment({
        "HOME": "/tmp/home", "PATH": "/usr/bin", "CODEX_HOME": "/tmp/codex",
        "TUSHARE_TOKEN": "secret", "OPENAI_API_KEY": "secret", "DB_PASSWORD": "secret",
    })
    self.assertEqual(env["HOME"], "/tmp/home")
    self.assertNotIn("TUSHARE_TOKEN", env)
    self.assertNotIn("OPENAI_API_KEY", env)
    self.assertNotIn("DB_PASSWORD", env)
```

- [ ] **Step 3: 写角色与状态交叉校验测试**

Luna 返回 `ACCEPTANCE_RECOMMENDED`、Terra 返回 `SUPPORTED`、Sol reviewer 返回 `IMPLEMENTED_CANDIDATE` 都必须拒绝。只读角色的真实 diff 非空时必须拒绝，不得仅相信模型的 `changed_files` 声明。

- [ ] **Step 4: 写最小上下文提示词测试**

`build_role_prompt` 必须包含 TOML 中的该角色 `instructions`、完整任务信封、点名证据文件的路径及哈希，以及“只输出 `ai-result-1` JSON”。它不得自动读取完整聊天记录、整个 `registry/` 或未在 `authoritative_files` 中的文档。

- [ ] **Step 5: 运行新测试确认失败**

Run: `python3 -m unittest tests.test_ai_workflow.CodexCommandTest -v`  
Expected: missing function failures.

- [ ] **Step 6: 实现最小上下文提示词和无 shell 命令构建**

Command list 必须包含：

```python
[
    "codex", "exec", "-m", model,
    "-c", f'model_reasoning_effort="{effort}"',
    "-s", sandbox,
    "-C", str(repo),
    "--json",
    "--output-schema", str(schema_path),
    "-o", str(output_path),
    "-",
]
```

`subprocess.run` 必须使用列表参数、`shell=False`、`input=prompt`、`text=True`、有限 `timeout`、脱敏环境及指定 `cwd`。

- [ ] **Step 7: 保存 JSONL 事件与最终输出**

stdout 原样写入任务 `logs/<role>-events.jsonl`，但写入前按密钥名和长高熵串执行脱敏。`-o` 文件只在 JSON 可解析、schema 核心字段完整和角色状态合法后接纳。

- [ ] **Step 8: 使用 mock 子进程运行测试**

Patch `subprocess.run`，断言 `shell` 不是 `True`、stdin 包含任务契约、密钥未转发，并模拟超时、非零退出和非法 JSON。

- [ ] **Step 9: 提交真实 runner**

```bash
git add scripts/ai_workflow.py tests/test_ai_workflow.py
git commit -m "feat: add safe codex role runner"
```

### Task 6: Git 快照、权限范围与 Worktree 安全

**Owner:** Terra xhigh

**Files:**
- Modify: `scripts/ai_workflow.py`
- Modify: `tests/test_ai_workflow.py`

**Interfaces:**
- Produces: `RepoSnapshot(head: str, status: tuple[str, ...])`、`capture_repo(repo: Path) -> RepoSnapshot`、`assert_pinned(snapshot: RepoSnapshot, repo: Path) -> None`、`changed_paths(repo: Path, base: str, candidate: str) -> set[str]`、`assert_allowed_changes(changed: set[str], allowed: Sequence[str]) -> None`、`create_worktree(task: dict, owner_authorized: bool) -> Path`。

- [ ] **Step 1: 用临时 Git 仓库写 HEAD 漂移测试**

在 `tempfile.TemporaryDirectory()` 内运行 `git init`、配置本地测试身份、建立两个提交。第二个提交后，用第一个快照调用 `assert_pinned` 必须报 `HEAD_DRIFT`。

- [ ] **Step 2: 写只读角色改动和越界测试**

`changed_paths` 返回 `{"allowed/a.py", "forbidden/b.py"}` 而允许范围只有 `allowed/`时，必须报 `OUT_OF_SCOPE_CHANGE`。Luna/Sol 运行前后 snapshot 不一致必须报 `READ_ONLY_ROLE_MODIFIED_REPO`。

- [ ] **Step 3: 写未授权 Worktree 测试**

`create_worktree(task, owner_authorized=False)` 必须在运行任何 git 命令前报 `OWNER_AUTHORIZATION_REQUIRED`。

- [ ] **Step 4: 实现不经 shell 的 git 封装**

```python
def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False, capture_output=True, text=True, timeout=30,
    )
    if completed.returncode != 0:
        raise WorkflowError("GIT_COMMAND_FAILED", completed.stderr.strip())
    return completed.stdout.strip()
```

不得为方便把任务提供的路径拼成 shell 命令。

- [ ] **Step 5: 实现受控 Worktree 创建**

分支固定为 `aiwf/<task_id-lower>`，目录固定为目标仓库的 `.codex-worktrees/<task_id-lower>`。任务 ID 在进入 git 命令前已通过正则，且必须有 `APPROVED_FOR_EXECUTION` 决策记录。编排器不提供 worktree 删除命令。

- [ ] **Step 6: 运行 Git 测试和全量测试**

Run: `python3 -m unittest -v`  
Expected: all pass; temporary repositories are removed by `TemporaryDirectory`.

- [ ] **Step 7: 提交 Git 安全层**

```bash
git add scripts/ai_workflow.py tests/test_ai_workflow.py
git commit -m "feat: enforce repository write boundaries"
```

### Task 7: 三条流水线、人工闸门与重试上限

**Owner:** Terra xhigh

**Files:**
- Modify: `scripts/ai_workflow.py`
- Modify: `tests/test_ai_workflow.py`

**Interfaces:**
- Produces: `route(task: Mapping[str, object]) -> tuple[str, ...]`、`run_until_gate(task_id: str, *, runner: Runner, allow_live_model: bool) -> str`、`apply_owner_decision(task_id: str, decision: str, actor: str) -> str`、`RetryBudget`.

- [ ] **Step 1: 写确定性路由测试**

```python
class RoutingTest(unittest.TestCase):
    def test_plan_route(self):
        self.assertEqual(workflow.route({"task_type": "PLAN", "risk_flags": []}), ("luna", "sol_planner"))

    def test_acceptance_route(self):
        self.assertEqual(workflow.route({"task_type": "ACCEPTANCE", "risk_flags": []}), ("luna", "sol_reviewer"))

    def test_plain_remediation_route(self):
        self.assertEqual(workflow.route({"task_type": "REMEDIATION", "risk_flags": []}), ("terra", "luna", "sol_reviewer"))

    def test_high_risk_remediation_plans_first(self):
        self.assertEqual(
            workflow.route({"task_type": "REMEDIATION", "risk_flags": ["PIT"]}),
            ("sol_planner", "terra", "luna", "sol_reviewer"),
        )
```

- [ ] **Step 2: 写 Sol xhigh 非自动测试**

即使 Sol reviewer 返回 `ESCALATION_PROPOSED`，下一状态也只能是 `AWAITING_OWNER_DECISION`。没有 `ESCALATION_AUTHORIZED` 追加决策时，runner 的调用记录中必须零次 `sol_xhigh`。

- [ ] **Step 3: 写循环上限测试**

Fake runner 连续返回两次非法 JSON 时，调用数必须是 2（首次 + 1 次技术重试）并进入 `BLOCKED`。Terra 首次返回实现错误、返工后再次失败时，不得第三次自动调用 Terra。

- [ ] **Step 4: 实现 `RetryBudget`**

```python
@dataclass
class RetryBudget:
    technical_retries: int = 0
    implementation_reworks: int = 0
    cross_model_escalations: int = 0

    def consume_technical(self) -> None:
        if self.technical_retries >= 1:
            raise WorkflowError("RETRY_BUDGET_EXHAUSTED", "technical")
        self.technical_retries += 1
```

实现对应的 `consume_rework` 和 `consume_escalation`，上限均为 1。

- [ ] **Step 5: 实现流程执行到闸门即停**

`run_until_gate` 每完成一角色追加事件，遇到 `PLAN_READY`、`REVIEW_READY`、`ESCALATION_PROPOSED` 或任何需人工决策状态立即返回，不等待 stdin。

在 Task 2 的 `TRANSITIONS` 上显式增加：

```python
{
    "APPROVED_FOR_EXECUTION": frozenset({"WORKTREE_READY", "ABORTED"}),
    "REWORK_AUTHORIZED": frozenset({"IMPLEMENTATION_RUNNING", "ABORTED"}),
    "ESCALATION_AUTHORIZED": frozenset({"PLAN_OR_REVIEW_RUNNING", "ABORTED"}),
    "DEFERRED": frozenset({"TASK_VALIDATED", "CLOSED", "ABORTED"}),
    "WORKTREE_READY": frozenset({"IMPLEMENTATION_RUNNING", "BLOCKED", "ABORTED"}),
    "IMPLEMENTATION_RUNNING": frozenset({"IMPLEMENTED_CANDIDATE", "BLOCKED", "NEEDS_REPLAN"}),
    "IMPLEMENTED_CANDIDATE": frozenset({"PRECHECK_RUNNING", "BLOCKED"}),
    "PRECHECK_RUNNING": frozenset({"PRECHECK_READY", "BLOCKED"}),
    "PRECHECK_READY": frozenset({"PLAN_OR_REVIEW_RUNNING", "BLOCKED"}),
}
```

`DEFERRED -> TASK_VALIDATED/CLOSED`、`REWORK_AUTHORIZED -> IMPLEMENTATION_RUNNING` 及 `ESCALATION_AUTHORIZED -> PLAN_OR_REVIEW_RUNNING` 仍必须有新的所有者决策记录，不能因状态表中存在该边就自动跳转。

- [ ] **Step 6: 实现 `decide` 闭集**

合法决策只有 `approve_execution`、`authorize_rework`、`authorize_escalation`、`defer`、`close`、`abort`。`actor` 不得为空，决策事件必须包含 UTC 时间、上一状态、新状态和当时任务文件 SHA256。

- [ ] **Step 7: 运行假 runner 端到端测试**

Run: `python3 -m unittest -v`  
Expected: all pass without invoking `codex`.

- [ ] **Step 8: 提交编排流程**

```bash
git add scripts/ai_workflow.py tests/test_ai_workflow.py
git commit -m "feat: orchestrate gated model workflows"
```

### Task 8: 指标账本与单文件脱敏报告

**Owner:** Terra xhigh

**Files:**
- Modify: `scripts/ai_workflow.py`
- Modify: `tests/test_ai_workflow.py`

**Interfaces:**
- Produces: `record_metrics(task_id: str, run: Mapping[str, object]) -> None`、`aggregate_metrics(root: Path) -> dict`、`render_report(metrics: Mapping[str, object]) -> str`。

- [ ] **Step 1: 写不伪造 token 的测试**

JSONL 没有官方使用量字段时，`metrics.json` 的 `token_usage` 必须是 `null`，不得用字符数、时间或模型权重估算。

- [ ] **Step 2: 写 Luna 独有价值与复核成本测试**

`aggregate_metrics` 必须分开计算 `luna_unique_findings`、`luna_findings_adopted_by_sol`、`luna_self_check_seconds`、`sol_verification_seconds`、`semantic_reworks`、`full_suite_runs` 和 `end_to_end_seconds`。

- [ ] **Step 3: 写脱敏报告测试**

向原始事件注入 `TUSHARE_TOKEN=abc123`、`OPENAI_API_KEY=sk-test-value` 及 64 字节高熵串，报告中必须只出现 `[REDACTED]`，不得出现原值。

- [ ] **Step 4: 实现指标原样记录**

时间使用 `time.monotonic()` 计算耗时，UTC 时间用 `datetime.now(timezone.utc).isoformat()` 记录。模型使用量只在 JSONL 显式提供可解析数值时写入。

- [ ] **Step 5: 实现单 Markdown 报告**

`report --output <path>` 生成一份包含校准期/试验期任务数、按角色调用数、Sol 参与量、首次交付通过率、重复全量测试、Luna 独有发现和停止线事件的报告。运行中不生成多份进度文档。

- [ ] **Step 6: 运行全量测试并提交**

```bash
python3 -m unittest -v
python3 -m compileall -q scripts tests
git add scripts/ai_workflow.py tests/test_ai_workflow.py
git commit -m "feat: measure workflow experiment outcomes"
```

### Task 9: 精简全局 `luna_worker` 为轻量适配器

**Owner:** 主控（该文件在全局目录，不交给自己修改自己的 Luna）

**Files:**
- Modify: `/Users/lee/.codex/agents/luna-worker.toml`

**Interfaces:**
- Consumes: 任务信封中的 `verification_level` 和所在仓库规则。
- Produces: 交互式具名 Luna Max 子代理；不是自动编排的第二套规则。

- [ ] **Step 1: 记录原文件 SHA256 和 diff 基线**

Run: `shasum -a 256 /Users/lee/.codex/agents/luna-worker.toml`  
Expected: one hash recorded in the task evidence; do not print unrelated global config.

- [ ] **Step 2: 缩减 `developer_instructions`**

保留：

```text
只接受明确、有界、可独立完成的委派；
不修改总目标、验收标准或工作范围；
优先读取所在仓库的持久规则和任务信封；
按 L0/L1/L2 要求交付最小证据包；
不声称最终验收；
需要宪法、PIT、安全、跨卡或开放式判断时立即停止并回交。
```

删除“每项任务最多 20 条主张、关键结论三种反证”的统一强制要求，但保留“自检不构成最终验收”。

- [ ] **Step 3: 校验 TOML 及 Codex 配置加载**

```bash
python3 -c 'import pathlib,tomllib; p=pathlib.Path("/Users/lee/.codex/agents/luna-worker.toml"); d=tomllib.loads(p.read_text()); assert d["name"]=="luna_worker"; assert d["model"]=="gpt-5.6-luna"; assert d["model_reasoning_effort"]=="max"'
codex --strict-config --version
```

Expected: both exit 0.

- [ ] **Step 4: 展示精确 diff 与新 SHA256**

Diff 必须只影响 `description`/`developer_instructions`，`name`、`model`和 `model_reasoning_effort` 不变。

### Task 10: 最终集成验收与逐级启用

**Owner:** Sol medium 验收；实验启用由所有者裁定

**Files:**
- Review: all tracked files
- Runtime only: `data/state/ai-workflow/`
- Optional final export after experiment: one owner-approved Markdown report

**Interfaces:**
- Consumes: Tasks 1–9 最终 candidate commit 和全局 Agent diff。
- Produces: `ACCEPTANCE_RECOMMENDED` / `REWORK_RECOMMENDED`；批准后可进入历史只读校准期。

- [ ] **Step 1: 在干净 candidate commit 上运行完整门禁一次**

```bash
git status --short
python3 -m unittest -v
python3 -m compileall -q scripts tests
```

Expected: empty status before tests; all tests pass. Tests must not leave tracked changes.

- [ ] **Step 2: 亲手执行六个高价值变异**

1. Luna 合法状态加 `ACCEPTED`；
2. `automatic_xhigh` 改 `true`；
3. 跳过人工 `APPROVED_FOR_EXECUTION`；
4. 只读角色 diff 守卫改为空操作；
5. 重试上限由 1 改 2；
6. Codex 子进程改用 `shell=True`。

每个变异必须有至少一条指定测试转红，还原后工作树干净。

- [ ] **Step 3: 运行假 runner 全闭环**

```text
PLAN: Luna → Sol planner → owner gate
ACCEPTANCE: Luna → Sol reviewer → owner gate
REMEDIATION: owner authorization → Terra → Luna → Sol reviewer → owner gate
```

断言三条流程的模型调用顺序、状态、事件和人工决策可逐条对账。

- [ ] **Step 4: 运行首次真实 Luna 只读冒烟**

仅对本项目的 `README.md` 创建 PLAN 任务，`verification_level=L1`，目标为“核对最终设计的状态机与人工闸门是否自洽”。运行时必须显式提供 `--allow-live-model`，并且不启动 Sol 或 Terra。

- [ ] **Step 5: 验证 Luna 冒烟为真只读且结构合法**

Run: `git status --short`  
Expected: no tracked or untracked changes caused by the Luna run outside ignored `data/state/ai-workflow/`.

核对 `luna-result.json` 最多 5 条关键主张、1 次交叉检查、无最终验收状态。

- [ ] **Step 6: 生成校准起点报告并等待所有者裁定**

报告必须如实标注仅证明 Luna 只读链路可运行，不得称工作流已降本增效。所有者批准后才能依次启用历史 Luna + Sol 及低风险 Terra 写任务。

- [ ] **Step 7: 提交最终候选**

```bash
git add README.md .gitignore config scripts tests docs/superpowers/plans/2026-08-03-ai-workflow-experiment.md
git commit -m "feat: complete workflow experiment MVP"
```

## Plan Self-Review Results

- **Spec coverage:** README 第 1–18 节分别映射到 Task 1–10；自动路由、人工闸门、L0/L1/L2、安全、指标、全局 Agent 精简及逐级启用均有实施和验收步骤。
- **Scope:** 一个顺序子系统，Task 1–4 产生不调用真模型的可测 MVP，Task 5–8 接入工程能力，Task 9–10 完成全局适配和受控启用。
- **Type consistency:** `task_id`、`role`、`status`、`verification_level`、`RetryBudget` 及各 runner/store 接口在各任务间名称一致。
- **No hidden implementation:** Task 1–3 不调用真 Codex；Task 5 之前必须经 Task 4 独立闸门。
- **No automatic authority:** 没有任务可自动启动 Sol xhigh、merge、push 或作最终验收。
