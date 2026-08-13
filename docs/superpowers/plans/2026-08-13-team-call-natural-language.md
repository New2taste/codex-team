# Team Call 自然语言调用指令实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户用一句 `team call <任务>` 安全地进入既有工作流：固定的低风险 L0 检查可立即执行，精确只读 L1 交给单个 Luna Max，其他事项创建可审计 intake 并停在既有计划/owner gate。

**Architecture:** 新建纯标准库 `ai_workflow_team_call` 模块，负责严格前缀解析、保守分类、append-only 调用收据和同调用去重；它不拥有角色路由、任务状态机或模型执行。`ai_workflow.py` 只提供受限的控制器适配与 `team-call` CLI；计划/写入路径继续使用现有 task、route、frozen-plan、owner-decision 和 dispatch 机制。根运行时和 Plugin 运行时保持字节一致。

**Tech Stack:** Python 3.11 标准库、argparse、JSON/JSONL、fcntl 文件锁、unittest、POSIX shell、Git。

## 全局约束

- 仅首个非空字符后的 `team call`（ASCII、不区分大小写）触发；接受空白、`:` 或 `：` 分隔，代码/引用/正文中的出现不得触发。
- 不接受任意 shell；L0 只允许固定 argv，且不得网络访问、写入、使用凭据或启动模型。
- L1 必须有精确、仓库内、非 symlink 的只读证据文件和 L1 证据契约；否则为 `PLAN_REQUIRED`，不猜测。
- 任何写入、外部系统、安全/权限、多阶段或不确定事项均为 `PLAN_REQUIRED`，不因 `team call` 放宽 owner gate、冻结信封、retry ladder、Terra review 或 Sol final acceptance。
- 调用按 `call_id` 串行且幂等；重复调用返回原收据，绝不重复启动命令、Luna 或 dispatch。ledger 漂移 fail closed。
- Luna Max 仅能执行合格 L1 只读工作；Terra xhigh 与 Sol 的既有角色边界不改变；一次 call 只有一个活动执行者。
- 根 `scripts/` 与 Plugin `runtime/` 的新增模块必须字节一致，`verify.sh` 和分发测试必须覆盖该镜像。
- 无自动 merge、push、外网、副作用 shell 或新第三方依赖。

## 文件结构

| 文件 | 责任 |
|---|---|
| `scripts/ai_workflow_team_call.py` | 纯解析、保守 intent 分类、收据值对象、registry 锁/JSONL replay 校验。 |
| `scripts/ai_workflow.py` | 导入并重导出公开 API；以固定 argv 执行 L0；为 L1/计划路径构造受限 task/intake；增加 CLI。 |
| `tests/test_ai_workflow_team_call.py` | 真实 registry/临时 Git 仓库上的 parser、注入、分类、幂等和控制器集成测试。 |
| `tests/test_ai_workflow.py` | CLI main() 与现有 WorkflowStore/路由/状态机的不变量回归。 |
| `README.md` | 面向用户的 `team call` 语法、示例、风险分流与不并行/不越权说明。 |
| `plugins/ai-workflow/skills/orchestration/SKILL.md` | 已安装 Skill 的等价调用契约和 Luna Max L1 边界。 |
| `plugins/ai-workflow/scripts/verify.sh` | 将 `ai_workflow_team_call.py` 纳入 root→Plugin byte parity。 |
| `plugins/ai-workflow/runtime/ai_workflow_team_call.py` | 根模块的字节一致发布镜像。 |
| `tests/test_ai_workflow_distribution.py` | 新 runtime copy、文档措辞、复制发布篡改的分发契约。 |

## 公开值对象与错误码

Task 1 产出下列精确接口；只有 `ai_workflow.py` 把其 `TeamCallError` 映射为
既有 `WorkflowError`：

```python
DIRECTIVE_VERSION = "team-call-1"

@dataclass(frozen=True)
class TeamCall:
    raw_message: str
    objective: str
    raw_request_sha256: str

@dataclass(frozen=True)
class TeamCallIntent:
    disposition: Literal["DIRECT_L0", "DIRECT_L1", "PLAN_REQUIRED", "BLOCKED"]
    risk_reasons: tuple[str, ...]
    l0_action: str | None
    evidence_path: str | None

@dataclass(frozen=True)
class TeamCallReceipt:
    call_id: str
    raw_request_sha256: str
    intake_sha256: str
    disposition: str
    risk_reasons: tuple[str, ...]
    task_id: str | None
    created_at_utc: str
    result_sha256: str | None

@dataclass(frozen=True)
class TeamCallRoute:
    task_id: str | None
    result_sha256: str | None
```

Stable rejection codes: `TEAM_CALL_EMPTY`, `TEAM_CALL_INVALID`,
`TEAM_CALL_UNSAFE_INPUT`, `TEAM_CALL_IDENTITY_DRIFT`,
`TEAM_CALL_LEDGER_INVALID`, `TEAM_CALL_ALREADY_RUNNING`,
`TEAM_CALL_EVIDENCE_INVALID`, and `TEAM_CALL_PLAN_REQUIRED`.

The initial L0 allowlist has only these normalized objectives and fixed argv:

```python
"检查当前工作区状态" -> ("git", "status", "--porcelain=v1", "--untracked-files=all")
"核对 plugin 根/镜像一致性" -> ("sh", "plugins/ai-workflow/scripts/verify.sh")
```

The only initial L1 grammar is `核对文件 <repo-relative-path>` with one nonempty
path and no other clause. Every other valid task body is conservatively
`PLAN_REQUIRED`; no heuristic verb matching may select direct execution.

---

### Task 1: 解析、保守分类和调用收据账本

**Owner:** Luna Max — bounded pure-module implementation and tests; no core routing change.

**Files:**
- Create: `scripts/ai_workflow_team_call.py`
- Create: `tests/test_ai_workflow_team_call.py`

**Interfaces:**
- Produces `parse_team_call(message) -> TeamCall | None`,
  `classify_team_call(call) -> TeamCallIntent`, `team_call_id(call, intent) -> str`,
  and `TeamCallRegistry(root).execute_once(call, intent, executor) -> TeamCallReceipt`.
- `TeamCallRegistry` persists `team-calls.jsonl` plus `.team-call.lock` at the
  provided state root. It validates every pre-existing row before returning or
  appending; a malformed row blocks future calls without rewriting it. The
  supplied `executor(receipt) -> TeamCallRoute` runs while the global lock is
  held, so it is the single serialized place where a direct action may start.

- [ ] **Step 1: Write failing parser/classifier/registry tests**

  Create `TeamCallContractTest` using a real temporary state directory. Include
  the following executable assertions:

  ```python
  def test_only_a_leading_team_call_directive_is_recognized(self):
      self.assertIsNone(team.parse_team_call("请解释 team call 的含义"))
      self.assertIsNone(team.parse_team_call("> team call 检查当前工作区状态"))
      self.assertEqual(
          "检查当前工作区状态",
          team.parse_team_call("  TeAm\tCaLl：检查当前工作区状态").objective,
      )

  def test_exact_l0_allowlist_never_accepts_user_shell(self):
      safe = team.classify_team_call(team.parse_team_call("team call 检查当前工作区状态"))
      self.assertEqual("DIRECT_L0", safe.disposition)
      unsafe = team.parse_team_call("team call 检查当前工作区状态; rm -rf x")
      with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_UNSAFE_INPUT"):
          team.classify_team_call(unsafe)

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
      (self.root / "team-calls.jsonl").write_text('{"call_id":"bad"}\n')
      with self.assertRaisesRegex(team.TeamCallError, "TEAM_CALL_LEDGER_INVALID"):
          self.registry.execute_once(call, intent, executor)
  ```

  Also cover `TEAM_CALL_EMPTY`, no match versus malformed leading directive,
  full-width colon, duplicate request hashes with distinct normalized intake,
  L1 relative-file grammar, `..`, absolute path, embedded NUL, a callback
  exception that leaves a terminal blocked route, and two-process/same-lock
  execution behavior.

- [ ] **Step 2: Run the focused tests and confirm RED**

  Run:

  ```bash
  /Users/lee/.local/bin/python3.11 -m unittest -v tests.test_ai_workflow_team_call.TeamCallContractTest
  ```

  Expected: import/API failures for `ai_workflow_team_call`; do not proceed on
  unrelated fixture or test-discovery failures.

- [ ] **Step 3: Implement the pure module minimally**

  Implement no subprocess calls in this task. Use a start-anchored regex that
  accepts only the declared separators and returns `None` before any directive
  mutation. Calculate SHA-256 over the exact UTF-8 raw message. Build the
  intent SHA-256 from `_canonical_json`-equivalent sorted JSON of directive
  version, normalized objective, disposition, reasons, action and evidence
  path. Reject all metacharacters (`;|&$\`\n\r\\`) before allowlist matching;
  do not try to sanitize them.

  `TeamCallRegistry` must use `fcntl.flock(LOCK_EX | LOCK_NB)` on the global
  lock, parse every JSONL row as either an exact `TEAM_CALL_RECEIVED` or exact
  `TEAM_CALL_ROUTED` event, recompute canonical identity, then append through
  a flushed and fsynced append. For a new call it appends `RECEIVED`, invokes
  the callback under that lock, and appends one terminal `ROUTED` event that
  binds `task_id` and result digest. A callback error must append a terminal
  blocked route before re-raising. A completed matching call returns its
  original receipt without invoking the callback; a partial or nonmatching
  same-ID history raises `TEAM_CALL_ALREADY_RUNNING` or
  `TEAM_CALL_IDENTITY_DRIFT` respectively.

- [ ] **Step 4: Run focused tests to confirm GREEN and mutate the real guard**

  Run the focused command. Then use a disposable copy that changes the
  `DIRECT_L0` status entry from its fixed argv to an argv containing `sh` and
  assert the unsafe test becomes RED; restore the source. Run:

  ```bash
  /Users/lee/.local/bin/python3.11 -m unittest -v tests.test_ai_workflow_team_call.TeamCallContractTest
  /Users/lee/.local/bin/python3.11 -m compileall -q scripts tests
  git diff --check
  ```

- [ ] **Step 5: Commit and obtain independent Terra xhigh adversarial review**

  ```bash
  git add scripts/ai_workflow_team_call.py tests/test_ai_workflow_team_call.py
  git commit -m "feat(workflow): parse Team Call directives"
  ```

  A different Terra xhigh reviewer must attack prefix recognition, ledger
  replay/identity drift, Unicode separators, path traversal/symlink inputs and
  all shell metacharacters. The owner must not self-accept.

---

### Task 2: Core controller, task handoff and CLI

**Owner:** Terra xhigh — integrates state/CLI execution without changing routing policy.

**Files:**
- Modify: `scripts/ai_workflow.py`
- Modify: `tests/test_ai_workflow.py`
- Modify: `tests/test_ai_workflow_team_call.py`

**Interfaces:**
- Re-export `parse_team_call` and add
  `run_team_call(message, *, repository_root, state_root, controller) -> TeamCallReceipt`.
- Add `ai-workflow team-call <message> --root STATE_ROOT --repository-root REPO`
  and `--runner {fake,live}` / `--allow-live-model` using the existing live
  authorization rules; direct live L1 never runs merely because the phrase was
  parsed.
- The controller protocol has `run_l0(argv: tuple[str, ...], cwd: Path)` and
  `run_l1(task: Mapping[str, object], *, role: Literal["luna"])`. Test fakes implement those calls; the
  production controller invokes subprocess with `shell=False` and supplies
  only fixed argv or the existing pinned Luna task contract.

- [ ] **Step 1: Write failing end-to-end and CLI tests**

  Use a real temporary Git repository and a fake controller. Add tests that
  prove concrete behavior rather than mock calls:

  ```python
  def test_l0_runs_fixed_git_status_once_and_returns_its_receipt(self):
      receipt = workflow.run_team_call(
          "team call 检查当前工作区状态", repository_root=self.repo,
          state_root=self.root, controller=self.controller,
      )
      self.assertEqual("DIRECT_L0", receipt.disposition)
      self.assertEqual(("git", "status", "--porcelain=v1", "--untracked-files=all"),
                       self.controller.executed_argv)
      repeated = workflow.run_team_call(
          "team call 检查当前工作区状态", repository_root=self.repo,
          state_root=self.root, controller=self.controller,
      )
      self.assertEqual(receipt, repeated)
      self.assertEqual(1, self.controller.execution_count)

  def test_explicit_l1_file_is_luna_only_and_cannot_write(self):
      receipt = workflow.run_team_call(
          "team call 核对文件 README.md", repository_root=self.repo,
          state_root=self.root, controller=self.controller,
      )
      self.assertEqual("DIRECT_L1", receipt.disposition)
      self.assertEqual("luna", self.controller.l1_role)
      self.assertEqual([], self.controller.l1_task["allowed_write_paths"])
      self.assertEqual("L1", self.controller.l1_task["verification_level"])

  def test_write_or_ambiguous_request_creates_no_dispatch_and_requires_plan(self):
      receipt = workflow.run_team_call(
          "team call 为 README 增加安装示例", repository_root=self.repo,
          state_root=self.root, controller=self.controller,
      )
      self.assertEqual("PLAN_REQUIRED", receipt.disposition)
      self.assertEqual(0, self.controller.execution_count)
      self.assertEqual(0, self.controller.dispatch_count)
  ```

  Include CLI stdout/exit-code checks, missing repository failures, a direct
  L1 result whose changed files are nonempty (must be `BLOCKED`), an L1 role
  mismatch, no `--allow-live-model` live rejection, and simultaneous duplicate
  calls that still produce one execution.

- [ ] **Step 2: Run focused tests to establish RED**

  Run:

  ```bash
  /Users/lee/.local/bin/python3.11 -m unittest -v \
    tests.test_ai_workflow_team_call.TeamCallControllerTest \
    tests.test_ai_workflow.TeamCallCliTest
  ```

  Expected: public integration and `team-call` parser are absent. Confirm the
  failures are assertions/API gaps, not a real Codex launch.

- [ ] **Step 3: Implement controller integration with fail-closed handoff**

  Map `TeamCallError` to `WorkflowError` without changing existing error
  messages. Resolve `repository_root` once and require it to be a Git worktree
  for both direct paths. For L0, map the action to the fixed argv inside the
  controller and call `subprocess.run(..., shell=False, cwd=repository_root)`;
  write result digest/output metadata to `TEAM_CALL_ROUTED` and return it in
  the receipt.

  For L1, require the one parsed repo-relative evidence file to be regular,
  beneath the resolved root and not a symlink. Build a valid read-only
  `ai-task-1` PLAN task with `verification_level="L1"`, `allowed_write_paths=[]`,
  a generated unique task ID, and exact `authoritative_files=[path]`. Allocate
  the ID while the Team Call registry lock is held by scanning that day's
  existing `AWF-YYYYMMDD-NNN` directories and choosing the next numeric suffix.
  Persist it through `WorkflowStore.create_task`; invoke only the existing Luna
  contract through the injected controller. Validate the result through the
  existing result/role/evidence guards and block on any write claim, diff or
  identity mismatch. Do not create a Terra or Sol dispatch.

  For `PLAN_REQUIRED`, append `TEAM_CALL_ROUTED` with no execution result and
  a deterministic `task_id=None`; the conversation controller can then create
  the full task/route via existing explicit planning APIs. This preserves the
  invariant that a one-sentence, underspecified write request cannot mint an
  incomplete task envelope.

  Add the argparse subcommand. Its positional message must be passed through
  the exact same parser; it must emit canonical JSON receipt on success and
  exit 2 with the stable code on workflow error.

- [ ] **Step 4: Verify GREEN and run negative mutations**

  Run the Task 2 focused suite. In a disposable copy, change the L0 controller
  to call `shell=True`, and separately remove the L1 `allowed_write_paths == []`
  check; each corresponding test must fail. Restore code and run:

  ```bash
  /Users/lee/.local/bin/python3.11 -m unittest -v \
    tests.test_ai_workflow_team_call tests.test_ai_workflow
  /Users/lee/.local/bin/python3.11 -m compileall -q scripts tests
  git diff --check
  ```

- [ ] **Step 5: Commit and obtain independent Terra xhigh adversarial review**

  ```bash
  git add scripts/ai_workflow.py tests/test_ai_workflow.py tests/test_ai_workflow_team_call.py
  git commit -m "feat(workflow): route Team Call requests safely"
  ```

  A distinct Terra xhigh reviewer must verify that L0 cannot execute user
  commands, L1 cannot write or select a non-Luna role, `PLAN_REQUIRED` cannot
  launch/dispatch, and duplicate/concurrent calls run once. Review uses a
  copied repository and must not spend live model quota.

---

### Task 3: 发布契约、根/Plugin 镜像与文档

**Owner:** Luna Max — exact mirrored file and bounded documentation/test work.

**Files:**
- Create: `plugins/ai-workflow/runtime/ai_workflow_team_call.py`
- Modify: `plugins/ai-workflow/scripts/verify.sh`
- Modify: `README.md`
- Modify: `plugins/ai-workflow/skills/orchestration/SKILL.md`
- Modify: `tests/test_ai_workflow_distribution.py`

**Interfaces:**
- Plugin verifier compares `scripts/ai_workflow_team_call.py` and its runtime
  copy byte-for-byte.
- Published instructions list exact directive syntax, direct L0/L1 versus
  `PLAN_REQUIRED`, default serial operation, and unchanged role/acceptance
  boundaries.

- [ ] **Step 1: Write failing publication and copied-release tests**

  Extend `DistributionContractTest` with:

  ```python
  def test_team_call_runtime_copy_and_published_contract_are_exact(self):
      self.assertEqual(
          (ROOT / "scripts/ai_workflow_team_call.py").read_bytes(),
          (PLUGIN / "runtime/ai_workflow_team_call.py").read_bytes(),
      )
      published = "\n".join((ROOT / "README.md").read_text().casefold(),
                              (PLUGIN / "skills/orchestration/SKILL.md").read_text().casefold())
      self.assertIn("team call", published)
      self.assertIn("plan_required", published)
      self.assertIn("不自动合并", published)

  def test_plugin_verifier_rejects_tampered_team_call_runtime_copy(self):
      with tempfile.TemporaryDirectory() as temporary:
          release_root = Path(temporary) / "release"
          shutil.copytree(ROOT / ".codex", release_root / ".codex")
          shutil.copytree(ROOT / "config", release_root / "config")
          shutil.copytree(ROOT / "scripts", release_root / "scripts")
          shutil.copytree(PLUGIN, release_root / "plugins" / "ai-workflow")
          tampered = release_root / "plugins" / "ai-workflow" / "runtime" / "ai_workflow_team_call.py"
          tampered.write_text(tampered.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
          result = subprocess.run(
              ["sh", str(release_root / "plugins" / "ai-workflow" / "scripts" / "verify.sh")],
              cwd=release_root, text=True, capture_output=True, check=False,
          )
          self.assertNotEqual(0, result.returncode)
  ```

  Add language assertions that `team call` does not grant Luna review/approval,
  does not remove existing L0/L1/owner-gate language, and does not promise
  parallel agents.

- [ ] **Step 2: Run the focused distribution tests and confirm RED**

  Run:

  ```bash
  /Users/lee/.local/bin/python3.11 -m unittest -v \
    tests.test_ai_workflow_distribution.DistributionContractTest
  ```

  Expected: missing mirror and published team-call contract; do not weaken old
  Luna Max lifecycle assertions.

- [ ] **Step 3: Synchronize the release surface**

  Copy the root module byte-for-byte to Plugin runtime. Add
  `ai_workflow_team_call.py` to the verifier's runtime list. In README and
  Skill, document only the approved grammar/examples, table of the four
  dispositions, one-active-worker default, L0 controller/no model, L1 Luna
  read-only, plan fallback, and preserved review/acceptance/merge/push limits.
  Do not edit routing, repair, cost, lifecycle or Agent template semantics.

- [ ] **Step 4: Verify GREEN, release tamper rejection and complete suite**

  Run the focused distribution suite, then a copied-release clean/tampered
  verifier pair. The clean copy must exit 0; changing only the Plugin
  `ai_workflow_team_call.py` must exit nonzero. Then run:

  ```bash
  /Users/lee/.local/bin/python3.11 -m unittest discover -s tests -v
  /Users/lee/.local/bin/python3.11 -m compileall -q config scripts tests plugins/ai-workflow/runtime plugins/ai-workflow/scripts
  sh plugins/ai-workflow/scripts/verify.sh
  python3 /Users/lee/.codex/plugins/cache/openai-bundled/sites/0.1.34/skills/plugin-creator/scripts/validate_plugin.py plugins/ai-workflow
  python3 /Users/lee/.codex/plugins/cache/openai-bundled/sites/0.1.34/skills/skill-creator/scripts/quick_validate.py plugins/ai-workflow/skills/orchestration
  for file in plugins/ai-workflow/scripts/*.sh; do sh -n "$file"; done
  git diff --check
  ```

- [ ] **Step 5: Commit and obtain independent Terra xhigh adversarial review**

  ```bash
  git add README.md plugins/ai-workflow/runtime/ai_workflow_team_call.py \
    plugins/ai-workflow/scripts/verify.sh \
    plugins/ai-workflow/skills/orchestration/SKILL.md \
    tests/test_ai_workflow_distribution.py
  git commit -m "docs(workflow): publish Team Call contract"
  ```

  A distinct Terra xhigh reviewer must mutate the copied release mirror and
  published wording, confirm the verifier/tests reject both, and inspect that
  no role/acceptance authority has widened.

---

### Task 4: Whole-project final acceptance

**Owner:** Sol medium — read-only final whole-project adversarial acceptance.

**Files:**
- Create (ignored evidence): `.superpowers/sdd/2026-08-13-team-call/task-4-report.md`
- Modify (ignored evidence): `.superpowers/sdd/2026-08-13-team-call/progress.md`

- [ ] **Step 1: Bind the candidate and review packages**

  Record base/candidate SHAs, confirm every construction task has a separate
  Terra xhigh adversarial verdict, and inspect the cumulative diff only. Reject
  any unreviewed production modification or merge/push.

- [ ] **Step 2: Independently reproduce the positive and negative path matrix**

  On a copied repository/state root, test: valid L0 run; quoted/non-prefix
  non-trigger; empty directive; metacharacter input; L1 traversal/symlink/write
  result/role mismatch; write and ambiguous plan fallback; replay/identity
  drift; concurrent duplicate; root/Plugin mirror drift; and published role
  wording mutation. Do not call a live model.

- [ ] **Step 3: Run final technical gates**

  Run the full unittest discovery, compileall, Plugin verifier, Plugin and
  Skill validators, shell syntax checks, `git diff --check BASE..HEAD`, and
  clean status. Read all output and record exact counts/exit codes.

- [ ] **Step 4: Record ACCEPT, REWORK or BLOCKED without self-repair**

  Write a concise evidence report. On Critical/Important findings, return
  `REWORK` to the controller; do not repair inside final acceptance. On
  `ACCEPT`, report that the branch is ready for the user's integration choice.

## Execution controls

- Exactly one construction owner is active at a time. Do not create broad
  parallel agent batches.
- Each task gets one independent Terra xhigh adversarial review. A first review
  failure returns to the owner; a second review failure uses the existing
  Sol-medium fallback plus a distinct Sol-medium peer; only then may the
  established Sol-xhigh terminal repair occur without task-level review.
- Sol medium performs only Task 4 final whole-project acceptance.
- Every review must use a fresh agent context and state exact test evidence,
  scope, residual risks and verdict. It must actively seek counterexamples;
  passing focused tests alone is not acceptance.
