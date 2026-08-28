# Sol xhigh 裁定采纳实施计划（Sol medium 第四次复审修订版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以「宿主确定性生成、运行时强制校验」的修改版形态落地 Sol xhigh 裁定的 P0 四项与 P1 三项，全程 fail-closed，`effective_route` 保持 `UNCHANGED`，不回退到模型写路由、子模型握手或宣布成本赢家。第四版已逐条闭合第三次复审的 14 条施工前必改清单；本版逐条闭合第四次复审的 5 条施工前必改清单（① 授权/消费记录 ID exclude 按类拆分、② violation 查询 `_locked` 接口、③ violation 以 `events.jsonl` 事件为唯一权威持久来源、④ 声明恢复的唯一原始字节读取者、⑤ 汇点释放守卫 helper 与 AST 范围断言），已 CLOSED 项不回退、卡号不变。

**Architecture:** 宿主内核原语（canonical JSON、append/read JSONL、`write_json_once`、`WorkflowError`、`content_id`/`verify_content_id`、`TaskStoreProtocol`、`PROCESS_GENERATION`）全部落在叶子模块 `ai_workflow_artifacts.py`；新业务模块只经 `TaskStoreProtocol` 声明的 store 方法做 I/O，运行时禁止 import `ai_workflow`/`ai_workflow_repairs`，由 AST import-graph 测试锁定。所有新信息走独立 sidecar（`ai-route-declaration-1` / `ai-candidate-state-1` / `ai-final-verdict-1` / `ai-ownership-registry-1` / `ai-owner-authorization-1` / `ai-rate-snapshot-1` / `ai-preflight-record-1` / `ai-runtime-evidence-2`）与 append-only 账本（`final-verdicts.jsonl` / `owner-authorizations.jsonl` / `side-effects.jsonl` / `dispatch-permits.jsonl` / `preflight-records.jsonl` / `runtime-evidence-v2.jsonl`）；冻结 schema 与 `adversarial-acceptance-1` 账本事件形状不动。所有能启动模型的路径收敛到**两个最低模型执行汇点**：`run_codex` 顶部与 `run_assignment` 子进程启动前，许可 reservation、EXTERNAL 记录、ownership lease、启动认领在这两个汇点（及 fake 分支）的同一段 `store.lock` 临界区内以单事务完成；早失败层只做只读预检，不写账本。编排层 `ai_workflow_dispatch_policy.py` 单向依赖声明、预检、所有权、副作用模块，无循环。回归基线由 **Task 00** 在任何生产卡之前固定。成本估算只在 router probe 研究面以逐臂分型结果对象输出；身份前置仅以隔离实验原型存在，不接生产链。

**Tech Stack:** Python 3.11 标准库，POSIX shell，unittest。

**Spec:** `docs/superpowers/specs/2026-08-28-sol-review-adoption-design.md`

## Global Constraints

- 冻结面：`ai-task-1`、`ai-result-1`、`ai-route-decision-1`（`scripts/ai_workflow_artifacts.py` 的 `ROUTE_DECISION_FIELDS` 九字段）与 `scripts/ai_workflow_repairs.py` 的 `_ACCEPTANCE_LEDGER_VERSION = "adversarial-acceptance-1"` 账本事件形状不得变更；`scripts/ai_workflow.py` 的 `OWNER_DECISIONS` 闭集（`approve_execution`/`authorize_rework`/`authorize_escalation`/`defer`/`close`/`abort`）不得增删。
- 禁止模型写路由声明、子模型自然语言握手进生产、宣布成本赢家；成本输出只有 `CACHE_MECHANISM_CANDIDATE_*` 与逐臂 `COST_ESTIMATE_UNDER_SNAPSHOT` 结果对象两个分层，过期/缺字段只有 `PRICE_STALE` / `PRICE_UNKNOWN`，无权威 usage 的臂只有 `USAGE_AUTHORITY_UNAVAILABLE`，部分权威时总计只有 `COST_TOTAL_UNAVAILABLE` 且不携带数值。
- 所有门控 fail-closed；禁止「缺声明自动补写（派发时）」「预检替代 `verify_runtime_identity`」「快照追溯改价」「调用者自报副作用/身份」「UTC 墙钟比较声明与派发先后」「部分权威总计冒充全路线成本」「缺观测字段当作没有 fork」「安全接口接收调用者构造的 baseline/current/PreflightContext」「历史 lease 为后续 dispatch 豁免路径」等便捷路径。
- 模块依赖方向按设计文档「模块依赖方向」节执行：新业务模块（declarations/candidate_state/authorizations/verdicts/ownership/side_effects/preflight/dispatch_policy）运行时（模块级与函数级）不得 import `ai_workflow`、`ai_workflow_repairs` 或 `sync_plugin`；store I/O 只经 `TaskStoreProtocol` 方法；`tests/test_ai_workflow_import_graph.py` 从 Task 01 起锁定该约束，后续每卡验收必跑。
- 锁纪律：`WorkflowStore.lock` 非重入（`LOCK_EX | LOCK_NB`，已持锁再获取必然 `TASK_ALREADY_RUNNING`）；凡可能在任务锁内被调用的新函数必须提供 `*_locked` 变体且第一行 `store._assert_lock_held(task_id)`；自取锁包装内除 `with store.lock(...)` 与委派外不得有任何逻辑；已持锁路径调用自取锁包装必然失败（负向测试锁定）；**两个最低模型执行汇点的 `with store.lock(...)` 语法范围内禁止调用自取锁版本**（AST 范围断言，冻结包装名清单）；授权校验、消费记录与被授权动作同处一段 `store.lock` 临界区。violation 查询同规：`has_unresolved_ownership_violation_locked` 第一行 `_assert_lock_held`，同名包装仅取锁委派；`require_dispatch_permit_locked` **只能**调用 `_locked` 版本；对 `require_dispatch_permit_locked` 与 `require_write_ownership_locked` 的**完整传递调用图**做 AST 检查（自取锁包装按「函数体仅含 `with store.lock(...)` 与委派」结构特征自动识别），持锁路径不得进入任何自取锁 wrapper。spawn 前失败的锁外释放只经 `release_permit_if_never_spawned` helper（仅 `spawned=False` 时调自取锁包装 `release_permit_before_start`）。
- 内容寻址 ID 的 exclude 按 record 类拆分：authorization 用 `AUTHORIZATION_ID_EXCLUDE = frozenset({"authorization_id"})`；consumption/transfer_lease 用 `RECORD_ID_EXCLUDE = frozenset({"record_id"})`——`authorization_id` **必须进入** `record_id` preimage（只改 `authorization_id` 的负向 golden 必然 `CONTENT_ID_MISMATCH`）；禁止共用跨类 exclude 常量；每类 compute/verify 共用同一模块私有 canonical projection（集合语义字段排序去重），禁止 compute 规范化而 verify 哈希原始列表；wire 上不适用的另一类 ID 字段**强制不存在**（不是 null/空串）。
- ownership violation 持久化：`events.jsonl` 的 `OWNERSHIP_VIOLATION_RECORDED` 事件是唯一权威来源（字段闭集冻结，见 Task 09）；`side-effects.jsonl` 的 `EFFECT_KINDS` 闭集不含、也永不加入 violation 值；实际写副作用仍按原 effect kind 记录；violation 查询只重放该权威来源，坏记录/跨任务/无法重放一律 fail-closed。
- 许可状态机：`dispatch-permits.jsonl` 的 `state` ∈ `{"RESERVED", "STARTED", "RELEASED_BEFORE_START"}`；合法转换仅 `∅→RESERVED`、`RESERVED→STARTED`、`RESERVED→RELEASED_BEFORE_START`；`STARTED`/`RELEASED_BEFORE_START` 为终态，同 ID 任何后续记录或再进 `require_dispatch_permit[_locked]` 一律拒绝（无幂等返回窗口）；spawn 标记置位后永不释放。技术重试 = 新 `attempt_id` = 新 `dispatch_id`。
- JSONL 完整性：仅 `dispatch-permits.jsonl` 带 `seq`（任务内从 1 连续；重放验证重复/断档/状态机）；其余新账本无 seq，完整性由内容 ID 唯一性、`transfer_lease` 局部 `dispatch_seq` 连续性、行序与 `event_index` 引用完整性承担；任何卡不得为无 seq 账本声称「重复 seq」检查。
- 新 schema 进 `scripts/sync_plugin.py` 的 `CONFIG_FILES`；被生产 workflow 导入的新模块进 `RUNTIME_FILES`；`ai_workflow_identity_probe.py`、`ai_workflow_evidence_chain.py`、`scripts/collect_test_baseline.py` 与 `ai_workflow_router_probe.py` 一样不进 `RUNTIME_FILES`。`plugins/ai-workflow/` 下的镜像只经 `python3.11 scripts/sync_plugin.py --write` 生成，禁止手改。
- 纯标准库，不引入新依赖。
- 回归基线：**Task 00 先于一切生产卡**，从固定 base commit 生成并提交 `tests/baseline_manifest.json`（全部测试 ID + 结果 + skip 原因 + 采集命令 + base commit）；此后每卡验收要求基线清单内用例全部保持通过、skip 语义不变，新增测试全绿。收口卡（Task 20）只复核 manifest，不得首次生成。禁止用固定总数做判据。
- 每卡在独立 worktree 分支施工；提交信息用 Conventional Commits。
- P2（代理预算闭集、owner 迁移工具、事务化安装与前向 CI、质量调整成本模型、来源与许可证核查）与 P3（角色/模型解耦 ADR）只保留在设计文档 backlog，本计划不出卡。

## 卡片索引与依赖

| 卡 | 内容 | 依赖 |
|---|---|---|
| 00 | 回归基线 manifest 先行（固定 base commit） | 无 |
| 01 | 宿主内核叶子化：artifacts 原语、TaskStoreProtocol、store I/O 方法、import-graph 测试 | 00 |
| 02 | P0-1a 路由声明 schema、writer、唯一创建阶段、崩溃恢复挂权威入口 | 01 |
| 03 | P0-2a CandidateState 与 digest 规范（权威 root、manifest 双采、pathspec 排除） | 01 |
| 04 | P0-2b 终验裁决不可变历史、canonical preimage、签发者角色证据派生、新鲜度评估 | 03 |
| 05 | P0-2c 终验放行出口全量接门（FRESH ACCEPT 语义、门内重放 baseline 权威重算、锁内消费） | 04、08 |
| 06 | P0-3a 所有权登记与副作用账本（控制面分离） | 01 |
| 07 | P0-3b 副作用真实观测挂钩（COMMAND_PRODUCERS 闭集、construction 冻结步骤 producer、EFFECTFUL_ROLES） | 03、06 |
| 08 | P0-3c 版本化 owner 授权 sidecar（分类 exclude、共用投影、golden preimage、`_locked` 变体、lease 记录） | 03 |
| 09 | P0-3d 所有权转让门：scoped lease 原子扣减、permit 绑定实际路径复核、事件型持久 violation 与 `_locked` 查询 | 06、07、08 |
| 10 | P0-4a 费率快照工件与归档链（RATE_UNITS 闭集与基数） | 00 |
| 11 | P0-4b 探针成本逐臂分型报告（权威 usage、localcontext Decimal、minor-unit int、总计规则） | 10 |
| 12 | P0-1b dispatch_policy 编排层：许可状态机单事务原语、dispatch_id 永久退休、violation 阻断、EXTERNAL 接线、释放守卫 helper | 02、07、09、14 |
| 13 | P0-1c 派发门控全路径接线与 legacy 规则（两个执行汇点单事务、spawn 证明、认领与释放语义） | 07、09、12 |
| 14 | P1-1a 按路由预检与任务内多键缓存（PreflightContext 全权威重算、安全入口内部重算） | 02 |
| 15 | P1-1b 预检生产接入与升级补预检 | 13、14 |
| 16 | P1-2a 身份前置探针契约（双钥匙入接口、权威每调用输出上限） | 00 |
| 17 | P1-2b 身份探针 runner 与 A/B 报告（唯一 runner、逐次预算 reservation） | 16 |
| 18 | P1-3a 证据链事件生产者（launch intent、ai-runtime-evidence-2、fork/nested 枚举） | 02、13、14 |
| 19 | P1-3b 证据链读取器与只读审计 CLI | 04、18 |
| 20 | 收口：baseline 复核、wire golden、负向回归、文档 | 00–19 |

可并行批次：批次零 00；批次一 01/10/16；批次二 02/03/06/11/17；批次三 04/07/08/14；批次四 05/09；批次五 12；批次六 13；批次七 18；批次八 15/19；批次九 20。

文件冲突纪律（必须按序合入，禁止并行改同一文件）：`scripts/ai_workflow.py` 的改动顺序固定为 01 → 07 → 13 → 18 → 15；`scripts/ai_workflow_repairs.py` 的改动顺序固定为 07 → 05 → 13 → 18；`scripts/sync_plugin.py` 每张涉及卡独占合入窗口。卡 12 必须等 07、09、14 全部合入后开工（其接口真实消费 Task 09 的 `has_unresolved_ownership_violation_locked` 与 Task 07 的 `derive_effectful_roles`/`record_external_side_effect_locked`，禁止与 09 并行）；卡 13 必须等 07、09、12 全部合入后开工。

---

### Task 00: 回归基线 manifest 先行

**依赖:** 无（必须第一个合入，所有后续卡的验收都引用它）

**分支:**

```bash
BASE_COMMIT=$(git rev-parse HEAD)
git worktree add ../wt-sol-adopt-00-baseline -b feat/sol-adopt-00-baseline "$BASE_COMMIT"
cd ../wt-sol-adopt-00-baseline
```

**Files:**

- Create: `scripts/collect_test_baseline.py`（只进仓库，不进 `RUNTIME_FILES`）
- Create: `tests/baseline_manifest.json`
- Create: `tests/test_ai_workflow_baseline_manifest.py`
- 无生产代码改动。

**Interfaces:**

- Produces:
  - `tests/baseline_manifest.json`，schema_version `ai-test-baseline-1`，字段闭集：`schema_version`、`base_commit`、`captured_with`（完整采集命令行）、`captured_at_utc`、`tests`（每条 `{id, outcome, skip_reason}`；`outcome` ∈ `{"pass", "skip"}`，`skip_reason` 仅 skip 时非空）
  - `scripts/collect_test_baseline.py`: `main(argv: list[str] | None = None) -> int`——用 stdlib `unittest` runner 挂自定义 `TestResult` 跑 `discover -s tests`，逐条记录 test id、结果与 skip 原因，写 manifest；不得 import 任何被测生产模块以外的第三方包
  - `tests/test_ai_workflow_baseline_manifest.py` 三个测试：
    - `test_manifest_ids_all_present`：`unittest.TestLoader().discover` 枚举当前全部测试 ID（只加载不运行），manifest 中每个 ID 必须仍存在（防删测/改 ID）；新增测试允许存在但必须通过全量验收约束
    - `test_skip_semantics_unchanged`：不运行用例，直接检查加载后测试对象上的 `__unittest_skip__` / `__unittest_skip_why__` 标记，manifest 中每条 skip 记录的用例仍被静态 skip 且原因逐字符一致；manifest 中 `outcome == "pass"` 的用例不得新出现静态 skip 标记
    - `test_manifest_shape`：字段闭集、`base_commit` 为 40 位十六进制、`tests` 按 `id` 排序且无重复

- [ ] **Step 1: 写 checker 的失败测试**

`tests/test_ai_workflow_baseline_manifest.py` 按上述三测试写好；此时
`tests/baseline_manifest.json` 不存在，`test_manifest_shape` 失败。

- [ ] **Step 2: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
```

Expected: `test_manifest_shape` 失败（manifest 尚不存在）。

- [ ] **Step 3: 实现采集器并生成 manifest**

写 `scripts/collect_test_baseline.py`；在 base commit 干净工作树上运行：

```bash
python3.11 scripts/collect_test_baseline.py --output tests/baseline_manifest.json
```

Expected: manifest 落盘，`base_commit` 等于分支起点 SHA，`captured_with`
记录完整命令行；当前全量测试零失败（若有红测，停止并上报，不得继续）。

- [ ] **Step 4: Verify GREEN**

```bash
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
```

Expected: 三个测试全部通过。

- [ ] **Step 5: 提交**

```bash
git add -A && git commit -m "test(baseline): pin pre-construction test manifest from fixed base commit"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
python3.11 -m unittest discover -s tests
```

Expected: 退出码均 0；manifest 已提交且 `base_commit` 可追溯；后续每卡
的验收都必须包含第一条命令。

---

### Task 01: 宿主内核叶子化（artifacts 原语、TaskStoreProtocol、store I/O 方法、import-graph 测试）

**依赖:** 00

**分支:**

```bash
git worktree add ../wt-sol-adopt-01-host-kernel -b feat/sol-adopt-01-host-kernel
cd ../wt-sol-adopt-01-host-kernel
```

**Files:**

- Modify: `scripts/ai_workflow_artifacts.py`（迁入内核原语，新增 Protocol 与 content ID 原语）
- Modify: `scripts/ai_workflow.py`（删除已迁出的定义，改为从 artifacts 回导；`WorkflowStore` 新增四个方法 + 锁内注册表）
- Modify: `scripts/ai_workflow_routing.py`（删除 `_write_json_once` 惰性 seam，改模块级 import artifacts）
- Create: `tests/test_ai_workflow_host_kernel.py`
- Create: `tests/test_ai_workflow_import_graph.py`
- Modify: `tests/test_ai_workflow_distribution.py`
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/runtime/ai_workflow_artifacts.py`、`plugins/ai-workflow/runtime/ai_workflow.py`、`plugins/ai-workflow/runtime/ai_workflow_routing.py`

**Interfaces:**

- `scripts/ai_workflow_artifacts.py` 新增（全部从 `scripts/ai_workflow.py`
  迁入或新建，迁入后 ai_workflow.py 经既有双模式 import 块回导，
  `scripts/ai_workflow.py:160` 同款 try/except ImportError 模式）：
  - `class WorkflowError(RuntimeError)`（从 `scripts/ai_workflow.py:152`
    原样迁入；`ai_workflow.WorkflowError` 须继续可 import，行为不变）
  - `def canonical_json(value: object) -> str`（从
    `scripts/ai_workflow.py:2135` 的 `_canonical_json` 迁入；
    `ai_workflow._canonical_json` 保留为别名，既有调用点零改动）
  - `def append_jsonl(path: Path, record: Mapping[str, object]) -> None`
    （从 `scripts/ai_workflow.py:2374` 原样迁入）
  - `def write_json_once(path: Path, value: object, *, conflict_code: str) -> str`（从 `scripts/ai_workflow.py:2270` 原样迁入）
  - `def read_jsonl(path: Path, *, code: str) -> tuple[dict[str, object], ...]`
    —— fail-closed 读取：文件不存在返回空元组；末行无换行符（截断尾
    记录）、任一行 JSON 解析失败、任一行非对象、非法 UTF-8，一律抛
    `WorkflowError(f"{code}_CORRUPT", ...)`；不得跳过坏行继续
  - `def content_id(kind: str, fields: Mapping[str, object], *, exclude: frozenset[str]) -> str`
    —— canonical preimage：`{"kind": kind, "fields": <去掉 exclude 键
    后的投影>}` 经 `canonical_json` 序列化后 sha256；`kind` 必须匹配
    `r"^[a-z0-9-]+$"`；`exclude` 非空且必须含该类的 ID 字段；集合语义
    字段由调用方先排序去重（提供
    `def sorted_strs(values: object) -> list[str]` 辅助，非字符串元素
    即拒绝）
  - `def verify_content_id(kind: str, record: Mapping[str, object], *, exclude: frozenset[str], id_field: str) -> None`
    —— **与生成接受完全相同的 exclude 集**：按 exclude 投影重算
    `content_id` 并与 `record[id_field]` 比对，不符即 `WorkflowError(
    "CONTENT_ID_MISMATCH", ...)`；`id_field ∉ exclude` 或
    `id_field` 缺失即拒绝。写、读、重放三路共用此函数；任何「生成
    排除两个字段、验证只排除一个字段」的调用组合在类型层面不存在
    （exclude 是单一必填参数）。各 record 类的具体 exclude 常量由
    各自模块冻结（**按类拆分，每类只排除自身 ID 字段**，见设计
    「内容寻址 ID」节与各业务卡）
  - `class TaskStoreProtocol(Protocol)`（`typing.Protocol`，仅类型注解，
    运行时不实例化）：`lock(task_id: str) -> ContextManager[None]`、
    `_require_task(task_id: str) -> Path`、`append_event(task_id: str,
    event: dict) -> None`、`write_task_artifact_once(task_id: str,
    name: str, value: Mapping[str, object], *, conflict_code: str) -> Path`、
    `append_task_ledger(task_id: str, name: str, record: Mapping[str,
    object]) -> None`、`read_task_ledger(task_id: str, name: str) ->
    tuple[dict[str, object], ...]`、`_assert_lock_held(task_id: str) -> None`
  - `PROCESS_GENERATION: str`（模块 import 时 `uuid.uuid4().hex` 生成
    一次的进程级常量）
- `scripts/ai_workflow.py` 的 `WorkflowStore` 新增：
  - `write_task_artifact_once(task_id, name, value, *, conflict_code) -> Path`
    —— `_require_task` 后对 `task_dir / name` 调 `write_json_once`
  - `append_task_ledger(task_id, name, record) -> None` —— 对
    `task_dir / name` 调 `append_jsonl`；`name` 必须匹配
    `r"^[a-z0-9-]+\.jsonl$"`
  - `read_task_ledger(task_id, name) -> tuple[dict[str, object], ...]`
    —— 调 `read_jsonl(path, code=name 派生的大写错误码)`
  - `_assert_lock_held(task_id) -> None` —— 锁内注册表（`threading.local`
    持有 task_id 集合，`lock()` 的 yield 前后登记/注销）中无此任务即
    抛 `WorkflowError("LOCK_REQUIRED", ...)`
- `scripts/ai_workflow_routing.py`：删除 `_write_json_once`
  （`scripts/ai_workflow_routing.py:449-461`）及其对 `ai_workflow` 的
  函数级 import；两处调用点（`:495`、`:1057`）改为直接调用从
  artifacts 模块级 import 的 `write_json_once`
- `tests/test_ai_workflow_import_graph.py`：
  - `IMPORT_GRAPH_ALLOWED: Mapping[str, frozenset[str]]`——以字典字面量
    声明设计文档的允许边（含 ai_workflow、repairs、scheduler、
    team_call 等既有模块的既有边，按仓内现状采集后冻结；含
    `ownership → authorizations`（Task 09 起）与
    `repairs → dispatch_policy`（Task 13 起）两条计划新增边的落位
    行，模块尚不存在时扫描容忍缺失）
  - AST 扫描 `scripts/ai_workflow*.py` 全部模块级与函数级 import，
    断言：(a) 新业务模块集合 `{ai_workflow_declarations,
    ai_workflow_candidate_state, ai_workflow_authorizations,
    ai_workflow_verdicts, ai_workflow_ownership, ai_workflow_side_effects,
    ai_workflow_preflight, ai_workflow_dispatch_policy}`（当前尚不存在，
    扫描须容忍缺失）到 `{ai_workflow, ai_workflow_repairs, sync_plugin}`
    的边为空；(b) 全部 ai_workflow* 模块间 import 图无环；(c) 扫描覆盖
    函数体内的 `import`/`from ... import` 节点

- [ ] **Step 1: 写内核原语的失败测试**

`tests/test_ai_workflow_host_kernel.py`：`canonical_json` 键序无关、
UTF-8 直出（`ensure_ascii=False`）、紧凑分隔符；`read_jsonl` 对截断尾
记录（末行无 `\n`）、坏 UTF-8、非对象行、缺失文件四路行为；
`content_id`：改任一非排除字段即变、排除字段变化不影响结果、键序
不影响结果；`verify_content_id` 正误两例 + **exclude 一致性两例**
（生成用 `exclude={"a","b"}`、验证用 `exclude={"a"}` 的错配组合必然
`CONTENT_ID_MISMATCH`——证明投影不一致无法通过验证；`id_field` 不在
exclude 内即拒绝）；`sorted_strs` 拒非字符串。

- [ ] **Step 2: 写 store 方法与锁纪律的失败测试**

`write_task_artifact_once` 冲突时抛给定 `conflict_code`；
`append_task_ledger`/`read_task_ledger` round-trip；非法 `name` 拒绝；
`_assert_lock_held` 在锁外抛 `LOCK_REQUIRED`、锁内通过；嵌套
`store.lock(task_id)` 抛 `TASK_ALREADY_RUNNING`（既有行为钉板）。

- [ ] **Step 3: 写 import-graph 的失败测试**

`tests/test_ai_workflow_import_graph.py`：断言 `ai_workflow_routing`
不再函数级 import `ai_workflow`（当前存在，测试先红）；断言无环；
断言允许边表覆盖全部现存边（采集现状冻结进表）。

- [ ] **Step 4: Verify RED**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_host_kernel \
  tests.test_ai_workflow_import_graph -v
```

Expected: 新测试失败（原语尚未迁入/新建）。

- [ ] **Step 5: 最小实现**

按 Interfaces 迁移与新增；`ai_workflow.py` 内 `_canonical_json =
canonical_json` 别名保持既有调用点不动；全仓 `from ai_workflow import
write_json_once` / `append_jsonl` / `WorkflowError` 的既有 import 路径
必须继续可用（re-export 测试锁定）。

- [ ] **Step 6: Verify GREEN + 全量回归 + 基线复核**

```bash
python3.11 -m unittest discover -s tests
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
```

Expected: 基线清单用例保持通过、skip 语义不变；新增全绿。

- [ ] **Step 7: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
python3.11 -m unittest tests.test_ai_workflow_distribution -v
git add -A && git commit -m "refactor(kernel): move host io primitives into artifacts leaf with store protocol"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_host_kernel tests.test_ai_workflow_import_graph tests.test_ai_workflow_baseline_manifest tests.test_ai_workflow_distribution -v
python3.11 -m unittest discover -s tests
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；import-graph 无环；输出 `PLUGIN_SYNC_OK`。

---

### Task 02: P0-1a 路由声明 schema、writer、唯一创建阶段、崩溃恢复挂权威入口

**依赖:** 01

**分支:**

```bash
git worktree add ../wt-sol-adopt-02-route-declaration -b feat/sol-adopt-02-route-declaration
cd ../wt-sol-adopt-02-route-declaration
```

**Files:**

- Create: `config/ai_workflow_route_declaration.schema.json`
- Create: `scripts/ai_workflow_declarations.py`
- Create: `tests/test_ai_workflow_declarations.py`
- Modify: `scripts/sync_plugin.py`（`CONFIG_FILES` 增 `ai_workflow_route_declaration.schema.json`；`RUNTIME_FILES` 增 `ai_workflow_declarations.py`）
- Modify: `tests/test_ai_workflow_distribution.py`
- Modify: `tests/test_ai_workflow_import_graph.py`（允许边表增 declarations 行）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/config/ai_workflow_route_declaration.schema.json`、`plugins/ai-workflow/runtime/ai_workflow_declarations.py`

**Interfaces:**

- Consumes:
  - `scripts/ai_workflow_routing.py`: `RuntimeRouteDecision`（属性 `task_sha256`、`route`、`rule_id`、`decided_at_utc`、`roles`、`effective_roles`）
  - `scripts/ai_workflow_artifacts.py`: `ROUTE_DECISION_FIELDS`、`artifact_sha256(value: Mapping[str, object]) -> str`、`load_artifact(path: Path) -> dict[str, object]`、`validate_route_decision(value: object) -> None`、`WorkflowError`、`TaskStoreProtocol`、`canonical_json`
- Produces:
  - `ROUTE_DECLARATION_SCHEMA_VERSION = "ai-route-declaration-1"`
  - `ROUTER_VERSION = "deterministic-router-1"`
  - `ROUTE_DECLARATION_FIELDS: frozenset[str]`（`schema_version`、`task_id`、`envelope_hash`、`router_version`、`route_config_hash`、`selected_route`、`allowed_roles`、`active_roles`、`rule_ids`、`reason_codes`、`max_dispatches`、`allowed_transitions`、`declared_at_utc`）
  - `ROUTE_DECLARED_EVENT_TYPE = "ROUTE_DECLARED"`
  - `@dataclass(frozen=True) class RouteDeclaration`，含 `to_dict() -> dict[str, object]`
  - `validate_route_declaration(value: Mapping[str, object]) -> None`
  - `compute_route_config_hash(config: Mapping[str, object]) -> str`（键排序、闭集字段规范化后 sha256）
  - `build_route_declaration(*, decision: RuntimeRouteDecision, route_config_hash: str, allowed_roles: tuple[str, ...], active_roles: tuple[str, ...], rule_ids: tuple[str, ...], reason_codes: tuple[str, ...], max_dispatches: int, allowed_transitions: tuple[Mapping[str, str], ...]) -> RouteDeclaration` —— `envelope_hash` 从 `decision.task_sha256` 派生、`declared_at_utc` 从 `decision.decided_at_utc` 派生，**不设**调用者传参；`active_roles` 必须 ⊆ `allowed_roles`
  - `_read_route_declaration_bytes(store: TaskStoreProtocol, task_id: str) -> bytes | None` —— **模块私有**（下划线前缀），declarations 模块内**唯一**对 `route-declaration.json` 做原始读取的函数：经 `store._require_task(task_id)` 定位任务目录并读取文件字节；文件缺失返回 `None`；不做解析、不做事件 I/O。**允许读取点 = `{recover_route_declaration_event, load_route_declaration_locked}`，二者都必须经本 helper**
  - `recover_route_declaration_event(store: TaskStoreProtocol, task_id: str) -> bool` —— 第一行 `store._assert_lock_held(task_id)`；经 `_read_route_declaration_bytes` 读取既有文件字节：文件存在且 `events.jsonl` 无 `ROUTE_DECLARED` → 从**既有文件字节**计算哈希派生事件内容并补记（**不改写声明文件**），返回 True；事件存在而文件缺失 → `ROUTE_DECLARATION_CORRUPT`；两者俱在或俱无 → 返回 False；补记事件写失败（I/O 错误）→ 原样抛 `WorkflowError`，调用方不得继续派发
  - `load_route_declaration_locked(store: TaskStoreProtocol, task_id: str) -> RouteDeclaration | None` —— 第一行 `store._assert_lock_held(task_id)`；**第一条 I/O 语句即调 `recover_route_declaration_event`**（先恢复后加载），恢复抛出即中断；随后经同一 `_read_route_declaration_bytes` 读取并解析声明（helper 返回 `None` → 返回 `None`）。**静态扫描冻结**：模块内原始读取点唯一（helper 内），允许读取点 = {recover, load_locked} 且二者源码均调用该 helper；绕过恢复或绕过 helper 的直接加载在结构上不存在
  - `load_route_declaration(store: TaskStoreProtocol, task_id: str) -> RouteDeclaration | None` —— 自取锁包装：`with store.lock(task_id): return load_route_declaration_locked(...)`，除取锁与委派外无任何逻辑
  - `record_route_declaration(store: TaskStoreProtocol, task_id: str, declaration: RouteDeclaration) -> Path` —— 锁内（第一行 `_assert_lock_held`）重读 `task.json` 与 `route-decision.json` 强制信封等式；两派发账本（`dispatches.jsonl`、`dispatch-permits.jsonl`，经 `store.read_task_ledger`）任一非空 → `ROUTE_DECLARATION_LATE`；写序固定：先 `store.write_task_artifact_once(task_id, "route-declaration.json", ...)`（冲突码 `ROUTE_DECLARATION_CONFLICT`），后 `store.append_event` 追加 `ROUTE_DECLARED` 事件
  - `ensure_route_declaration(store: TaskStoreProtocol, task_id: str, declaration: RouteDeclaration) -> RouteDeclaration` —— 唯一创建阶段入口；第一行 `_assert_lock_held`；**先调 `load_route_declaration_locked`（即先恢复）**：返回既有声明则逐字节比对规范化 JSON，一致返回，不一致 `ROUTE_DECLARATION_CONFLICT`；返回 None 则走 `record_route_declaration`

模块纪律：`ai_workflow_declarations.py` 运行时不得 import
`ai_workflow`/`ai_workflow_repairs`/`sync_plugin`（import-graph 测试
锁定）；全部 I/O 经 `TaskStoreProtocol` 方法。

- [ ] **Step 1: 写声明 schema 与校验的失败测试**

覆盖：合法声明 round-trip；缺字段、多余字段、`schema_version` 错误、
`max_dispatches` 为负/非整数/布尔、`allowed_roles` 含未知角色或空列表、
`active_roles` 含不在 `allowed_roles` 中的角色、`allowed_transitions`
元素缺 `from_role`/`to_role` 或角色不在 `ROLES` 闭集，均 fail-closed。
`build_route_declaration` 的 `envelope_hash` 恒等于所给
`decision.task_sha256`（构造不同 decision 断言跟随变化），且接口上不
存在传入 `envelope_hash` 的参数（`inspect.signature` 内省断言）。

- [ ] **Step 2: 写 record/ensure/load 的失败测试**

覆盖：`record_route_declaration` 成功后写出 `route-declaration.json`
且 `events.jsonl` 追加一条 `ROUTE_DECLARED` 事件（`task_id`、
`envelope_hash`、`selected_route`、声明文件 sha256）；`task.json` 被
篡改（信封哈希不匹配）→ `ROUTE_DECLARATION_MISMATCH`；无
`route-decision.json` 或其 `task_sha256` 与声明信封不一致 →
`ROUTE_DECLARATION_MISMATCH`；`dispatches.jsonl` 已有记录 →
`ROUTE_DECLARATION_LATE`（顺序证据只用账本空态；静态扫描断言模块源码
不对 `declared_at_utc` 做任何比较运算）；`ensure_route_declaration`
对同一冻结 decision 重建的声明幂等返回（文件 mtime/内容不变）、对内容
漂移的声明 `ROUTE_DECLARATION_CONFLICT`；锁外调
`record_route_declaration`/`ensure_route_declaration`/
`load_route_declaration_locked` → `LOCK_REQUIRED`；
`load_route_declaration` 包装对缺失任务返回 `None`。

- [ ] **Step 3: 写崩溃窗口恢复挂权威入口的失败测试**

声明文件已落盘但事件未追加（手工删事件模拟崩溃）→
`load_route_declaration`（包装）与 `load_route_declaration_locked`
（锁内）都**先补记事件再返回声明**，声明文件字节不变（恢复前后
sha256 一致）；`ensure_route_declaration` 在同样的崩溃窗口下先恢复再
幂等返回；事件存在但文件被删 → 两个 load 入口与 ensure 均
`ROUTE_DECLARATION_CORRUPT`；两者俱在 → 不补记、事件流不增长；
锁外调 `recover_route_declaration_event` → `LOCK_REQUIRED`；补记事件
写入注入失败（monkeypatch `append_event` 抛错）→ load 向上抛出、不
返回声明（恢复失败不得继续）；静态扫描断言：declarations 模块源码中
对 `route-declaration.json` 的原始读取（`open(`/`.read_bytes()`/
`load_artifact(` 命中声明路径）**只出现在
`_read_route_declaration_bytes` 内**；
`recover_route_declaration_event` 与 `load_route_declaration_locked`
的源码均调用该 helper，且 `load_route_declaration_locked` 的
`recover_route_declaration_event` 调用先于 helper 读取——允许读取点
= {recover, load_locked}，**禁止**「除 load 外无人可读文件」式断言
（那会禁止 recover 履行自己的职责）。

- [ ] **Step 4: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_declarations -v
```

Expected: 全部新测试失败，模块尚不存在（ImportError）。

- [ ] **Step 5: 最小实现**

新建 `scripts/ai_workflow_declarations.py` 与
`config/ai_workflow_route_declaration.schema.json`；同步
`scripts/sync_plugin.py` 两份清单与 import-graph 允许边。

- [ ] **Step 6: Verify GREEN + 基线复核**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_declarations \
  tests.test_ai_workflow_distribution \
  tests.test_ai_workflow_import_graph \
  tests.test_ai_workflow_baseline_manifest -v
```

Expected: 全部通过。

- [ ] **Step 7: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(declarations): add ai-route-declaration-1 sidecar with recovery wired into all load entries"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_declarations tests.test_ai_workflow_distribution tests.test_ai_workflow_import_graph tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；恢复挂在 load/ensure 入口的测试证据齐备；后者
输出 `PLUGIN_SYNC_OK`。

---

### Task 03: P0-2a CandidateState 与 digest 规范（权威 root、manifest 双采、pathspec 排除）

**依赖:** 01

**分支:**

```bash
git worktree add ../wt-sol-adopt-03-candidate-state -b feat/sol-adopt-03-candidate-state
cd ../wt-sol-adopt-03-candidate-state
```

**Files:**

- Create: `config/ai_workflow_candidate_state.schema.json`
- Create: `scripts/ai_workflow_candidate_state.py`
- Create: `tests/test_ai_workflow_candidate_state.py`
- Modify: `scripts/sync_plugin.py`（`CONFIG_FILES` 增 `ai_workflow_candidate_state.schema.json`；`RUNTIME_FILES` 增 `ai_workflow_candidate_state.py`）
- Modify: `tests/test_ai_workflow_distribution.py`
- Modify: `tests/test_ai_workflow_import_graph.py`（允许边表增 candidate_state 行）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/config/ai_workflow_candidate_state.schema.json`、`plugins/ai-workflow/runtime/ai_workflow_candidate_state.py`

**Interfaces:**

- Consumes:
  - `scripts/ai_workflow_artifacts.py`: `artifact_sha256`、`load_artifact`、`canonical_json`、`WorkflowError`、`TaskStoreProtocol`
  - `scripts/ai_workflow_planning.py`: `normalize_scope(path: str) -> PurePosixPath`（`:50`）
- Produces:
  - `CANDIDATE_STATE_SCHEMA_VERSION = "ai-candidate-state-1"`
  - `CANDIDATE_STATE_FIELDS: frozenset[str]`（`schema_version`、`task_id`、`envelope_hash`、`candidate_commit`、`baseline_commit`、`tree_digest`、`diff_digest`、`runtime_evidence_ids`、`captured_at_utc`）
  - `@dataclass(frozen=True) class CandidateEntry`（`path`、`mode`、`kind`、`content_sha256`；`kind` ∈ `{"file", "link"}`）
  - `@dataclass(frozen=True) class CandidateState`，含 `to_dict() -> dict[str, object]` 与 `state_digest() -> str`（对 commit/baseline/tree/diff/排序去重后 evidence ids 的规范化 sha256；不含 `captured_at_utc`）
  - `validate_candidate_state(value: Mapping[str, object]) -> None`
  - `candidate_exclusions(repo: Path, state_root: Path) -> tuple[PurePosixPath, ...]`（固定排除 `.git`、任务状态根 `data/state/ai-workflow/`、runtime sessions 目录；解析为 repo 相对 POSIX 前缀，落在 repo 外即 `CANDIDATE_REPO_INVALID`）
  - `candidate_root_from_envelope(task: Mapping[str, object]) -> Path`——`task_type == "REMEDIATION"` → `source_worktree`，其余 → `repository_root`；resolve 后必须是 git worktree，否则 `CANDIDATE_REPO_INVALID`。**这是唯一权威 root 来源**
  - `scan_candidate_manifest(repo: Path, *, exclusions: tuple[PurePosixPath, ...]) -> tuple[CandidateEntry, ...]`——候选范围（tracked + untracked，排除目录之外）全量条目，按路径排序；路径 NFC UTF-8、POSIX 分隔符、不做大小写折叠；mode 只分 `100644`/`100755`，symlink 以目标字符串为内容、kind 为 `link`；submodule → `CANDIDATE_DIGEST_UNSUPPORTED`
  - `compute_tree_digest(manifest: tuple[CandidateEntry, ...]) -> str`（纯函数）
  - `compute_diff_digest(repo: Path, *, baseline_commit: str, exclusions: tuple[PurePosixPath, ...], untracked: tuple[CandidateEntry, ...]) -> str`——`git diff --binary --full-index <baseline_commit> -- .` 附加每个排除前缀的 `:(exclude)<前缀>/**` pathspec；再逐文件段解析 diff 文本、丢弃规范化路径命中排除集的段落（纵深防御）；拼接 untracked 规范化条目后整体 sha256
  - `capture_candidate_state(store: TaskStoreProtocol, task_id: str, *, baseline_commit: str, runtime_evidence_ids: tuple[str, ...]) -> CandidateState`——**签名无 repo/root 参数**；内部：`store._require_task` → `load_artifact(task_dir / "task.json")` → `candidate_root_from_envelope`；`git merge-base --is-ancestor <baseline_commit> HEAD` 验证失败 → `CANDIDATE_BASELINE_INVALID`；准原子流程：记录 `HEAD₁` 与 `manifest₁ = scan_candidate_manifest(...)` → 计算 tree/diff digest → 复读 `HEAD₂` 与 `manifest₂` → `HEAD₁ != HEAD₂` 或 `manifest₁ != manifest₂` 即 `CANDIDATE_STATE_UNSTABLE`

模块纪律：同 Task 02，运行时不得 import `ai_workflow` 等（import-graph
锁定）；git 调用只经 `subprocess.run([...], shell=False)` 固定 argv。

- [ ] **Step 1: 写 digest 规范的失败测试**

临时 git 仓库内覆盖：同内容两目录 digest 稳定；任一 tracked 文件内容
变化 → tree 与 diff 都变；**仅**新增 untracked 文件 → 两 digest 都变；
**仅** POSIX mode 变化（100644→100755）→ tree 变；文件删除 → 两
digest 都变；symlink 目标变化 → tree 变；路径大小写不同名文件不混淆；
submodule → `CANDIDATE_DIGEST_UNSUPPORTED`。

- [ ] **Step 2: 写控制面排除的失败测试**

在临时 repo 内**追踪** `data/state/ai-workflow/<task>/events.jsonl`
（`git add` 落 baseline），随后修改该 tracked 控制面文件：
`diff_digest` 与 `tree_digest` 均不变（pathspec 排除 + 纵深过滤双重
生效，删任一防御都有对应变红测试）；非 git 目录 →
`CANDIDATE_REPO_INVALID`；baseline 非祖先 →
`CANDIDATE_BASELINE_INVALID`。

- [ ] **Step 3: 写准原子竞态的失败测试**

monkeypatch 模块内扫描函数，在 `manifest₁` 与 `manifest₂` 两次采集
之间注入「文件内容变化但 `git status --porcelain` 状态字母仍为 M」的
修改 → `CANDIDATE_STATE_UNSTABLE`；同法注入 HEAD 前进 → 同码；
无注入 → 正常返回。

- [ ] **Step 4: 写权威 root 与 round-trip 的失败测试**

`capture_candidate_state` 的 root 恒等于信封派生值（REMEDIATION 用
`source_worktree`、其余用 `repository_root`，各一例）；用
`inspect.signature` 断言接口无 `repo`/`root` 形参；合法 state
round-trip；缺字段、`runtime_evidence_ids` 含空串、`envelope_hash`
非 64 位十六进制 → fail-closed；`state_digest()` 与
`captured_at_utc` 无关。

- [ ] **Step 5: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_candidate_state -v
```

Expected: 全部新测试失败（模块尚不存在）。

- [ ] **Step 6: 最小实现**

新建模块与 schema；同步 `scripts/sync_plugin.py` 清单与
import-graph 允许边。

- [ ] **Step 7: Verify GREEN + 基线复核**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_candidate_state \
  tests.test_ai_workflow_distribution \
  tests.test_ai_workflow_import_graph \
  tests.test_ai_workflow_baseline_manifest -v
```

Expected: 全部通过。

- [ ] **Step 8: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(candidate-state): add ai-candidate-state-1 with envelope-derived root and manifest double-scan stability"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_candidate_state tests.test_ai_workflow_distribution tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；输出 `PLUGIN_SYNC_OK`。

---

### Task 04: P0-2b 终验裁决不可变历史、canonical preimage、签发者角色证据派生、新鲜度评估

**依赖:** 03

**分支:**

```bash
git worktree add ../wt-sol-adopt-04-final-verdict -b feat/sol-adopt-04-final-verdict
cd ../wt-sol-adopt-04-final-verdict
```

**Files:**

- Create: `config/ai_workflow_final_verdict.schema.json`
- Create: `scripts/ai_workflow_verdicts.py`
- Create: `tests/test_ai_workflow_verdicts.py`
- Modify: `scripts/sync_plugin.py`（`CONFIG_FILES` 增 `ai_workflow_final_verdict.schema.json`；`RUNTIME_FILES` 增 `ai_workflow_verdicts.py`）
- Modify: `tests/test_ai_workflow_distribution.py`
- Modify: `tests/test_ai_workflow_import_graph.py`（允许边表增 verdicts 行）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/config/ai_workflow_final_verdict.schema.json`、`plugins/ai-workflow/runtime/ai_workflow_verdicts.py`

**Interfaces:**

- Consumes: Task 03 全部 Produces；`scripts/ai_workflow_artifacts.py` 的
  `content_id`、`verify_content_id`、`sorted_strs`、`canonical_json`、
  `WorkflowError`、`TaskStoreProtocol`。
- Produces:
  - `FINAL_VERDICT_SCHEMA_VERSION = "ai-final-verdict-1"`
  - `FINAL_VERDICT_FIELDS: frozenset[str]`（`schema_version`、`verdict_id`、`task_id`、`envelope_hash`、`candidate_state`、`verdict`、`verdict_source_role`、`issuer_evidence_id`、`recorded_at_utc`）
  - `VERDICT_VALUES = frozenset({"ACCEPT", "REJECT"})`
  - `FINAL_VERDICT_ISSUER_ROLES = frozenset({"sol_medium_reviewer"})`
  - `ISSUER_ROLE_CONTRACTS: Mapping[str, tuple[str, str, str, str]]`——角色到钉死观测身份四元组 `(model, reasoning_effort, sandbox, permission_profile)`，当前仅 `{"sol_medium_reviewer": ("gpt-5.6-sol", "medium", "read-only", "read-only")}`
  - `FRESHNESS_VALUES = frozenset({"FRESH", "STALE", "MISSING"})`
  - `VERDICT_ID_EXCLUDE = frozenset({"verdict_id"})`——本类 record 的**唯一** exclude 常量，生成与验证共用（只排除自身 ID 字段）
  - `@dataclass(frozen=True) class FinalVerdict`，含 `to_dict() -> dict[str, object]`
  - `validate_final_verdict(value: Mapping[str, object]) -> None`
  - `compute_verdict_id(record: Mapping[str, object]) -> str`——`content_id("ai-final-verdict-1", _verdict_preimage(record), exclude=VERDICT_ID_EXCLUDE)`；`_verdict_preimage` 为模块私有投影（`candidate_state.runtime_evidence_ids` 先经 `sorted_strs` 排序去重），compute 与 verify 共用
  - `verify_verdict_id(record: Mapping[str, object]) -> None`——`verify_content_id("ai-final-verdict-1", _verdict_preimage(record), exclude=VERDICT_ID_EXCLUDE, id_field="verdict_id")`；写、读、重放三路只经此函数
  - `record_final_verdict(store: TaskStoreProtocol, task_id: str, *, verdict: str, candidate_state: CandidateState, issuer_evidence_id: str, recorded_at: str) -> Path`——第一行 `store._assert_lock_held(task_id)`；锁内验真签发者（见下）→ 追加 `final-verdicts.jsonl`（`store.append_task_ledger`）；`verdict_id` 由 `compute_verdict_id` 生成，`verdict_source_role` 由证据派生盖章（见下），两者都不接受调用者传入
  - `load_verdict_history(store: TaskStoreProtocol, task_id: str) -> tuple[FinalVerdict, ...]`——重放逐条 `verify_verdict_id` 重验；截断尾记录、非对象行、`task_id` 不属本任务、重复 `verdict_id`，一律 `VERDICT_LEDGER_CORRUPT`（本账本**无 seq**，不做任何「重复 seq」检查——完整性 = 内容 ID 唯一性 + 行序，见设计「JSONL 账本完整性策略」）
  - `latest_verdict(store: TaskStoreProtocol, task_id: str) -> FinalVerdict | None`（账本行序最新）
  - `evaluate_verdict_freshness(store: TaskStoreProtocol, task_id: str, *, current: CandidateState) -> str`——最新裁决缺失 → `MISSING`；其 `candidate_state` 与 `current` 逐字段整体比较（commit/baseline/tree/diff/排序后证据集合），任一漂移 → `STALE`；否则 `FRESH`（纯比较函数，放行门的 baseline/current 来源见 Task 05）

签发者验真（`record_final_verdict` 锁内强制执行，`ACCEPT` 与
`REJECT` 同规；**`verdict_source_role` 从证据派生，签名无角色参数**）：

1. `store.read_task_ledger(task_id, "runtime-evidence.jsonl")` 中找到
   canonical JSON sha256 == `issuer_evidence_id` 的记录，找不到 →
   `VERDICT_ISSUER_EVIDENCE_UNKNOWN`；
2. 该记录 `verification_status == "VERIFIED"`，否则
   `VERDICT_ISSUER_EVIDENCE_NOT_VERIFIED`；
3. `role = 记录["requested_role"]`；`role ∉ FINAL_VERDICT_ISSUER_ROLES`
   → `VERDICT_ISSUER_ROLE_FORBIDDEN`；
4. 该记录的观测身份四元组 `(observed_model, observed_reasoning_effort,
   observed_sandbox_policy, observed_permission_profile)` 与
   `ISSUER_ROLE_CONTRACTS[role]` **逐字段精确相等**，否则
   `VERDICT_ISSUER_IDENTITY_MISMATCH`；
5. 本任务 `events.jsonl`（经 `store.read_task_ledger(task_id,
   "events.jsonl")`）中存在一条 `RUNTIME_EVIDENCE_RECORDED` 事件其
   `runtime_evidence_sha256 == issuer_evidence_id`，否则
   `VERDICT_ISSUER_EVIDENCE_ORPHAN`；
6. `candidate_state.runtime_evidence_ids` 逐条属于本任务且内容哈希
   匹配；`candidate_state.envelope_hash == artifact_sha256(task.json)`；
7. 通过后将派生的 `role` 盖章进记录的 `verdict_source_role` 字段。

- [ ] **Step 1: 写裁决历史与 golden preimage 的失败测试**

合法裁决追加后 round-trip；**golden preimage**：固定字段的冻结裁决
fixture 的 `compute_verdict_id` 输出等于钉死的哈希字面量（逐字符）；
改 `recorded_at_utc` 即变；手工构造含 `verdict_id` 自引用的输入无法
伪造——`compute_verdict_id` 输出与输入中预填的 `verdict_id` 无关；
`verdict` 越出闭集、缺 `candidate_state` → fail-closed；两次不同裁决
追加后 `load_verdict_history` 按行序返回两条、旧记录字节不变；
`latest_verdict` 取行序最新。

- [ ] **Step 2: 写签发者验真（角色证据派生）的失败测试**

证据记录的 `requested_role` 不在闭集 →
`VERDICT_ISSUER_ROLE_FORBIDDEN`；`issuer_evidence_id` 在
`runtime-evidence.jsonl` 中不存在 → `VERDICT_ISSUER_EVIDENCE_UNKNOWN`；
属于别的任务 → 同码；证据非 VERIFIED →
`VERDICT_ISSUER_EVIDENCE_NOT_VERIFIED`；events.jsonl 中无同哈希
`RUNTIME_EVIDENCE_RECORDED` 事件 → `VERDICT_ISSUER_EVIDENCE_ORPHAN`；
观测身份四元组任一字段偏离钉死合约（model/effort/sandbox/permission
各一例，共四例）→ `VERDICT_ISSUER_IDENTITY_MISMATCH`；`ACCEPT` 与
`REJECT` 各一例验真通过且记录 `verdict_source_role` 恒等于证据派生值；
`inspect.signature(record_final_verdict)` 断言**无**
`verdict_source_role` 形参（调用者无法自报角色）；调用方在
candidate_state 之外的任何输入都无法改变盖章角色。
`ISSUER_ROLE_CONTRACTS` 与 `scripts/ai_workflow_repairs.py:1309-1315`
既有验收映射对 `sol_medium_reviewer` 一致（一致性测试，防双源漂移）。

- [ ] **Step 3: 写新鲜度评估的失败测试**

裁决后 CandidateState 不变 → `FRESH`；**仅** candidate_commit 前进、
**仅** tree_digest 变化、**仅** diff_digest 变化、**仅**证据 ID 集合
变化（四个独立用例）→ `STALE`；无裁决 → `MISSING`；重验追加新裁决后
对新 CandidateState → `FRESH`，旧裁决仍 `STALE`（历史不可变）。

- [ ] **Step 4: 写账本重放 fail-closed 的失败测试**

`final-verdicts.jsonl` 截断尾记录（末行无换行）→
`VERDICT_LEDGER_CORRUPT`；篡改任一历史行内容（`verify_verdict_id`
重验失败）→ 同码；混入 `task_id` 属于其他任务的记录 → 同码；非对象
行 → 同码；重复 `verdict_id` 两行 → 同码。均不得跳过继续。断言测试
名与断言中**不出现**「重复 seq」声称（本账本无 seq）。

- [ ] **Step 5: 写旧账本不动的负向测试**

取本卡开工前已有的 `adversarial-acceptance-1` 账本 fixture，本卡落地
前后分别跑 `replay_acceptance_ledger`，两次重放结果（含
`phase_outcomes`、`current_candidate_commit`、`whole_project_final`）
逐字段一致；`_v2_append` 写出的事件字段集不变。

- [ ] **Step 6: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_verdicts -v
```

Expected: 新测试失败（模块尚不存在）。

- [ ] **Step 7: 最小实现**

新建 `scripts/ai_workflow_verdicts.py` 与 schema；不 import、不修改
`scripts/ai_workflow_repairs.py` 的任何写入路径。同步
`scripts/sync_plugin.py` 清单与 import-graph 允许边。

- [ ] **Step 8: Verify GREEN + 基线复核**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_verdicts \
  tests.test_ai_workflow_adversarial_acceptance \
  tests.test_ai_workflow_distribution \
  tests.test_ai_workflow_baseline_manifest -v
```

Expected: 全部通过，含旧账本一致性负向测试。

- [ ] **Step 9: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(verdicts): add append-only ai-final-verdict-1 history with canonical ids and evidence-derived issuer attestation"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_verdicts tests.test_ai_workflow_adversarial_acceptance tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；旧账本重放一致；输出 `PLUGIN_SYNC_OK`。

---

### Task 05: P0-2c 终验放行出口全量接门（FRESH ACCEPT 语义、门内重放 baseline 权威重算、锁内消费）

**依赖:** 04、08

**分支:**

```bash
git worktree add ../wt-sol-adopt-05-verdict-stale-gate -b feat/sol-adopt-05-verdict-stale-gate
cd ../wt-sol-adopt-05-verdict-stale-gate
```

**Files:**

- Modify: `scripts/ai_workflow_verdicts.py`（新增放行门）
- Modify: `scripts/ai_workflow_repairs.py`（`_v2_append` 终末阶段完成事件接门；`authorize_final_xhigh` 签发前接门——均在各自既有锁块内）
- Modify: `tests/test_ai_workflow_verdicts.py`
- Modify: `tests/test_ai_workflow_repairs.py`
- Modify: `tests/test_ai_workflow_adversarial_acceptance.py`
- Modify: `tests/test_ai_workflow_whole_project_final.py`
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/runtime/ai_workflow_verdicts.py`、`plugins/ai-workflow/runtime/ai_workflow_repairs.py`

**Interfaces:**

- Consumes: Task 04 全部 Produces；Task 08 的
  `consume_owner_authorization_locked`；Task 03 的
  `capture_candidate_state`；`scripts/ai_workflow_repairs.py` 的
  `_v2_append`（`:1285`）、`_ACCEPTANCE_EVENT_TYPES`、
  `replay_acceptance_ledger`、`_AcceptanceReplay`（`phase_outcomes`、
  `current_candidate_commit`、`whole_project_final`）、
  `authorize_final_xhigh`（`:2299`）、`complete_acceptance_assignment`
  （`:2511`）、`record_adversarial_review`（`:2486`）、`run_assignment`
  （`:2802`）；`scripts/ai_workflow_scheduler.py` 的
  `issue_final_acceptance`（`:1261`）。
- Produces:
  - `RELEASE_COMPLETION_PHASES = frozenset({"SOL_XHIGH_TERMINAL_REPAIR"})`（整项目终验流程的终末阶段由 `_is_whole_project_final`（`:368`）上下文判定，同样接门）
  - `require_verdict_fresh_locked(store: TaskStoreProtocol, task_id: str, *, override_authorization_id: str | None = None) -> None`——**签名无 `current` 参数、无 `baseline_commit` 参数**；第一行 `store._assert_lock_held(task_id)`；内部权威重算：
    1. `history = load_verdict_history(store, task_id)`；空 →
       `VERDICT_MISSING`（此时无需 baseline）；
    2. `latest = history[-1]`（行序最新）；`latest.verdict == "REJECT"`
       → `VERDICT_REJECTED`（**无论新鲜与否；永不查 override**）；
    3. **baseline 门内重放**：`baseline = latest.candidate_state.
       baseline_commit`；证据集合门内重读：`runtime_evidence_ids` =
       `store.read_task_ledger(task_id, "events.jsonl")` 中本任务全部
       `RUNTIME_EVIDENCE_RECORDED` 事件的 `runtime_evidence_sha256`；
    4. `current = capture_candidate_state(store, task_id,
       baseline_commit=baseline, runtime_evidence_ids=...)`；
    5. `evaluate_verdict_freshness(store, task_id, current=current)`：
       `STALE` 且无授权 → `VERDICT_STALE`；`STALE` 且有授权 → 同临界区
       `consume_owner_authorization_locked(..., binding={
       "authorization_type": "VERDICT_STALE_OVERRIDE",
       "candidate_state_digest": current.state_digest()})` 后放行，
       放行后任何后续变化再次 `STALE`；`FRESH` → 放行
  - `require_verdict_fresh(store: TaskStoreProtocol, task_id: str, *, override_authorization_id: str | None = None) -> None`——自取锁包装：`with store.lock(task_id): require_verdict_fresh_locked(...)`，除取锁与委派外无任何逻辑

放行出口全量清单（卡内注释 + 测试逐一覆盖）：

1. `_v2_append` 写入终末阶段的 `REPAIR_COMPLETED` / `REVIEW_COMPLETED`
   （覆盖 `complete_acceptance_assignment`、`record_adversarial_review`
   及 `run_assignment` 驱动的完成写入，`:2990`）——在其调用方既有
   `store.lock` 块内调 `require_verdict_fresh_locked(store, task_id)`
   （无任何 baseline/current 传参），校验、消费与完成事件 append 同处
   一段临界区；
2. `authorize_final_xhigh` 签发终验 ticket——在其既有锁块
   （`:2305`）内、决策记录前接门；
3. `issue_final_acceptance` 创建的整项目终验子任务，其完成仍走出口 1；
4. 非账本任务（generic pipeline）的放行权威是 CLI `decide` 的 owner
   决定，不属于本门范围（本门只约束 adversarial-acceptance-1 账本
   任务），以注释与测试钉死该边界。

- [ ] **Step 1: 写 REJECT 永不放行的失败测试**

最新裁决为 `REJECT` 且对当前状态 `FRESH` → 终末完成写入
`VERDICT_REJECTED`；`REJECT` 后因状态漂移变陈旧，持合法
`VERDICT_STALE_OVERRIDE` 授权 → 仍 `VERDICT_REJECTED`，且授权未被
消费（重放无 consumption 记录）；`authorize_final_xhigh` 同样阻断。

- [ ] **Step 2: 写 STALE/MISSING 阻断的失败测试**

`tests/test_ai_workflow_adversarial_acceptance.py`：裁决 `ACCEPT` 落账
后修改候选文件（或推进 commit、替换证据 ID），随后到达终末阶段完成
写入 → `VERDICT_STALE`，`whole_project_final` 不被签发；
`tests/test_ai_workflow_repairs.py`：无裁决时到达终末完成写入 →
`VERDICT_MISSING`。

- [ ] **Step 3: 写全出口覆盖的失败测试**

出口清单每条各一例：终末 `REPAIR_COMPLETED`、终末 `REVIEW_COMPLETED`、
`authorize_final_xhigh`、整项目终验子任务完成，在
MISSING/STALE/REJECT 下均阻断；非终末阶段（如 `REVIEW_1`）的完成
写入**不**需要终验裁决（不误伤阶梯）。

- [ ] **Step 4: 写重验、授权豁免与门内权威重算的失败测试**

`STALE` 后重新终验追加新裁决 → 放行；不重验但 owner 签发并传入绑定
当前 `CandidateState.state_digest()` 的 `VERDICT_STALE_OVERRIDE`
授权 → 放行且授权被单次消费（再次使用 → `AUTHORIZATION_CONSUMED`）；
授权绑定 digest 与当前状态不符 → `AUTHORIZATION_SCOPE_MISMATCH`；
放行后再改候选文件 → 再次 `VERDICT_STALE`。用 `inspect.signature`
断言 `require_verdict_fresh_locked` 与 `require_verdict_fresh` **均无**
`current`、`baseline_commit` 形参；**baseline 门内权威重放**两例：
(a) 裁决账本最新裁决的 baseline 为 B，调用方环境「以为」baseline 是
A——门内实际以 B 重算（构造在 A 下新鲜、B 下陈旧的候选树，判定必须
为 `STALE`，证明 baseline 来自账本而非调用环境）；(b) 篡改账本最新
裁决的 baseline 字段（连带内容重签失败）→ 重放即
`VERDICT_LEDGER_CORRUPT`，门不开。锁外调用
`require_verdict_fresh_locked` → `LOCK_REQUIRED`；
`authorize_final_xhigh` 路径全程不出现 `TASK_ALREADY_RUNNING`（同
临界区证明）。

- [ ] **Step 5: 写旧形状负向测试**

`_v2_append` 写出的事件字段集与 `_ACCEPTANCE_LEDGER_VERSION` 值断言
不变；授权与豁免信息只进 `owner-authorizations.jsonl` 与
`events.jsonl` 新事件，不进 acceptance 账本事件字段。

- [ ] **Step 6: Verify RED**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_verdicts \
  tests.test_ai_workflow_repairs \
  tests.test_ai_workflow_adversarial_acceptance \
  tests.test_ai_workflow_whole_project_final -v
```

Expected: 新增放行语义/出口测试失败（当前无新鲜度门）。

- [ ] **Step 7: 最小实现**

`require_verdict_fresh_locked` 接进 `_v2_append` 终末阶段分支与
`authorize_final_xhigh` 既有锁块；baseline 由门内重放裁决账本获得，
两个接门点都不传任何 baseline/current。不改动 acceptance 账本事件
形状。

- [ ] **Step 8: Verify GREEN + 全量回归 + 基线复核**

```bash
python3.11 -m unittest discover -s tests
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
```

Expected: 基线清单用例全部保持通过，新增全绿。

- [ ] **Step 9: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(verdicts): gate acceptance release on fresh ACCEPT verdict with in-gate ledger-replayed baseline"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_repairs tests.test_ai_workflow_adversarial_acceptance tests.test_ai_workflow_whole_project_final tests.test_ai_workflow_baseline_manifest -v
python3.11 -m unittest discover -s tests
```

Expected: 四出口阻断、REJECT 永不放行、重验/豁免放行、baseline 门内
重放均有测试证据；全量 0 失败。

---

### Task 06: P0-3a 所有权登记与副作用账本（控制面分离）

**依赖:** 01

**分支:**

```bash
git worktree add ../wt-sol-adopt-06-ownership -b feat/sol-adopt-06-ownership
cd ../wt-sol-adopt-06-ownership
```

**Files:**

- Create: `config/ai_workflow_ownership_registry.schema.json`
- Create: `config/ai_workflow_side_effect.schema.json`
- Create: `scripts/ai_workflow_ownership.py`
- Create: `tests/test_ai_workflow_ownership.py`
- Modify: `scripts/sync_plugin.py`（`CONFIG_FILES` 增上述两个 schema；`RUNTIME_FILES` 增 `ai_workflow_ownership.py`）
- Modify: `tests/test_ai_workflow_distribution.py`
- Modify: `tests/test_ai_workflow_import_graph.py`（允许边表增 ownership 行）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/config/ai_workflow_ownership_registry.schema.json`、`plugins/ai-workflow/config/ai_workflow_side_effect.schema.json`、`plugins/ai-workflow/runtime/ai_workflow_ownership.py`

**Interfaces:**

- Consumes:
  - `scripts/ai_workflow_planning.py`: `scope_owner_map(plan: FrozenPlan) -> dict[str, str]`（`:654`）、`normalize_scope(path: str) -> PurePosixPath`（`:50`）
  - `scripts/ai_workflow_artifacts.py`: `WorkflowError`、`TaskStoreProtocol`
- Produces:
  - `OWNERSHIP_REGISTRY_SCHEMA_VERSION = "ai-ownership-registry-1"`
  - `OWNERSHIP_REGISTRY_FIELDS: frozenset[str]`（`schema_version`、`task_id`、`envelope_hash`、`path_owners`、`registered_at_utc`）
  - `SIDE_EFFECT_SCHEMA_VERSION = "ai-side-effect-1"`
  - `EFFECT_KINDS = frozenset({"CONTROL_PLANE_ARTIFACT", "OWNED_WRITE", "UNTRACKED_WRITE", "COMMAND_GENERATED", "EXTERNAL", "UNOBSERVED_ASSUMED_PRESENT"})`
  - `LOCKING_EFFECT_KINDS = frozenset({"OWNED_WRITE", "UNTRACKED_WRITE", "COMMAND_GENERATED", "EXTERNAL", "UNOBSERVED_ASSUMED_PRESENT"})`
  - `OWNERSHIP_VIOLATION_EVENT_TYPE = "OWNERSHIP_VIOLATION_RECORDED"`——**这只是 `events.jsonl` 的事件类型，不是 `effect_kind`**；`EFFECT_KINDS` 闭集不含、也永不加入该值；`side-effects.jsonl` 永不承载 violation 账本项（violation 的唯一权威持久来源与事件字段闭集见 Task 09）
  - `@dataclass(frozen=True) class OwnershipRegistry`，含 `to_dict() -> dict[str, object]`
  - `validate_ownership_registry(value: Mapping[str, object]) -> None`
  - `build_ownership_registry(*, task_id: str, envelope_hash: str, plan: FrozenPlan, registered_at_utc: str) -> OwnershipRegistry`（`path_owners` 键经 `normalize_scope` 规范化）
  - `record_ownership_registry(store: TaskStoreProtocol, task_id: str, registry: OwnershipRegistry) -> Path`（`store.write_task_artifact_once`，冲突码 `OWNERSHIP_REGISTRY_CONFLICT`）
  - `load_ownership_registry(store: TaskStoreProtocol, task_id: str) -> OwnershipRegistry | None`
  - `record_side_effect(store: TaskStoreProtocol, task_id: str, *, role: str, path: str, effect_kind: str, permit_id: str | None = None, extra: Mapping[str, object] | None = None) -> None`（append-only `side-effects.jsonl` + `SIDE_EFFECT_RECORDED` 事件；路径经 `normalize_scope`；kind 越出闭集即拒绝；`extra` 承载 `COMMAND_GENERATED` 的 `producer`/`producer_ref`/`command_sha256s` 等结构化 metadata。此函数只是记录 API，不构成副作用证据）
  - `record_side_effect_locked(store: TaskStoreProtocol, task_id: str, *, role: str, path: str, effect_kind: str, permit_id: str | None = None, extra: Mapping[str, object] | None = None) -> None`（第一行 `_assert_lock_held`，供执行汇点锁内使用）
  - `load_side_effects(store: TaskStoreProtocol, task_id: str) -> tuple[dict[str, object], ...]`（重放 fail-closed：截断尾记录、非对象行、跨任务记录 → `SIDE_EFFECT_LEDGER_CORRUPT`；本账本**无 seq**，不做「重复 seq」检查——完整性 = 行序 + 闭集校验，见设计「JSONL 账本完整性策略」）
  - `has_ownership_locking_side_effect(store: TaskStoreProtocol, task_id: str) -> bool`（只统计 `LOCKING_EFFECT_KINDS`）

- [ ] **Step 1: 写登记器的失败测试**

`build_ownership_registry` 的 `path_owners` 与 `scope_owner_map(plan)`
逐键一致且键已规范化（含 `./`、`..`、重复分隔符的输入被规范化）；
重复登记以 `OWNERSHIP_REGISTRY_CONFLICT` 拒绝；`ai-task-1` 任务信封
字段集在登记前后不变（读取并断言原九字段）。

- [ ] **Step 2: 写副作用账本与控制面分离的失败测试**

`record_side_effect` 追加 `side-effects.jsonl` 并写
`SIDE_EFFECT_RECORDED` 事件；`effect_kind` 越出闭集 → 拒绝（含
**负向钉死**：`effect_kind="OWNERSHIP_VIOLATION_RECORDED"` 同样拒绝——
事件类型不是 effect kind）；`UNTRACKED_WRITE`、`COMMAND_GENERATED`、
`EXTERNAL`、`UNOBSERVED_ASSUMED_PRESENT` 记录后
`has_ownership_locking_side_effect` 为真；**`CONTROL_PLANE_ARTIFACT`
记录后仍为假**（声明、登记器、预检记录、许可账本等控制面工件逐一
举例断言）；锁外调 `record_side_effect_locked` → `LOCK_REQUIRED`。

- [ ] **Step 3: 写账本重放 fail-closed 的失败测试**

`side-effects.jsonl` 截断尾记录 → `SIDE_EFFECT_LEDGER_CORRUPT`；混入
其他 `task_id` 记录 → 同码；非对象行 → 同码。测试名与断言不出现
「重复 seq」声称（本账本无 seq）。

- [ ] **Step 4: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_ownership -v
```

Expected: 新测试失败（模块尚不存在）。

- [ ] **Step 5: 最小实现**

新建模块与两份 schema；账本 append-only。同步
`scripts/sync_plugin.py` 清单与 import-graph 允许边。

- [ ] **Step 6: Verify GREEN + 基线复核**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_ownership \
  tests.test_ai_workflow_distribution \
  tests.test_ai_workflow_baseline_manifest -v
```

Expected: 全部通过。

- [ ] **Step 7: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(ownership): add ai-ownership-registry-1 sidecar and side-effect ledger with control-plane separation"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_ownership tests.test_ai_workflow_distribution tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；输出 `PLUGIN_SYNC_OK`。

---

### Task 07: P0-3b 副作用真实观测挂钩（COMMAND_PRODUCERS 闭集、construction 冻结步骤 producer、EFFECTFUL_ROLES）

**依赖:** 03、06

**分支:**

```bash
git worktree add ../wt-sol-adopt-07-side-effect-observation -b feat/sol-adopt-07-side-effect-observation
cd ../wt-sol-adopt-07-side-effect-observation
```

**Files:**

- Create: `scripts/ai_workflow_side_effects.py`
- Create: `tests/test_ai_workflow_side_effects.py`
- Modify: `scripts/ai_workflow.py`（`run_codex` 子进程段前后接观测挂钩，含异常路径的未知副作用记录；复用既有 `parse_codex_jsonl` 输出；construction 上下文传入冻结步骤 producer metadata）
- Modify: `scripts/ai_workflow_repairs.py`（`run_assignment` controller 执行段前后接观测挂钩）
- Modify: `tests/test_ai_workflow.py`
- Modify: `tests/test_ai_workflow_repairs.py`
- Modify: `scripts/sync_plugin.py`（`RUNTIME_FILES` 增 `ai_workflow_side_effects.py`）
- Modify: `tests/test_ai_workflow_distribution.py`
- Modify: `tests/test_ai_workflow_import_graph.py`（允许边表增 side_effects 行）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/runtime/ai_workflow_side_effects.py`、`plugins/ai-workflow/runtime/ai_workflow.py`、`plugins/ai-workflow/runtime/ai_workflow_repairs.py`

**Interfaces:**

- Consumes: Task 03 的 `candidate_exclusions`、`scan_candidate_manifest`
  （快照原语）；Task 06 的 `record_side_effect`、
  `record_side_effect_locked`、`load_ownership_registry`、
  `LOCKING_EFFECT_KINDS`；`scripts/ai_workflow.py` 的 `run_codex`
  （`:1656`）、`capture_repo`（`:329`）、`parse_codex_jsonl`
  （调用点 `:1860`）、`ConstructionExecutionContext`（`:2757`，仅取
  `plan.plan_sha256` 与 `step.id` 两个字符串字段，经 `run_codex` 既有
  形参 `construction_context` 获得）；`scripts/ai_workflow_repairs.py`
  的 `run_assignment`（`:2802`）。
- Produces:
  - `@dataclass(frozen=True) class FSEntry`（`path`、`mode`、`kind`、`content_sha256`）
  - `@dataclass(frozen=True) class FSSnapshot`（`root`、`entries: tuple[FSEntry, ...]`、`head: str`）
  - `@dataclass(frozen=True) class FSChange`（`path`、`change_kind`（`ADDED`/`MODIFIED`/`DELETED`）、`entry_after: FSEntry | None`）
  - `COMMAND_PRODUCERS = frozenset({"ROLLOUT_TOOL_EVENTS", "CONSTRUCTION_FROZEN_STEP"})`
  - `@dataclass(frozen=True) class CommandExecution`（`command_sha256`、`producer`（∈ `COMMAND_PRODUCERS`，**不得恒定**）、`producer_ref`：`ROLLOUT_TOOL_EVENTS` → 工具事件在事件流中的序号字符串；`CONSTRUCTION_FROZEN_STEP` → `"<plan_sha256>:<subtask_id>"`）
  - `capture_fs_snapshot(repo: Path, *, exclusions: tuple[PurePosixPath, ...]) -> FSSnapshot`
  - `diff_fs_snapshots(before: FSSnapshot, after: FSSnapshot) -> tuple[FSChange, ...]`
  - `classify_side_effect(change: FSChange, *, path_owners: Mapping[str, str]) -> str`（owned 路径内 → `OWNED_WRITE`；候选范围但未分配 → `UNTRACKED_WRITE`；控制面排除目录内 → `CONTROL_PLANE_ARTIFACT`。**永不产生 `COMMAND_GENERATED`**——静态扫描断言该函数源码不含 `"COMMAND_GENERATED"` 字面量以外的返回值路径）
  - `extract_command_executions(rollout_events: tuple[Mapping[str, object], ...]) -> tuple[CommandExecution, ...]`——从既有 `parse_codex_jsonl` 解析出的事件流提取命令/工具执行条目，逐条规范化取 `command_sha256`，`producer="ROLLOUT_TOOL_EVENTS"`、`producer_ref=事件序号`；识别规则以真实采样 fixture 的 golden 测试冻结；空输入返回空元组
  - `construction_step_producer_ref(*, plan_sha256: str, subtask_id: str) -> str`——返回 `"<plan_sha256>:<subtask_id>"`；两参数必须为非空 64 位十六进制 / 非空字符串，否则拒绝
  - `retag_command_executions(executions: tuple[CommandExecution, ...], *, producer: str, producer_ref: str) -> tuple[CommandExecution, ...]`——`producer ∉ COMMAND_PRODUCERS` 即拒绝；供 construction 路径把 rollout 提取的命令执行重标为冻结步骤 producer
  - `EFFECTFUL_ROLE_SANDBOXES = frozenset({"workspace-write", "assignment-scoped-write"})`
  - `derive_effectful_roles(config: Mapping[str, object]) -> frozenset[str]`——角色钉死 `sandbox ∈ EFFECTFUL_ROLE_SANDBOXES` 即 effectful（只读角色永不 effectful）
  - `record_external_side_effect_locked(store: TaskStoreProtocol, task_id: str, *, role: str, permit_id: str) -> None`（追加 `EXTERNAL` 账本项，第一行 `_assert_lock_held`；供许可单事务步骤 1 接线）
  - `observe_execution_side_effects(store: TaskStoreProtocol, task_id: str, *, role: str, permit_id: str | None, before: FSSnapshot, after: FSSnapshot, rollout_events: tuple[Mapping[str, object], ...] = (), construction_step: Mapping[str, object] | None = None) -> tuple[FSChange, ...]`——逐条 FS diff 分类写账本；`extract_command_executions(rollout_events)` 非空则追加一条 `COMMAND_GENERATED`：`construction_step` 为 None → 原样入账（producer=`ROLLOUT_TOOL_EVENTS`）；否则先经 `retag_command_executions(..., producer="CONSTRUCTION_FROZEN_STEP", producer_ref=construction_step_producer_ref(plan_sha256=construction_step["plan_sha256"], subtask_id=construction_step["subtask_id"]))` 重标——账本项 `extra` 携带 `producer`、`producer_ref`、全部 `command_sha256s`；返回完整变更集供所有权复核
  - `record_unobserved_side_effect(store: TaskStoreProtocol, task_id: str, *, role: str, permit_id: str | None, reason: str) -> None`（写 `UNOBSERVED_ASSUMED_PRESENT`，锁定级）

挂钩位置（精确到既有代码区）：`run_codex`
（`scripts/ai_workflow.py:1656`）在既有 `capture_repo(repo)` 前置
快照处加 `capture_fs_snapshot`，子进程结束后（含异常路径）再快照并
`observe_execution_side_effects`（`rollout_events` 复用 `:1860` 既有
`parse_codex_jsonl(completed.stdout)` 的结果；既有形参
`construction_context` 非 None 时传 `construction_step=
{"plan_sha256": construction_context.plan.plan_sha256, "subtask_id":
construction_context.step.id}`——两值来自
`CodexConstructionRunner.run_construction`（`:4442`）经 `:4476` 传入的
冻结上下文，不是运行时猜测）；子进程已启动但结果未知（超时、异常、
崩溃）→ `record_unobserved_side_effect`。`run_assignment`
（`scripts/ai_workflow_repairs.py:2802`）在 `_v2_controller_snapshot`
（`:2558`，调用点 `:2843`）之后、`codex exec resume` 子进程段
（spawn 于 `:2914`）前后做同样观测，`rollout_events` 复用 `:2929`
既有解析结果。控制面写入由排除规则归入 `CONTROL_PLANE_ARTIFACT`。

- [ ] **Step 1: 写快照与 diff 的失败测试**

临时 git 仓库：新增/修改/删除/untracked 文件各产生对应 `FSChange`；
mode 变化产生 `MODIFIED`；排除目录内变化不出现在 diff 中；
`classify_side_effect` 对 owned/未分配/控制面三路分类正确，且对任何
输入都不返回 `COMMAND_GENERATED`（含命令痕迹样例路径）。

- [ ] **Step 2: 写 COMMAND_GENERATED producer 的失败测试**

以真实采样（或经既有测试 fixture 固化）的 `parse_codex_jsonl` 事件流
输入：含 shell/工具执行条目 → `extract_command_executions` 返回对应
`command_sha256` 且 `producer == "ROLLOUT_TOOL_EVENTS"`、
`producer_ref` 为事件序号；`observe_execution_side_effects` 追加一条
`COMMAND_GENERATED` 且 `extra.producer == "ROLLOUT_TOOL_EVENTS"`；
无命令条目的事件流 + FS diff 有新增文件 → 只产生 OWNED/UNTRACKED
分类，**不**产生 `COMMAND_GENERATED`（证明该 kind 不来自 FS diff
猜测）。

- [ ] **Step 3: 写 construction 冻结步骤 producer 的失败测试**

传入 `construction_step={"plan_sha256": <64 hex>, "subtask_id": "..."}`
时，`COMMAND_GENERATED` 账本项 `extra.producer ==
"CONSTRUCTION_FROZEN_STEP"` 且 `extra.producer_ref ==
"<plan_sha256>:<subtask_id>"`；不传时保持 `ROLLOUT_TOOL_EVENTS`——
同一事件流两种 producer 可区分（`CommandExecution.producer` 不恒定
的运行时证据）；`construction_step_producer_ref` 对畸形
plan_sha256/subtask_id 拒绝；`retag_command_executions` 对闭集外
producer 拒绝；`run_codex` 的 construction 路径（fake 化执行 +
真实 `ConstructionExecutionContext`）账本中出现冻结步骤 producer 的
`COMMAND_GENERATED`。

- [ ] **Step 4: 写执行器观测的失败测试**

`tests/test_ai_workflow.py`：fake 化的 live runner 场景下，执行后工作
树新增文件被自动记录为相应 kind；只读角色运行后工作树无变化 → 账本
无锁定级记录；执行期间子进程抛异常/超时（stub）→ 账本出现
`UNOBSERVED_ASSUMED_PRESENT` 且 `has_ownership_locking_side_effect`
为真（不得假定零副作用）。

- [ ] **Step 5: 写 run_assignment 观测的失败测试**

`tests/test_ai_workflow_repairs.py`：v2 controller 路径执行前后快照被
采集；异常退出同样落 `UNOBSERVED_ASSUMED_PRESENT`；观测返回的变更集
与既有 `actual_changed_paths`（`:2947`）口径一致（对拍断言）。

- [ ] **Step 6: 写不自报与 effectful 派生的失败测试**

全程不调用 `record_side_effect` 的执行路径（直接驱动 `run_codex` 级
挂钩）也能在账本中产生记录——证明证据来自宿主观测而非调用者自报；
`derive_effectful_roles` 对当前 `config/ai_workflow.toml` 逐角色
判定结果与「sandbox 非 read-only」手算一致（tomllib 解析对拍），
`sol_medium_reviewer` 等只读角色不在结果集内。

- [ ] **Step 7: Verify RED**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_side_effects \
  tests.test_ai_workflow \
  tests.test_ai_workflow_repairs -v
```

Expected: 新增观测测试失败。

- [ ] **Step 8: 最小实现**

新建 `scripts/ai_workflow_side_effects.py`；在两个执行点接挂钩；不改
任何既有验证语义（HEAD_DRIFT、READ_ONLY_ROLE_MODIFIED_REPO 等保持
原样）。同步 `scripts/sync_plugin.py` 与 import-graph 允许边。

- [ ] **Step 9: Verify GREEN + 全量回归 + 基线复核**

```bash
python3.11 -m unittest discover -s tests
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
```

Expected: 基线清单用例保持通过，新增全绿。

- [ ] **Step 10: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(side-effects): observe executor effects with rollout- and frozen-step-sourced command attribution"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_side_effects tests.test_ai_workflow tests.test_ai_workflow_repairs tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；异常路径 `UNOBSERVED_ASSUMED_PRESENT`、两类
COMMAND_GENERATED producer 可区分测试证据齐备；输出
`PLUGIN_SYNC_OK`。

---

### Task 08: P0-3c 版本化 owner 授权 sidecar（分类 exclude、共用投影、golden preimage、`_locked` 变体、lease 记录）

**依赖:** 03

**分支:**

```bash
git worktree add ../wt-sol-adopt-08-owner-authorization -b feat/sol-adopt-08-owner-authorization
cd ../wt-sol-adopt-08-owner-authorization
```

**Files:**

- Create: `config/ai_workflow_owner_authorization.schema.json`
- Create: `scripts/ai_workflow_authorizations.py`
- Create: `tests/test_ai_workflow_authorizations.py`
- Modify: `scripts/sync_plugin.py`（`CONFIG_FILES` 增 `ai_workflow_owner_authorization.schema.json`；`RUNTIME_FILES` 增 `ai_workflow_authorizations.py`）
- Modify: `tests/test_ai_workflow_distribution.py`
- Modify: `tests/test_ai_workflow_import_graph.py`（允许边表增 authorizations 行）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/config/ai_workflow_owner_authorization.schema.json`、`plugins/ai-workflow/runtime/ai_workflow_authorizations.py`

**Interfaces:**

- Consumes: `scripts/ai_workflow_artifacts.py` 的 `content_id`、
  `verify_content_id`、`sorted_strs`、`WorkflowError`、`TaskStoreProtocol`；
  Task 03 的 `CandidateState.state_digest`（仅作字符串口径引用）。
- Produces:
  - `OWNER_AUTHORIZATION_SCHEMA_VERSION = "ai-owner-authorization-1"`
  - `AUTHORIZATION_TYPES = frozenset({"VERDICT_STALE_OVERRIDE", "OWNERSHIP_TRANSFER"})`
  - `AUTHORIZATION_RECORD_KINDS = frozenset({"authorization", "consumption", "transfer_lease"})`
  - `OWNER_AUTHORIZATION_FIELDS: frozenset[str]`（字段词汇表并集：`schema_version`、`record_kind`、`authorization_id`、`record_id`、`authorization_type`、`task_id`、`envelope_hash`、`candidate_state_digest`、`path`、`from_role`、`to_role`、`allowed_paths`、`max_dispatches`、`permit_id`、`dispatch_seq`、`binding`、`actor`、`owner_evidence_id`、`issued_at_utc`）
  - `AUTHORIZATION_ID_EXCLUDE = frozenset({"authorization_id"})`——authorization 记录专用 exclude 常量，**只排除自身**
  - `RECORD_ID_EXCLUDE = frozenset({"record_id"})`——consumption/transfer_lease 记录专用 exclude 常量，**只排除自身**；这两类记录的 `authorization_id` **必须进入** `record_id` preimage。禁止任何跨类共用 exclude 的组合（旧 `OWNER_AUTH_ID_EXCLUDE = {"authorization_id", "record_id"}` 废止，不得出现）
  - `_authorization_preimage(record: Mapping[str, object]) -> dict[str, object]`——**模块私有 canonical projection**（下划线前缀、无 store 形参、不做 I/O）：集合语义字段（`allowed_paths`）经 `sorted_strs` 排序去重；`compute_authorization_id` 与 `verify_authorization_id` 共用本投影
  - `_record_preimage(record: Mapping[str, object]) -> dict[str, object]`——同上，consumption/transfer_lease 专用；`compute_record_id` 与 `verify_record_id` 共用本投影。**禁止** compute 规范化而 verify 对原始列表直接哈希
  - `@dataclass(frozen=True) class OwnerAuthorization`，含 `to_dict() -> dict[str, object]`
  - `validate_owner_authorization(value: Mapping[str, object]) -> None`——按 `record_kind` 分型强制 wire 闭集（**不适用字段强制不存在——不是 null、不是空串**）：
    - `authorization`：必有 `schema_version`、`record_kind`、`authorization_id`、`authorization_type`、`task_id`、`envelope_hash`、`actor`、`owner_evidence_id`、`issued_at_utc` + 类型作用域（`VERDICT_STALE_OVERRIDE` → `candidate_state_digest`；`OWNERSHIP_TRANSFER` → `path`、`from_role`、`to_role`、`allowed_paths`、`max_dispatches`）；**禁止出现** `record_id`、`permit_id`、`dispatch_seq`、`binding` 与另一类型的作用域字段；
    - `consumption`：必有 `schema_version`、`record_kind`、`record_id`、`authorization_id`、`task_id`、`envelope_hash`、`binding`（本次消费的绑定映射）、`issued_at_utc`；其余字段禁止出现；
    - `transfer_lease`：必有 `schema_version`、`record_kind`、`record_id`、`authorization_id`、`task_id`、`envelope_hash`、`permit_id`、`dispatch_seq`、`allowed_paths`（本次声称路径，规范化排序，⊆ 授权 `allowed_paths`）、`issued_at_utc`；其余字段禁止出现
  - `compute_authorization_id(record: Mapping[str, object]) -> str`——`content_id("ai-owner-authorization-1", _authorization_preimage(record), exclude=AUTHORIZATION_ID_EXCLUDE)`
  - `verify_authorization_id(record: Mapping[str, object]) -> None`——`verify_content_id("ai-owner-authorization-1", _authorization_preimage(record), exclude=AUTHORIZATION_ID_EXCLUDE, id_field="authorization_id")`（**与生成同一 exclude、同一投影**）
  - `compute_record_id(record: Mapping[str, object]) -> str`——`content_id("ai-owner-authorization-1", _record_preimage(record), exclude=RECORD_ID_EXCLUDE)`，consumption/transfer_lease 记录专用；`authorization_id` 在 preimage 内
  - `verify_record_id(record: Mapping[str, object]) -> None`——`verify_content_id("ai-owner-authorization-1", _record_preimage(record), exclude=RECORD_ID_EXCLUDE, id_field="record_id")`（**与生成同一 exclude、同一投影**）
  - `issue_owner_authorization(store: TaskStoreProtocol, task_id: str, *, authorization_type: str, actor: str, owner_evidence_id: str, issued_at_utc: str, candidate_state_digest: str | None = None, path: str | None = None, from_role: str | None = None, to_role: str | None = None, allowed_paths: tuple[str, ...] | None = None, max_dispatches: int | None = None) -> OwnerAuthorization`
  - `load_owner_authorization(store: TaskStoreProtocol, task_id: str, authorization_id: str) -> OwnerAuthorization | None`——**纯读取函数**（只经 `store.read_task_ledger`，**永不取锁**），可在临界区内安全调用
  - `consume_owner_authorization(store: TaskStoreProtocol, task_id: str, authorization_id: str, *, binding: Mapping[str, object]) -> OwnerAuthorization`——自取锁包装（仅取锁委派）
  - `consume_owner_authorization_locked(store: TaskStoreProtocol, task_id: str, authorization_id: str, *, binding: Mapping[str, object]) -> OwnerAuthorization`——第一行 `_assert_lock_held`；授权不存在 → `AUTHORIZATION_UNKNOWN`；已有 consumption 记录 → `AUTHORIZATION_CONSUMED`；`binding` 与授权作用域精确不一致 → `AUTHORIZATION_SCOPE_MISMATCH`；通过则追加 `record_kind="consumption"` 记录（`record_id` 由 `compute_record_id` 生成，`authorization_id` 进入 preimage，`binding` 随记录落账），单次消费
  - `record_transfer_lease_locked(store: TaskStoreProtocol, task_id: str, authorization_id: str, *, permit_id: str, paths: tuple[str, ...]) -> dict[str, object]`——第一行 `_assert_lock_held`；`paths` 经规范化排序且必须 ⊆ 授权 `allowed_paths`（否则 `AUTHORIZATION_SCOPE_MISMATCH`）；锁内重放该授权既有 lease 数 ≥ 其 `max_dispatches` → `AUTHORIZATION_EXHAUSTED`；否则追加 `record_kind="transfer_lease"` 记录（`allowed_paths` = 本次声称路径的规范化排序闭集；`dispatch_seq` = 该授权当前 lease 数 + 1，按 `authorization_id` 局部从 1 连续；`record_id` 由 `compute_record_id` 生成）并返回
  - `count_transfer_leases(store: TaskStoreProtocol, task_id: str, authorization_id: str) -> int`——纯读取函数（永不取锁）
  - `leases_for_permit(store: TaskStoreProtocol, task_id: str, permit_id: str) -> tuple[Mapping[str, object], ...]`——**纯读取函数**（永不取锁）：重放过滤 `record_kind == "transfer_lease"` 且 `permit_id` 匹配的记录；供 Task 09 `verify_actual_write_paths` 取本次允许集（**禁止**合并其他 permit 的历史 lease）
  - `replay_authorizations(store: TaskStoreProtocol, task_id: str) -> tuple[dict[str, object], ...]`——纯读取函数（永不取锁）；逐条按 `record_kind` 重验（`authorization` → `verify_authorization_id`；`consumption`/`transfer_lease` → `verify_record_id`）；截断尾记录、非对象行、跨任务记录、重复 `record_id`、同一 `authorization_id` 出现两条 `authorization` 记录、lease 的 `dispatch_seq` 局部断档/重复 → `AUTHORIZATION_LEDGER_CORRUPT`（本账本**无全局 seq**，不做「重复 seq」检查——完整性 = 内容 ID 唯一性 + 局部 `dispatch_seq` 连续性 + 行序，见设计「JSONL 账本完整性策略」）

规则：`VERDICT_STALE_OVERRIDE` 必须携带 `candidate_state_digest`；
`OWNERSHIP_TRANSFER` 必须携带 `path`/`from_role`/`to_role` 且携带聚焦
闭集 `allowed_paths` + `max_dispatches`；两类不共用笔段之外的模糊通道。
`owner_evidence_id` 必须指向 `human-decisions.jsonl` 中同 `actor` 的
一条 owner 决定记录。`apply_owner_decision` 的 `OWNER_DECISIONS` 闭集
不变。

- [ ] **Step 1: 写签发校验与 golden preimage 的失败测试**

合法两类授权 round-trip；类型越出闭集、缺类型对应的作用域字段、
`owner_evidence_id` 不存在或 actor 不符、`max_dispatches` 非正整数，
均 fail-closed；**wire 分型负向**：authorization 记录携带
`record_id`（或 `permit_id`/`dispatch_seq`/`binding`）→ 拒绝——即使
值为 null/空串也拒绝（必须不存在）；consumption 缺 `authorization_id`
或缺 `binding` → 拒绝；lease 缺 `permit_id`/`dispatch_seq` → 拒绝；
**golden preimage 三例**：固定字段冻结 authorization 记录的
`compute_authorization_id` 输出等于钉死哈希字面量；固定 consumption
记录与固定 transfer_lease 记录的 `compute_record_id` 输出分别等于
钉死字面量；**exclude 只排自身**：预填 `authorization_id` 垃圾值不
改变 `compute_authorization_id` 输出、预填 `record_id` 垃圾值不改变
`compute_record_id` 输出；**负向 golden（关键）**：固定
consumption/lease 记录**只修改 `authorization_id`** →
`verify_record_id` 必须 `CONTENT_ID_MISMATCH`（record_id 密码学绑定
被消费/租用的授权；篡改改指即失效）；**投影共用**：lease 的
`allowed_paths` 不同顺序/含重复值的同一语义记录得到同一
`record_id` 且 `verify_record_id` 通过（证明 verify 与 compute 共用
同一规范化投影，而非对原始列表直接哈希）；用错配 exclude 手算的哈希
无法通过 `verify_authorization_id`/`verify_record_id`。

- [ ] **Step 2: 写单次消费与锁纪律的失败测试**

消费成功追加 consumption 记录（含 `binding` 字段落账）；同一
`authorization_id` 二次消费 → `AUTHORIZATION_CONSUMED`；并发消费
（双线程）只有一者成功；binding 不符 → `AUTHORIZATION_SCOPE_MISMATCH`
且不产生 consumption；锁外调 `consume_owner_authorization_locked` →
`LOCK_REQUIRED`；在已持锁上下文调 `consume_owner_authorization` 包装
→ `TASK_ALREADY_RUNNING`（证明嵌套锁风险被结构消除：持锁方必须用
`_locked` 变体）。

- [ ] **Step 3: 写 lease 原子扣减的失败测试**

`max_dispatches=2` 的转让授权：连续两次
`record_transfer_lease_locked` 成功且 `dispatch_seq` 为 1、2；第三次
→ `AUTHORIZATION_EXHAUSTED`；并发（双线程在各自任务锁外排队进入）
不超发；lease 记录绑定 `permit_id` 且 `allowed_paths` 等于本次声称
路径的规范化排序（重放可核对哪次派发消耗了额度、声称了哪些路径）；
`paths` 越出授权 `allowed_paths` → `AUTHORIZATION_SCOPE_MISMATCH`；
对 `VERDICT_STALE_OVERRIDE` 类型调 `record_transfer_lease_locked` →
`AUTHORIZATION_SCOPE_MISMATCH`；`leases_for_permit` 只返回绑定该
permit 的 lease（构造两个 permit 各一笔 lease，互不出现在对方结果中）。

- [ ] **Step 4: 写重放 fail-closed 与不扩张旧闭集的失败测试**

截断尾记录、重复 `record_id`、同一 `authorization_id` 两条
authorization 记录、`dispatch_seq` 局部断档/重复、跨任务混入 →
`AUTHORIZATION_LEDGER_CORRUPT`；测试名与断言不出现「重复 seq」声称
（本账本无全局 seq）；`scripts/ai_workflow.py` 的
`OWNER_DECISIONS` 闭集与本卡开工前逐元素一致；
`apply_owner_decision(task_id, "VERDICT_STALE_OVERRIDE", actor)` 与
`apply_owner_decision(task_id, "OWNERSHIP_TRANSFER", actor)` 仍抛
`INVALID_OWNER_DECISION`。

- [ ] **Step 5: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_authorizations -v
```

Expected: 新测试失败（模块尚不存在）。

- [ ] **Step 6: 最小实现**

新建模块与 schema；账本 append-only。同步 `scripts/sync_plugin.py`
清单与 import-graph 允许边。

- [ ] **Step 7: Verify GREEN + 基线复核**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_authorizations \
  tests.test_ai_workflow_distribution \
  tests.test_ai_workflow_baseline_manifest -v
```

Expected: 全部通过，含旧闭集负向测试。

- [ ] **Step 8: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(authorizations): add scoped ai-owner-authorization-1 sidecar with per-kind exclude ids and transfer leases"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_authorizations tests.test_ai_workflow_distribution tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；`OWNER_DECISIONS` 未扩张；golden preimage、
负向 golden（只改 `authorization_id` 即验证失败）与投影共用测试
齐备；输出 `PLUGIN_SYNC_OK`。

---

### Task 09: P0-3d 所有权转让门：scoped lease 原子扣减、permit 绑定实际路径复核、事件型持久 violation 与 `_locked` 查询

**依赖:** 06、07、08

**分支:**

```bash
git worktree add ../wt-sol-adopt-09-ownership-transfer -b feat/sol-adopt-09-ownership-transfer
cd ../wt-sol-adopt-09-ownership-transfer
```

**Files:**

- Modify: `scripts/ai_workflow_ownership.py`（新增重放推导、转让门、permit 绑定实际写路径复核与事件型持久 violation）
- Modify: `tests/test_ai_workflow_ownership.py`
- Modify: `tests/test_ai_workflow_import_graph.py`（允许边表增 `ownership → authorizations`）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/runtime/ai_workflow_ownership.py`

**Interfaces:**

- Consumes: Task 06 全部 Produces（含 `OWNERSHIP_VIOLATION_EVENT_TYPE`）；
  Task 07 的 `observe_execution_side_effects` 返回值口径；Task 08 的
  `consume_owner_authorization_locked`、`record_transfer_lease_locked`、
  `load_owner_authorization`、`count_transfer_leases`、
  `leases_for_permit`。
- Produces:
  - `resolve_path_owner(store: TaskStoreProtocol, task_id: str, path: str) -> str`——`normalize_scope` 规范化后从**不可变登记器**按最长前缀取所有者；转让授权与 lease 不改变本函数结果（当前所有者永不被改写）
  - `precheck_write_ownership(store: TaskStoreProtocol, task_id: str, role: str, *, paths: tuple[str, ...]) -> str`——只读预检（不消费任何授权），返回闭集 `{"OWNED", "LEASE_REQUIRED", "BLOCKED"}`；供 `_run_role_with_technical_retry` 早失败层使用
  - `require_write_ownership_locked(store: TaskStoreProtocol, task_id: str, role: str, *, permit_id: str, paths: tuple[str, ...], authorization_id: str | None = None) -> None`——第一行 `_assert_lock_held`；每路径：登记器所有者 == `role` → 放行；否则要求 `authorization_id` 指向绑定该 path/from/to 的 `OWNERSHIP_TRANSFER` 授权：已有锁定级副作用时授权必须携带聚焦闭集且 `paths ⊆ allowed_paths`（`AUTHORIZATION_SCOPE_MISMATCH`）；同临界区 `record_transfer_lease_locked` 扣减额度（`AUTHORIZATION_EXHAUSTED` 即拒），lease 记录绑定本次 `permit_id`；完全无授权 → `OWNERSHIP_TRANSFER_BLOCKED`
  - `verify_actual_write_paths(store: TaskStoreProtocol, task_id: str, role: str, *, permit_id: str, actual_paths: tuple[str, ...]) -> None`——**签名必须接收本次 `permit_id`**；允许集 = 该角色登记器名下路径 ∪ **绑定本次 `permit_id` 的 transfer_lease 记录**（经 `leases_for_permit` 重放过滤，**禁止**合并其他 permit 的历史 lease）的 `allowed_paths`；实际写路径超出允许集 → 向 `events.jsonl` 追加**持久** `OWNERSHIP_VIOLATION_RECORDED` 事件（字段闭集 `OWNERSHIP_VIOLATION_EVENT_FIELDS`，含越界路径清单与本次 `permit_id`）并抛 `OWNERSHIP_VIOLATION`；**不得**向 `side-effects.jsonl` 写 violation 账本项（`EFFECT_KINDS` 闭集不含该值；实际写副作用仍由 Task 07 观测挂钩按原 effect kind 记录，violation 只作为独立事件）；`actual_paths` 未知/不可得时调用方不得跳过本复核
  - `OWNERSHIP_VIOLATION_EVENT_FIELDS = frozenset({"event_type", "task_id", "envelope_hash", "permit_id", "role", "paths", "timestamp_utc"})`——violation 事件字段闭集（`paths` 为越界路径的规范化排序清单）；`events.jsonl` 的该事件是 violation 的**唯一权威持久来源**（golden 测试冻结事件形状）
  - `has_unresolved_ownership_violation_locked(store: TaskStoreProtocol, task_id: str) -> bool`——第一行 `store._assert_lock_held(task_id)`；**只重放** `events.jsonl` 的 `OWNERSHIP_VIOLATION_RECORDED` 事件这一权威来源：存在任一字段闭集合法的事件即为真；violation 事件字段越闭集/类型错误、跨任务记录、账本截断尾/坏行/无法重放 → fail-closed 抛 `WorkflowError("OWNERSHIP_VIOLATION_LEDGER_CORRUPT", ...)`（阻断方向，不得静默返回 False）；无清除 API（持久）
  - `has_unresolved_ownership_violation(store: TaskStoreProtocol, task_id: str) -> bool`——自取锁包装：`with store.lock(task_id): return has_unresolved_ownership_violation_locked(store, task_id)`，除取锁与委派外无任何逻辑
  - `claimed_write_paths(plan_scopes: tuple[str, ...]) -> tuple[str, ...]`——从冻结计划/施工上下文的作用域规范化得到声称写路径（供执行汇点传入 `require_write_ownership_locked`）

- [ ] **Step 1: 写重放推导的失败测试**

登记器 + 依次消费的两笔转让授权（A→B 再 B→C）后
`resolve_path_owner` 仍返回登记器原始所有者（转让不改写登记器）；
目录授权按前缀匹配；`..`、symlink、相对路径输入经规范化后判定一致。

- [ ] **Step 2: 写转让门与 lease 扣减的失败测试**

无锁定级副作用时，非所有者写路径需授权方可派发（登记器不可变，转让
永远要授权）；任一锁定级副作用（含 `UNTRACKED_WRITE` /
`COMMAND_GENERATED`）之后，缺聚焦闭集的授权 → 拒绝，携带聚焦闭集且
路径匹配 → 放行并追加 lease；`paths` 超出授权 `allowed_paths` →
`AUTHORIZATION_SCOPE_MISMATCH`；lease 用尽后继续派发 →
`AUTHORIZATION_EXHAUSTED`；完全无授权 → `OWNERSHIP_TRANSFER_BLOCKED`
且 executor 未被调用；锁外调用 → `LOCK_REQUIRED`。

- [ ] **Step 3: 写聚焦修复、permit 绑定复核与事件型持久 violation 的失败测试**

副作用后原所有者写自己名下路径 → `precheck_write_ownership` 返回
`OWNED`，不需要授权、不被误伤；`verify_actual_write_paths` 收到观测
到的实际写路径，越出名下路径与**本次 permit** 的 lease 闭集 →
`OWNERSHIP_VIOLATION_RECORDED` **事件**落账于 `events.jsonl`
（字段闭集与 `OWNERSHIP_VIOLATION_EVENT_FIELDS` golden 逐字符钉死，
含本次 `permit_id` 与规范化排序的越界 `paths`）且
`side-effects.jsonl` 中**不存在** violation 账本项（全账本无越
`EFFECT_KINDS` 闭集的 kind）；`has_unresolved_ownership_violation_locked`
为真、再次调用仍为真（持久）；锁外调
`has_unresolved_ownership_violation_locked` → `LOCK_REQUIRED`；已持锁
上下文调无后缀包装 → `TASK_ALREADY_RUNNING`；包装仅取锁委派
（`inspect.getsource` 断言）；篡改 violation 事件字段（少字段/多字段/
类型错误）、混入跨任务 violation 事件、`events.jsonl` 截断尾行 →
查询 fail-closed 抛 `OWNERSHIP_VIOLATION_LEDGER_CORRUPT`（不得静默
返回 False）；**历史 lease 不豁免**（关键负向）：转让授权对 permit_A
扣减产生 lease（`allowed_paths` 含路径 X）；随后另一笔派发 permit_B
（无 lease）实际写路径含 X → `verify_actual_write_paths(...,
permit_id=permit_B, actual_paths=(X, ...))` 判越界并落 violation
事件——证明允许集只查绑定 permit_B 的 lease（此处为空），历史 lease
不为后续 dispatch 提供路径豁免；用 `inspect.signature` 断言
`verify_actual_write_paths` 含 `permit_id` 必填形参；只检查声称路径而
实际写路径未知时 → 调用方必须拒绝放行（以 `run_codex` 接线测试在
Task 13 锁定，本卡先锁定模块级语义）。

- [ ] **Step 4: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_ownership -v
```

Expected: 新增门控测试失败。

- [ ] **Step 5: 最小实现**

实现上述函数；授权只经 Task 08 sidecar 消费，不提供绕过 API；
violation 只经 `store.append_event` 落 `events.jsonl`。同步
import-graph 允许边（`ownership → authorizations`）。

- [ ] **Step 6: Verify GREEN + 全量回归 + 基线复核**

```bash
python3.11 -m unittest discover -s tests
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
```

Expected: 基线清单用例保持通过，新增全绿。

- [ ] **Step 7: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(ownership): scoped transfer leases with per-permit actual-path verification and event-sourced persistent violations"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_ownership tests.test_ai_workflow_import_graph tests.test_ai_workflow_baseline_manifest -v
python3.11 -m unittest discover -s tests
```

Expected: 副作用后未授权更换被拒、聚焦授权放行、lease 用尽拒发、聚焦
修复放行、permit 绑定复核、历史 lease 不豁免、实际路径越权落
events.jsonl 持久 violation（side-effects.jsonl 无 violation 项）、
`_locked` 查询锁纪律与坏记录 fail-closed 均有测试证据；全量 0 失败。

---

### Task 10: P0-4a 费率快照工件与归档链（RATE_UNITS 闭集与基数）

**依赖:** 00

**分支:**

```bash
git worktree add ../wt-sol-adopt-10-rate-snapshot -b feat/sol-adopt-10-rate-snapshot
cd ../wt-sol-adopt-10-rate-snapshot
```

**Files:**

- Create: `config/ai_workflow_rate_snapshot.schema.json`
- Modify: `scripts/ai_workflow_costs.py`
- Modify: `tests/test_ai_workflow_costs.py`
- Modify: `scripts/sync_plugin.py`（`CONFIG_FILES` 增 `ai_workflow_rate_snapshot.schema.json`）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/config/ai_workflow_rate_snapshot.schema.json`、`plugins/ai-workflow/runtime/ai_workflow_costs.py`

**Interfaces:**

- Consumes: `scripts/ai_workflow_costs.py` 现有
  `COST_EVIDENCE_ROLES`（`scripts/ai_workflow_costs.py:66`，已含
  `router_probe`）、`evaluate_optimization_gate`
  （`scripts/ai_workflow_costs.py:589`）。
- Produces:
  - `RATE_SNAPSHOT_SCHEMA_VERSION = "ai-rate-snapshot-1"`
  - `RATE_SNAPSHOT_FIELDS: frozenset[str]`（`schema_version`、`rate_snapshot_id`、`skus`、`effective_at`、`retrieved_at`、`archive`、`approved_by`、`approval_evidence_id`）
  - `RATE_UNITS = frozenset({"PER_TOKEN", "PER_1K_TOKENS", "PER_1M_TOKENS"})`
  - `RATE_UNIT_BASE: Mapping[str, int]`——`{"PER_TOKEN": 1, "PER_1K_TOKENS": 1_000, "PER_1M_TOKENS": 1_000_000}`（单位基数闭集表，换算公式见 Task 11）
  - `RATE_SNAPSHOT_SKU_FIELDS: frozenset[str]`（`sku`、`model`、`currency`、`unit`（∈ `RATE_UNITS`）、`billing_channel`、`price_uncached_input`、`price_cached_input`、`price_output`（三个分项单价为**该 SKU `unit` 基准数量**的主货币单位十进制字符串）、`cache_write_applies`、`long_context_tiers_applies`、`source_url`、`retrieved_at`）
  - `RATE_SNAPSHOT_ARCHIVE_FIELDS: frozenset[str]`（`archive_path`、`archive_sha256`、`mime_type`、`retrieval_status`）；`archive_path` 为内容寻址相对路径 `docs/rate-archives/<archive_sha256>`，哈希必须可解析到实际归档文件
  - `PRICING_STATUSES = frozenset({"CURRENT", "PRICE_STALE", "PRICE_UNKNOWN"})`
  - `validate_rate_snapshot(value: Mapping[str, object]) -> None`（含 SKU `unit ∈ RATE_UNITS`、单价可解析为非负十进制字符串的校验）
  - `load_rate_snapshot(path: Path) -> Mapping[str, object]`
  - `resolve_snapshot_archive(snapshot: Mapping[str, object], *, root: Path) -> Path`（归档文件不存在或内容哈希不符 → `RATE_ARCHIVE_UNRESOLVABLE`）
  - `snapshot_pricing_status(snapshot: Mapping[str, object], *, now_utc: str, max_age_seconds: int, root: Path | None = None) -> str`

- [ ] **Step 1: 写快照校验的失败测试**

合法快照 round-trip；缺 SKU 必填字段、SKU 缺独立 `source_url`、SKU
`unit` 越出 `RATE_UNITS` 闭集、单价为负/非十进制字符串、
`effective_at`/`retrieved_at` 非 UTC 时间、缺 `archive` 或
`approved_by`/`approval_evidence_id` → fail-closed；超
`max_age_seconds` → `PRICE_STALE`；缺关键价格字段 → `PRICE_UNKNOWN`。

- [ ] **Step 2: 写归档链的失败测试**

`archive_path` 指向的文件存在且内容 sha256 匹配 →
`resolve_snapshot_archive` 返回路径；文件缺失或哈希不符 →
`RATE_ARCHIVE_UNRESOLVABLE`；`snapshot_pricing_status` 在归档不可解析
时返回 `PRICE_UNKNOWN`（哈希不是孤立字符串，必须可解析到实际归档）。

- [ ] **Step 3: 写不可变与不追溯的失败测试**

同一 `rate_snapshot_id` 两次写入不同内容 → 拒绝（快照不可变）；
历史 `CostEvidence` 绑定旧快照 ID，加载新快照后旧证据的
`rate_snapshot_id` 不被改写。

- [ ] **Step 4: 写不接生产链的负向测试**

`evaluate_optimization_gate` 在提供/不提供费率快照两种输入下的返回
值逐字段一致（快照不进优化门）。

- [ ] **Step 5: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_costs -v
```

Expected: 新增快照测试失败（schema/函数尚不存在）。

- [ ] **Step 6: 最小实现**

在 `scripts/ai_workflow_costs.py` 追加快照校验、归档解析与状态函数，
不改动 `evaluate_optimization_gate` 与既有证据校验逻辑。同步
`scripts/sync_plugin.py` 的 `CONFIG_FILES`。

- [ ] **Step 7: Verify GREEN + 基线复核**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_costs \
  tests.test_ai_workflow_baseline_manifest -v
```

Expected: 全部通过，含不进优化门的负向测试。

- [ ] **Step 8: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(costs): add immutable ai-rate-snapshot-1 artifact with closed rate units and resolvable archive chain"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_costs tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；输出 `PLUGIN_SYNC_OK`。

---

### Task 11: P0-4b 探针成本逐臂分型报告（权威 usage、localcontext Decimal、minor-unit int、总计规则）

**依赖:** 10

**分支:**

```bash
git worktree add ../wt-sol-adopt-11-probe-cost-layers -b feat/sol-adopt-11-probe-cost-layers
cd ../wt-sol-adopt-11-probe-cost-layers
```

**Files:**

- Modify: `scripts/ai_workflow_router_probe.py`
- Modify: `tests/test_ai_workflow_router_probe.py`

**Interfaces:**

- Consumes: Task 10 的 `load_rate_snapshot`、`snapshot_pricing_status`、
  `resolve_snapshot_archive`、`RATE_UNITS`、`RATE_UNIT_BASE`；现有
  `aggregate_probe_results`（`scripts/ai_workflow_router_probe.py:525`）、
  `evaluate_probe_decision`（`:806`）、`render_probe_report`（`:866`）；
  `scripts/ai_workflow_runtime.py` 的 `USAGE_FIELDS =
  ("input_tokens", "cached_input_tokens", "output_tokens")`（`:54`）。
- Produces:
  - `PROBE_SUMMARY_SCHEMA_VERSION = "router-probe-summary-2"`（协议升级；旧 `router-probe-summary-1` 读取行为有兼容测试）
  - `USAGE_SOURCES = frozenset({"BILLING_USAGE", "TEXT_TOKEN_ESTIMATE"})`
  - `ARM_COST_TYPES = frozenset({"COST_ESTIMATE_UNDER_SNAPSHOT", "TEXT_TOKEN_ESTIMATE", "USAGE_AUTHORITY_UNAVAILABLE"})`
  - `COST_ESTIMATE_TYPES = frozenset({"COST_ESTIMATE_UNDER_SNAPSHOT", "PRICE_STALE", "PRICE_UNKNOWN", "UNAVAILABLE_WITHOUT_RATE_SNAPSHOT"})`
  - `COST_TOTAL_TYPES = frozenset({"COST_TOTAL_UNDER_SNAPSHOT", "COST_TOTAL_UNAVAILABLE"})`
  - `COST_TOTAL_UNAVAILABLE_REASONS = frozenset({"PARTIAL_AUTHORITY", "CURRENCY_MISMATCH", "UNIT_MISMATCH"})`
  - `CURRENCY_MINOR_UNITS: Mapping[str, int]`（如 `{"USD": 2}`；未知币种即 `COST_INPUT_INVALID`）
  - `USAGE_WIRE_SHAPE = ("uncached_input", "cached_input", "output")`——逐臂 `usage`、逐臂 `tokens` 与任何汇总输入总量字段**共用同一个三键形状**（int 值）；wire 一致性由 schema 级测试钉死
  - `compute_arm_cost_minor(*, tokens: int, price: str, unit: str, currency: str) -> int`——换算公式（冻结）：在 `with decimal.localcontext(decimal.Context(prec=28, rounding=decimal.ROUND_HALF_EVEN)):` 内计算 `cost = (Decimal(tokens) * Decimal(price)) / Decimal(RATE_UNIT_BASE[unit])`，再 `minor = (cost * (10 ** CURRENCY_MINOR_UNITS[currency])).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)`，返回 `int(minor)`——**输出为最小货币单位的整数计数（JSON int，如 USD 的美分数）**；`unit ∉ RATE_UNITS`、币种未知、tokens/price 为负 → `COST_INPUT_INVALID`
  - `build_arm_cost_result(arm_id: str, arm_usage: Mapping[str, object], *, snapshot: Mapping[str, object]) -> Mapping[str, object]`——返回三型之一的**逐臂结果对象**：权威计费 usage（`usage_source == "BILLING_USAGE"` 且带证据 ID）→ `COST_ESTIMATE_UNDER_SNAPSHOT`（含 `usage: {"uncached_input", "cached_input", "output"}`、`usage_evidence_ids`、`sku`、`currency`、`unit`、`estimated_cost_minor`（int，经 `compute_arm_cost_minor` 逐分项计算后求和）、`quality: {retries, escalations, reviews, failures}`）；文本 token 估计 → `TEXT_TOKEN_ESTIMATE`（`tokens` 同 `USAGE_WIRE_SHAPE`，**不含金额字段**）；无权威 usage → `USAGE_AUTHORITY_UNAVAILABLE`
  - `compute_cost_total(arms: tuple[Mapping[str, object], ...]) -> Mapping[str, object]`——全部臂为 `COST_ESTIMATE_UNDER_SNAPSHOT` 且 `currency`/`unit` 全一致 → `COST_TOTAL_UNDER_SNAPSHOT`（`total_cost_minor` = 逐臂 int 精确求和）；否则 `COST_TOTAL_UNAVAILABLE` + `reason`（`PARTIAL_AUTHORITY`/`CURRENCY_MISMATCH`/`UNIT_MISMATCH`），**不携带任何数值字段**
  - `build_cost_estimate(arm_usage: Mapping[str, Mapping[str, object]], *, snapshot: Mapping[str, object] | None, now_utc: str, root: Path | None = None) -> Mapping[str, object]`——顶层 `{"type": ..., "arms": [...], "total": {...}}`；快照过期/缺字段/归档不可解析 → 对应 `PRICE_*` 类型且 arms/total 均不输出金额；无快照 → `UNAVAILABLE_WITHOUT_RATE_SNAPSHOT`
  - `aggregate_probe_results(...)` 的 summary 增加 `cost_estimate` 结果对象与 `schema_version: "router-probe-summary-2"`；既有 `CACHE_MECHANISM_CANDIDATE_*` 判定逻辑、`cost_comparison_status` 取值与 `effective_route == "UNCHANGED"` 不变

数值纪律（实现与测试共同锁定）：证据 `input_tokens ==
uncached_input + cached_input` 不成立即拒绝该臂 usage
（`COST_INPUT_INVALID`）；任何 token 数或单价为负 →
`COST_INPUT_INVALID`；金额一律在 `decimal.localcontext(
decimal.Context(prec=28, rounding=ROUND_HALF_EVEN))` 内计算（**禁止**
`Decimal(prec=28)` 这类不可实现表述——`Decimal` 构造器没有 `prec`
形参；静态扫描断言模块不出现 `Decimal(prec`）；`estimated_cost_minor`
与 `total_cost_minor` 为 JSON int；二进制浮点不得进入报告。

- [ ] **Step 1: 写逐臂分型与单位换算的失败测试**

三臂分别构造权威 usage / 文本估计 / 无权威 usage：逐臂类型正确；
文本估计臂不含 `estimated_cost_minor`；`input ≠ uncached + cached`
的臂 → `COST_INPUT_INVALID`；负 token/负单价 → `COST_INPUT_INVALID`；
**单位换算三基数各一例手算对拍**（`PER_TOKEN`/`PER_1K_TOKENS`/
`PER_1M_TOKENS`：同一单价同一 token 数在三单位下的
`estimated_cost_minor` 与手算美分逐一相等）；金额手算对拍（含 0.5
进位边界用例，`ROUND_HALF_EVEN`）；`estimated_cost_minor` 与
`total_cost_minor` 的 `isinstance(..., int)` 断言；逐臂 `usage` 与
汇总输入总量字段同为三键形状（schema 级断言）。

- [ ] **Step 2: 写总计规则的失败测试**

全臂权威且同币种同单位 → `COST_TOTAL_UNDER_SNAPSHOT` 数值正确（int
精确求和）；任一臂降级 → `PARTIAL_AUTHORITY` 且 total 无数值；混币种
→ `CURRENCY_MISMATCH`；混单位 → `UNIT_MISMATCH`；部分权威的总计不被
表述为全路线成本（报告文本断言无「总计」字样伴随部分口径）。

- [ ] **Step 3: 写不改路由、不宣布赢家的负向测试**

所有快照状态下 summary 的 `effective_route == "UNCHANGED"`；
`render_probe_report` 渲染结果中 `cost_winner=` 行的取值仍落在原
`cost_comparison_status` 闭集内；全文不出现任何新造赢家或路线比较
标签（`COST_WINNER`、`REAL_COST_WINNER`、`CHEAPER` 等，逐一断言）；
既有 `CACHE_MECHANISM_CANDIDATE_*` 判定结果在引入快照前后逐字段
一致；成本字段不进入 `evaluate_optimization_gate`（提供/不提供的返回
一致）。

- [ ] **Step 4: 写协议版本的失败测试**

summary `schema_version == "router-probe-summary-2"`；旧读取路径
（`evaluate_probe_decision`、R1–R3 manifest 校验）对 summary-2 的
未知字段不崩溃且判定结果不变；新成本臂不与 R1–R3 样本合并
（`data_origin` 与批次隔离断言）。

- [ ] **Step 5: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_router_probe -v
```

Expected: 新增分层测试失败。

- [ ] **Step 6: 最小实现**

`aggregate_probe_results` 增加 `cost_estimate` 结果对象；
`render_probe_report` 增加 `cost_estimate=` 摘要行（类型 + 快照 ID +
总计或不可用原因）；不改 `evaluate_probe_decision` 的签发逻辑。

- [ ] **Step 7: Verify GREEN + 全量回归 + 基线复核**

```bash
python3.11 -m unittest tests.test_ai_workflow_router_probe tests.test_ai_workflow_baseline_manifest -v
python3.11 -m unittest discover -s tests
```

Expected: 全部通过，基线清单用例不变红。

- [ ] **Step 8: 提交**

`scripts/ai_workflow_router_probe.py` 不在 `RUNTIME_FILES`，无需同步
runtime 镜像；仍跑 `--check` 确认无漂移。

```bash
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(router-probe): emit per-arm discriminated cost results under rate snapshot, keep effective_route UNCHANGED"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_router_probe tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 逐臂分型、总计规则、单位换算、精度舍入、权威/降级 usage
分流与全部负向断言全绿；输出 `PLUGIN_SYNC_OK`。

---

### Task 12: P0-1b dispatch_policy 编排层：许可状态机单事务原语、dispatch_id 永久退休、violation 阻断、EXTERNAL 接线、释放守卫 helper

**依赖:** 02、07、09、14（其接口真实消费 Task 09 的
`has_unresolved_ownership_violation_locked` 与 Task 07 的
`derive_effectful_roles`/`record_external_side_effect_locked`，禁止与
09 并行施工）

**分支:**

```bash
git worktree add ../wt-sol-adopt-12-dispatch-policy -b feat/sol-adopt-12-dispatch-policy
cd ../wt-sol-adopt-12-dispatch-policy
```

**Files:**

- Create: `scripts/ai_workflow_dispatch_policy.py`
- Create: `tests/test_ai_workflow_dispatch_policy.py`
- Modify: `scripts/sync_plugin.py`（`RUNTIME_FILES` 增 `ai_workflow_dispatch_policy.py`）
- Modify: `tests/test_ai_workflow_distribution.py`
- Modify: `tests/test_ai_workflow_import_graph.py`（允许边表增 dispatch_policy 行）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/runtime/ai_workflow_dispatch_policy.py`

**Interfaces:**

- Consumes: Task 02 全部 Produces（含 `load_route_declaration_locked`、
  `ensure_route_declaration`）；Task 14 的
  `require_role_preflighted_locked`、`is_role_preflighted_locked`；Task 09
  的 `has_unresolved_ownership_violation_locked`；Task 07 的
  `derive_effectful_roles`、`record_external_side_effect_locked`；
  `scripts/ai_workflow_routing.py` 的 `RuntimeRouteDecision`；
  `scripts/ai_workflow_planning.py` 的 `dispatch_id`（`:723`）；
  `scripts/ai_workflow_artifacts.py` 的 `WorkflowError`、
  `TaskStoreProtocol`。
- Produces:
  - `DISPATCH_PERMIT_LEDGER = "dispatch-permits.jsonl"`
  - `DISPATCH_PERMIT_SCHEMA_VERSION = "ai-dispatch-permit-1"`
  - `DISPATCH_PERMIT_FIELDS: frozenset[str]`（`schema_version`、`seq`、`permit_id`、`task_id`、`role`、`state`、`reason`、`recorded_at_utc`；`reason` 仅 `RELEASED_BEFORE_START` 记录非空）
  - `PERMIT_STATES = frozenset({"RESERVED", "STARTED", "RELEASED_BEFORE_START"})`
  - `PERMIT_TERMINAL_STATES = frozenset({"STARTED", "RELEASED_BEFORE_START"})`
  - `@dataclass(frozen=True) class DispatchPermit`（`permit_id`、`task_id`、`role`、`reservation_seq`）
  - `derive_dispatch_identity(*, task_sha256: str, role: str, attempt_id: str) -> str`（generic/直调路径）；construction 路径复用 `scripts/ai_workflow_planning.py:723` 的 `dispatch_id`；v2 验收路径 `derive_assignment_dispatch_identity(*, task_sha256: str, assignment_id: str, attempt_id: str) -> str`
  - `replay_permit_ledger(store: TaskStoreProtocol, task_id: str) -> tuple[dict[str, object], ...]`——fail-closed：截断尾记录、非对象行、跨任务记录、重复 `seq`、`seq` 不从 1 连续、非法状态转换（仅 `∅→RESERVED`、`RESERVED→STARTED`、`RESERVED→RELEASED_BEFORE_START` 合法）、同 ID 两条 `RESERVED`、终态后再有同 ID 记录 → `DISPATCH_PERMIT_LEDGER_CORRUPT`
  - `permit_latest_states(records: tuple[dict[str, object], ...]) -> dict[str, str]`（permit_id → 最新 state 的纯函数）
  - `derive_active_roles(store: TaskStoreProtocol, task_id: str, declaration: RouteDeclaration) -> frozenset[str]`（声明 `active_roles` + `ROLE_ACTIVATED` 事件按账本序重放；**禁止**把上一派发角色当状态）
  - `ensure_declaration_for_task(store: TaskStoreProtocol, task_id: str, *, decision: RuntimeRouteDecision, config: Mapping[str, object]) -> RouteDeclaration`（唯一创建阶段编排：锁内经 `load_route_declaration_locked` **先恢复**，再 build + `ensure_route_declaration`）
  - `activate_role(store: TaskStoreProtocol, task_id: str, *, from_role: str, to_role: str) -> None`（校验 `(from_role, to_role)` ∈ 声明 `allowed_transitions`，追加 `ROLE_ACTIVATED` 事件；不在此预检）
  - `require_dispatch_permit_locked(store: TaskStoreProtocol, task_id: str, role: str, *, dispatch_identity: str, config: Mapping[str, object]) -> DispatchPermit`
  - `require_dispatch_permit(store: TaskStoreProtocol, task_id: str, role: str, *, dispatch_identity: str, config: Mapping[str, object]) -> DispatchPermit`——自取锁包装（仅取锁委派 `_locked`）
  - `precheck_dispatch_permit_locked(store: TaskStoreProtocol, task_id: str, role: str, *, config: Mapping[str, object]) -> None`——与 `require_dispatch_permit_locked` 相同的检查序列（声明/信封/violation/角色/激活/预检），但**只读**：不追加任何账本记录、不做生命周期与预算断言（这两个状态性检查的唯一权威是汇点单事务，避免只读预检与汇点之间的 TOCTOU 假象）；供早失败层使用
  - `precheck_dispatch_permit(store: TaskStoreProtocol, task_id: str, role: str, *, config: Mapping[str, object]) -> None`——自取锁包装（仅取锁委派 `_locked` 变体）
  - `release_permit_before_start_locked(store: TaskStoreProtocol, task_id: str, permit: DispatchPermit, *, reason: str) -> None`
  - `release_permit_before_start(store: TaskStoreProtocol, permit: DispatchPermit, *, reason: str) -> None`——自取锁包装（锁 `permit.task_id` 后委派）
  - `release_permit_if_never_spawned(store: TaskStoreProtocol, permit: DispatchPermit, *, spawned: bool, reason: str) -> None`——**锁外守卫 helper**：`spawned` 为真 → 直接返回（spawn/认领后永不释放）；`spawned` 为假 → 调自取锁包装 `release_permit_before_start(store, permit, reason=reason)`。本 helper 是 dispatch_policy 模块源码中 `release_permit_before_start(` 的**唯一直接调用点**（模块级静态扫描锁定）；执行汇点（`run_codex`/`run_assignment`）与 fake 分支只调本 helper，绝不直接调包装
  - `claim_permit_start_locked(store: TaskStoreProtocol, task_id: str, permit: DispatchPermit) -> None`——**只有 `_locked` 变体，无包装**（只存在于执行汇点与 fake 分支的临界区内）

`require_dispatch_permit_locked` 在第一行
`store._assert_lock_held(task_id)` 后顺序执行（即设计文档许可单事务
的步骤 1）：

1. 经 `load_route_declaration_locked`（**先恢复后加载**）取声明，缺失
   → `ROUTE_DECLARATION_MISSING`；
2. 锁内重读 `task.json` 与 `route-decision.json`，信封等式不成立 →
   `ROUTE_DECLARATION_MISMATCH`；
3. `has_unresolved_ownership_violation_locked(store, task_id)` 为真 →
   `DISPATCH_BLOCKED_OWNERSHIP_VIOLATION`（持久 violation 阻断一切
   后续派发；**只能**调 `_locked` 版本，静态扫描锁定本函数源码不含
   无后缀调用）；
4. `role` ∉ `allowed_roles` → `ROLE_NOT_ALLOWED`；
5. `role` ∉ `derive_active_roles(...)` → `ROUTE_TRANSITION_BLOCKED`
   （须先 `activate_role`）；
6. `require_role_preflighted_locked(store, task_id, role)`——预检
   context 由预检模块**内部重算**（本函数不接收、不转发任何外部
   context）；未命中 → `ROLE_NOT_PREFLIGHTED`；
7. **dispatch_id 生命周期（同 ID 再进一律拒绝，无幂等返回）**：
   `records = replay_permit_ledger(...)`；`permit_latest_states`
   中该 ID 状态为 `RESERVED` → `DISPATCH_PERMIT_UNCLAIMED`（崩溃
   孤儿或调用错误；每条派发只有一个真实预留点，正常流程不存在合法
   的同 ID 二次进入）；`STARTED` → `DISPATCH_PERMIT_ALREADY_STARTED`
   （启动已认领，不得再次启动）；`RELEASED_BEFORE_START` →
   `DISPATCH_IDENTITY_RETIRED`（永久退休，串行/并发重放同规）；
8. 预算：最新状态 ∈ {`RESERVED`, `STARTED`} 的许可数 ≥
   `max_dispatches` → `ROUTE_BUDGET_EXCEEDED`；
9. 追加 `RESERVED` 记录（`seq` = 账本当前行数 + 1）；`role ∈
   derive_effectful_roles(config)` → 同临界区
   `record_external_side_effect_locked(..., permit_id=dispatch_identity)`
   （EXTERNAL 在启动前接线）；
10. 返回 `DispatchPermit(permit_id=dispatch_identity, ...)`。

`claim_permit_start_locked`：第一行 `_assert_lock_held`；重放确认该
permit 最新状态为 `RESERVED` → 追加 `STARTED` 记录（`seq` 连续）；
状态为 `STARTED`/`RELEASED_BEFORE_START` →
`DISPATCH_PERMIT_STATE_ILLEGAL`。

`release_permit_before_start_locked`：第一行 `_assert_lock_held`；
重放确认最新状态为 `RESERVED` → 追加 `RELEASED_BEFORE_START`（含
`reason`）；状态为 `STARTED` → `DISPATCH_PERMIT_STATE_ILLEGAL`
（**spawn 后永不释放**）；`RELEASED_BEFORE_START` →
`DISPATCH_IDENTITY_RETIRED`。

预算口径（显式）：每次执行器启动消耗一个许可；技术重试是新
attempt_id、新 dispatch_id、消耗新许可；声明 `max_dispatches` 的推导
已计入每角色 `1 + 技术重试上限`；`RELEASED_BEFORE_START` 释放额度供
新 attempt_id 的重试使用。许可单事务的完整步骤顺序（含 ownership
lease、launch intent、spawn、claim 与崩溃语义）按设计文档「许可状态
机、单事务步骤顺序与崩溃语义」节执行——reservation（本函数）、
EXTERNAL（步骤 9）、ownership lease 与启动认领由 Task 13 的执行汇点
在同一 `store.lock` 临界区内依次组合完成。

- [ ] **Step 1: 写许可门的失败测试**

缺声明 → `ROUTE_DECLARATION_MISSING`；信封被篡改 →
`ROUTE_DECLARATION_MISMATCH`；存在持久 violation →
`DISPATCH_BLOCKED_OWNERSHIP_VIOLATION`（**完整时序**：先成功预留并
`claim_permit_start_locked` 认领一次 → 经 Task 09 接口落 violation
事件 → 后续所有 `require_dispatch_permit_locked` 与自取锁包装
`require_dispatch_permit` 调用均拒绝）；角色越权 →
`ROLE_NOT_ALLOWED`；未激活角色 → `ROUTE_TRANSITION_BLOCKED`；未预检
→ `ROLE_NOT_PREFLIGHTED`；预算满 → `ROUTE_BUDGET_EXCEEDED`；合法路径
返回许可且账本追加 `RESERVED`（`seq` 从 1 连续递增）。

- [ ] **Step 2: 写许可状态机与 dispatch_id 永久退休的失败测试**

同一 `dispatch_identity` 预留后 `claim_permit_start_locked` 认领 →
同 ID 再进 `require_dispatch_permit_locked` →
`DISPATCH_PERMIT_ALREADY_STARTED`（**不得**幂等返回既有许可再启动）；
认领后同 ID 的第二次 spawn 通路在许可门即被拒（结合 Task 13 的
Popen spy 断言调用计数不增长，本卡先锁定门控语义）；
`release_permit_before_start_locked` 后**串行**重放同 ID →
`DISPATCH_IDENTITY_RETIRED`；释放后**并发**双线程重放同 ID → 两者
均 `DISPATCH_IDENTITY_RETIRED`（锁串行化后逐一拒绝）；`RESERVED`
未认领状态下同 ID 再进 → `DISPATCH_PERMIT_UNCLAIMED`；对已
`STARTED` 的许可调 `release_permit_before_start_locked` →
`DISPATCH_PERMIT_STATE_ILLEGAL`（spawn 后永不释放）；对已释放许可
再 release → `DISPATCH_IDENTITY_RETIRED`；对非 `RESERVED` 状态调
claim → `DISPATCH_PERMIT_STATE_ILLEGAL`；释放后以新 `attempt_id`
推导的新 `dispatch_identity` → 正常授权。

- [ ] **Step 3: 写锁纪律与传递调用图的失败测试**

锁外调 `require_dispatch_permit_locked` /
`precheck_dispatch_permit_locked` /
`release_permit_before_start_locked` / `claim_permit_start_locked`
→ `LOCK_REQUIRED`；已持锁路径调自取锁包装
`require_dispatch_permit` / `precheck_dispatch_permit` /
`release_permit_before_start` → `TASK_ALREADY_RUNNING`（嵌套锁被
结构消除）；`inspect.getsource` 断言三个包装函数体仅含
`with store.lock(...)` 与委派调用；`claim_permit_start_locked` 不存在
同名无后缀包装（`hasattr(module, "claim_permit_start")` 为假）；
`inspect.getsource(require_dispatch_permit_locked)` 断言调用
`has_unresolved_ownership_violation_locked(` 且**不含**
`has_unresolved_ownership_violation(` 的无后缀调用形式；
**释放守卫 helper**：`release_permit_if_never_spawned` 在
`spawned=True` 时零释放（spy 断言 `release_permit_before_start`
未被调用）、`spawned=False` 时恰好调包装一次、锁内调用 →
`TASK_ALREADY_RUNNING`（证明只能在锁外使用）；dispatch_policy 模块
级扫描断言 `release_permit_before_start(` 的唯一直接调用点在
`release_permit_if_never_spawned` 函数体内；**传递调用图 AST 检查**：
从 `require_dispatch_permit_locked` 与
`require_write_ownership_locked`（ownership 模块）出发，沿新业务模块
（dispatch_policy/ownership/authorizations/declarations/preflight/
side_effects）内调用边遍历可达闭包，断言闭包内不含任何自取锁包装
（包装按「函数体仅含 `with store.lock(...)` 与委派」结构特征自动
识别）——持锁路径进入自取锁包装在结构上不可能。

- [ ] **Step 4: 写并发预算与 EXTERNAL 接线的失败测试**

并发线程对同一任务抢许可（线程数 > `max_dispatches`）只有预算内数量
成功，其余 `ROUTE_BUDGET_EXCEEDED`，无 TOCTOU 超发；effectful 角色
（workspace-write）授权成功后 `side-effects.jsonl` 同锁出现
`EXTERNAL` 记录且携带 `permit_id`；只读角色授权不产生 `EXTERNAL`。

- [ ] **Step 5: 写顺序、激活与账本重放的失败测试**

声明先于任何 `RESERVED`（账本序断言，不读任何 UTC 字段——静态扫描
断言模块无 `declared_at_utc` 比较）；`activate_role` 校验转换图闭集，
非法转换 → `ROUTE_TRANSITION_BLOCKED`；激活后
`derive_active_roles` 包含新角色；重放与上一派发角色无关（构造上一
派发为其他角色但无激活事件的场景仍 `ROUTE_TRANSITION_BLOCKED`）；
许可账本截断尾记录/重复 `seq`/`seq` 断档/非法状态转换（同 ID 两次
`RELEASED_BEFORE_START`、`STARTED` 后再有同 ID 记录、同 ID 两条
`RESERVED`）/跨任务混入 → `DISPATCH_PERMIT_LEDGER_CORRUPT`；
`precheck_dispatch_permit_locked` 全程不写账本（前后账本字节一致）
且对合法角色通过、对越权角色早失败。

- [ ] **Step 6: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_dispatch_policy -v
```

Expected: 新测试失败（模块尚不存在）。

- [ ] **Step 7: 最小实现**

新建 `scripts/ai_workflow_dispatch_policy.py`；模块级只 import
declarations/preflight/routing/ownership/side_effects/artifacts（依赖
方向按设计文档，import-graph 测试锁定）。同步
`scripts/sync_plugin.py`。

- [ ] **Step 8: Verify GREEN + 基线复核**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_dispatch_policy \
  tests.test_ai_workflow_distribution \
  tests.test_ai_workflow_import_graph \
  tests.test_ai_workflow_baseline_manifest -v
```

Expected: 全部通过。

- [ ] **Step 9: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(dispatch-policy): add permit state machine primitives with single-transaction locked variants"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_dispatch_policy tests.test_ai_workflow_distribution tests.test_ai_workflow_import_graph tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；许可状态机负向测试（认领后再进拒、释放后退休、
未认领再进拒、spawn 后永不释放）、violation 后所有后续 permit 拒绝、
传递调用图 AST 检查、释放守卫 helper 三态测试全绿；输出
`PLUGIN_SYNC_OK`。

---

### Task 13: P0-1c 派发门控全路径接线与 legacy 规则（两个执行汇点单事务、spawn 证明、认领与释放语义）

**依赖:** 07、09、12（必须先合入，禁止与本卡并行改 `scripts/ai_workflow.py`）

**分支:**

```bash
git worktree add ../wt-sol-adopt-13-dispatch-gate -b feat/sol-adopt-13-dispatch-gate
cd ../wt-sol-adopt-13-dispatch-gate
```

**Files:**

- Modify: `scripts/ai_workflow.py`（执行汇点 1 单事务与入口接线，见下）
- Modify: `scripts/ai_workflow_repairs.py`（`run_assignment` 子进程启动前接执行汇点 2 单事务）
- Modify: `scripts/ai_workflow_scheduler.py`（`dispatch_ready_batch` 记录提案前逐任务校验声明与角色闭集）
- Modify: `tests/test_ai_workflow.py`
- Modify: `tests/test_ai_workflow_declarations.py`
- Modify: `tests/test_ai_workflow_dispatch_policy.py`
- Modify: `tests/test_ai_workflow_team_call.py`
- Modify: `tests/test_ai_workflow_scheduler.py`
- Modify: `tests/test_ai_workflow_repairs.py`
- Modify: `tests/test_ai_workflow_construction_execution.py`
- Modify: `tests/test_ai_workflow_import_graph.py`（允许边表增 `repairs → dispatch_policy`）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/runtime/ai_workflow.py`、`plugins/ai-workflow/runtime/ai_workflow_repairs.py`、`plugins/ai-workflow/runtime/ai_workflow_scheduler.py`

**Interfaces:**

- Consumes: Task 12 全部 Produces（`require_dispatch_permit_locked`、
  `precheck_dispatch_permit`、`release_permit_before_start`、
  `release_permit_if_never_spawned`、`claim_permit_start_locked`、
  `derive_dispatch_identity`、
  `derive_assignment_dispatch_identity`、`ensure_declaration_for_task`、
  `activate_role`）；Task 09 的 `require_write_ownership_locked`、
  `precheck_write_ownership`、`verify_actual_write_paths`、
  `claimed_write_paths`；Task 07 的 `observe_execution_side_effects`
  返回值；Task 02 的 `load_route_declaration`；
  `scripts/ai_workflow.py` 的 `run_codex`（`:1656`）、`_run_live_luna`
  （`:6267`）、`_run_trusted_team_call_l1`（`:4289`）、
  `_run_trusted_team_call_l0`（`:4276`）、`run_team_call`（`:4307`）、
  `run_until_gate`（`:5692`）、`run_enforced_construction`（`:5535`）、
  `_run_role_with_technical_retry`（`:4804`）、
  `persist_or_reuse_route_decision`（`:2650`）、`decide_route`
  （`:2618`）、`_resume_stored_task`（`:6087`）、CLI `route` 处理器
  （`:6553`）；`scripts/ai_workflow_repairs.py` 的 `run_assignment`
  （`:2802`）、`AcceptanceAssignment.allowed_paths`（`:1093`）；
  `scripts/ai_workflow_scheduler.py` 的 `dispatch_ready_batch`（`:854`）。
- Produces:（无新模块；接线语义）

**许可单事务（设计文档「许可状态机、单事务步骤顺序与崩溃语义」节的
接线落点；reservation、EXTERNAL、ownership lease、启动认领四者在同一
段 `store.lock(task_id)` 临界区完成）**：

  - 执行汇点 1（`run_codex`，`:1656`）：临界区自
    `_require_attempt_accounting_context` 调用点（`:1703`）之后开始——
    `paths.state_root` 为 None → `ROUTE_DECLARATION_MISSING`；否则
    `with store.lock(task_id):` 依次调
    `require_dispatch_permit_locked(store, task_id, role,
    dispatch_identity=derive_dispatch_identity(task_sha256=
    artifact_sha256(task), role=role, attempt_id=
    accounting_context.attempt_id), config=...)`（步骤 1，含全部检查、
    `RESERVED` 追加与 effectful 角色的 `EXTERNAL` 接线）→ 写类角色
    （`TERRA_WRITE_ROLES`）同临界区
    `require_write_ownership_locked(store, task_id, role,
    permit_id=permit.permit_id, paths=claimed_write_paths(<该角色冻结
    计划/施工上下文写作用域>))`（步骤 2）→（步骤 3 的
    `LAUNCH_INTENT_RECORDED` 由 Task 18 插入同一临界区 spawn 之前，
    本卡在临界区内预留扩展点注释）→ 完成全部可失败的 spawn 前置准备
    （既有 `_claim_attempt_context`（`:1721` 调用点）、schema
    materialize、输出路径新鲜性、runtime sessions 目录校验；临界区内
    不得出现任何自取锁调用——`append_event`、`_claim_attempt_context`
    均为无锁实现（`:2467`、`:967`），如既有辅助另有自取锁则本卡为其
    补 `_locked` 变体或移出临界区）→ 既有 `subprocess.run`（`:1785`）
    拆为 `proc = subprocess.Popen(...)`（步骤 5，不可返回点）→
    成功返回后立即 `claim_permit_start_locked(store, task_id, permit)`
    （步骤 6，claim 与 Popen 之间无任何其他语句）→ 出锁 →
    `proc.communicate(input=..., timeout=...)` 在锁外等待（语义等价：
    `subprocess.run` 本就是 Popen+communicate 的组合）
  - 执行汇点 2（`run_assignment`，`:2802`）：第二段
    `store.lock(task_id)`（`:2864` 区域）扩展为同一事务结构——
    `_v2_start_attempt` 之后依次调 `require_dispatch_permit_locked(
    store, task_id, assignment.expected_actor.role,
    dispatch_identity=derive_assignment_dispatch_identity(
    task_sha256=..., assignment_id=assignment.assignment_id,
    attempt_id=assignment.attempt_id), config=...)` → 同临界区
    `require_write_ownership_locked(..., permit_id=permit.permit_id,
    paths=claimed_write_paths(assignment.allowed_paths))` →（Task 18
    意图事件扩展点）→ 既有 spawn 前置准备（codex 可执行文件解析、
    schema materialize）→ `:2914` 处 `subprocess.run` 同样拆为
    Popen（锁内）+ communicate（锁外）→ spawn 成功后立即
    `claim_permit_start_locked`
  - spawn 证明与释放（两个汇点同构）：临界区与 spawn 包
    try/except——`proc` 未创建（异常发生在 Popen 之前或 Popen 抛错）
    且 `permit` 已预留 → 锁外守卫调
    `release_permit_if_never_spawned(store, permit,
    spawned=proc is not None, reason=...)` 后原样抛出（helper 内部
    才调自取锁包装 `release_permit_before_start`）；`proc` 已创建
    （spawn 标记置位）后任何异常/超时/非零退出/崩溃 → helper 零动作、
    **不得**释放（许可恒为 `STARTED` 终态；
    `UNOBSERVED_ASSUMED_PRESENT` 由 Task 07 挂钩记录）；
    `claim_permit_start_locked` 自身抛错而 `proc` 已存在 → 立即
    `proc.kill()`、不释放、原样抛出
  - fake 分支单事务：`_run_role_with_technical_retry`（`:4804`）的
    `getattr(runner, "is_live_model", False)` 为假的分支（既有分支点
    `:4843`），在 `runner.run(...)` 调用前以同一事务结构完成：
    `with store.lock(task_id):` → `require_dispatch_permit_locked`
    （`dispatch_identity = derive_dispatch_identity(task_sha256=...,
    role=role, attempt_id=<attempt 区分符>)`，attempt 区分符 =
    `attempt_context.attempt_id`（runner 拥有记账上下文时）否则
    `f"{retry_kind}:{循环序号}"`——技术重试必然得到新 dispatch_id）
    → 写类角色 `require_write_ownership_locked` →
    `claim_permit_start_locked`（fake 无 OS spawn，runner 调用即视为
    启动，认领在调用前同一临界区完成）→ 出锁 → `runner.run(...)`；
    临界区包 try/except：claim 完成前任何异常（含 ownership 拒绝）→
    锁外守卫 `release_permit_if_never_spawned(..., spawned=False,
    reason=...)` 释放（fake 的 spawn 标记 = claim 完成）；claim 完成
    后 runner 调用抛任何异常 → 许可保持 `STARTED`，不得释放
  - 早失败层（只读）：`_run_role_with_technical_retry` 的 live 分支在
    进入 runner 调用前调 `precheck_dispatch_permit(store, task_id,
    role, config=...)`（只读，不写账本、不断言生命周期与预算——两者
    的唯一权威是汇点单事务）与 `precheck_write_ownership`（只读，不
    消费 lease）；早失败层不是安全边界
  - 执行后复核：`run_codex` 与 `run_assignment` 把
    `observe_execution_side_effects` 返回的变更集路径传给
    `verify_actual_write_paths(..., permit_id=permit.permit_id,
    actual_paths=...)`（**绑定本次 permit**，历史 lease 不豁免）；
    实际路径未知（异常且观测缺失）→ 不得放行完成路径
  - 唯一创建阶段接线：CLI `route` 处理器在
    `persist_or_reuse_route_decision` 之后调
    `ensure_declaration_for_task`；`run_until_gate` 在首个
    `_run_pipeline_role` 之前（锁内）调；`run_enforced_construction`
    入口调；`run_team_call` DIRECT_L1 在 `store.create_task` 之后、
    `_run_trusted_team_call_l1` 之前调（先 `decide_route` +
    `persist_or_reuse_route_decision` 冻结决定）；`_run_live_luna` 在
    `run_codex` 之前调；`resume`（`_resume_stored_task`）两分支沿用
    所属入口——`ensure_declaration_for_task` 与
    `load_route_declaration` 内部均先经
    `recover_route_declaration_event` 恢复（Task 02 接口），resume
    路径因此必然先完成崩溃恢复再继续派发
  - 汇点锁纪律静态断言（**禁止**对整个 `run_codex` /
    `run_assignment` 源码做 `release_permit_before_start` 名称不存在
    检查；`inspect.getsource` 只许做正向存在检查，不许做该包装名的
    整函数缺失断言）：
    1. AST 定位两个执行汇点中的 `with store.lock(...)` 语法块；**仅在
       这些持锁语法块内**禁止所有自取锁 wrapper（冻结包装名清单：
       `require_dispatch_permit`、`precheck_dispatch_permit`、
       `release_permit_before_start`、`consume_owner_authorization`、
       `has_unresolved_ownership_violation`、`run_role_preflight`、
       `is_role_preflighted`、`require_role_preflighted`、
       `load_route_declaration`、`require_verdict_fresh`）
    2. AST/调用图确认两个执行汇点只调用
       `release_permit_if_never_spawned`，不直接调用
       `release_permit_before_start`
    3. 确认 `release_permit_if_never_spawned` 是
       `release_permit_before_start` 的唯一直接调用者（Task 12 模块级
       扫描锁定）
    4. 确认 helper 仅在 `spawned=False` 分支调用该 wrapper
    5. `inspect.getsource(run_codex)` 与
       `inspect.getsource(run_assignment)` **仅**断言含
       `require_dispatch_permit_locked(`、`claim_permit_start_locked(`
       与 `release_permit_if_never_spawned(`（正向存在，不是缺失检查）
    6. 两函数正常路径不出现 `TASK_ALREADY_RUNNING`

完整派发调用图（设计文档 P0-1 表格为本卡契约）：八条路径逐一标注门控
位置；DIRECT_L0 为固定 `L0_FIXED_ARGV` 闭集
（`scripts/ai_workflow_team_call.py:74`）、无任务、无模型，以负向测试
证明其永远到不了 `run_codex`，若未来触达模型执行则执行汇点必然
fail-closed；`schedule-batch` 只写提案不启动模型，但记录提案前逐任务
校验声明存在且计划角色 ⊆ `allowed_roles`，禁止 batch 级声明替代逐任务
声明。

legacy 规则：新建 legacy-mode 任务先 `decide_route(mode="legacy")` 并经
`persist_or_reuse_route_decision` 冻结，再派生声明；已有历史任务（无
`route-decision.json` 且账本非空）派发时 fail-closed
（`ROUTE_DECLARATION_MISSING`/`ROUTE_DECLARATION_LATE`），本轮不提供
迁移工具（进 backlog）；依赖无声明直调的旧测试 fixture 改走正式创建
流程，不加隐式兼容旁路。

- [ ] **Step 1: 写执行汇点 1 的失败测试**

`tests/test_ai_workflow.py`：未建声明直接到达 `run_codex` 的任务被拒
（`ROUTE_DECLARATION_MISSING`）且 executor 未被调用（spy 断言子进程
函数调用次数为零）、`dispatches.jsonl` 与 `dispatch-permits.jsonl`
无新增；合法声明后放行且账本依次出现 `RESERVED` → `STARTED`（同一
`permit_id`，`seq` 连续）。`paths.state_root=None` 的直调 →
`ROUTE_DECLARATION_MISSING`。汇点临界区静态断言（上述 AST 持锁块范围
检查、helper 唯一直接调用者、`spawned=False` 才释放，以及 getsource
正向存在检查）通过；**不得**对两汇点整函数源码断言不含
`release_permit_before_start`。

- [ ] **Step 2: 写 spawn 证明、认领与释放的失败测试**

许可预留后、spawn 前注入异常（如 schema materialize 失败 stub）→
账本出现 `RELEASED_BEFORE_START`（锁外守卫经
`release_permit_if_never_spawned` 释放；spy 断言 helper 被调且
`spawned=False`），同 `dispatch_identity` 重放 →
`DISPATCH_IDENTITY_RETIRED`；spawn 标记置位后注入超时 → 无释放记录、
许可保持 `STARTED`、`UNOBSERVED_ASSUMED_PRESENT` 落账（spy 断言
helper 被调且 `spawned=True` 时零释放）；claim 追加注入失败
（monkeypatch 使其抛错）且 Popen 已成功 → 子进程被 kill、无释放
记录、异常原样传播；技术重试（新 attempt_id）→ 新许可正常预留；
**认领后同 ID 再启动被拒**：构造同 `dispatch_identity` 的二次进入
→ `DISPATCH_PERMIT_ALREADY_STARTED`，Popen spy 断言调用计数不增长
（再 spawn 通路被门控切断）。

- [ ] **Step 3: 写八条路径的失败测试**

逐路径：fake runner 路径（`run_until_gate`）缺声明拒发、合法声明后
账本出现 `RESERVED` → `STARTED`（fake 分支单事务）且 runner 调用
抛错后许可保持 `STARTED` 无释放；`_run_live_luna` 缺声明拒发且
`run_codex` 未被调用；construction 路径（`run_enforced_construction`）
缺声明拒发；`resume` 两分支同（且 resume 入口在声明崩溃窗口下先
补记 `ROUTE_DECLARED` 再继续）；team-call DIRECT_L1 建任务后自动有
声明、删除声明后拒发；DIRECT_L0 spy 断言不到达 `run_codex` 且不创建
任务目录；`schedule-batch` 对无声明任务拒绝记录提案、对计划角色越出
`allowed_roles` 拒绝；`run_assignment` 缺声明在 spawn 前拒发。每路径
另有：角色越权拒发、未预检拒发、预算超额拒发、executor 调用计数为
零。

- [ ] **Step 4: 写所有权接线的失败测试**

写类角色在执行汇点内：声称路径越出登记器名下且无授权 →
`OWNERSHIP_TRANSFER_BLOCKED` 且 executor 调用计数为零；持聚焦授权 →
放行且 lease 落账绑定本次 `permit_id`；执行后实际写路径越界 →
`OWNERSHIP_VIOLATION_RECORDED` 事件落账于 `events.jsonl`（含本次
`permit_id`；`side-effects.jsonl` 无 violation 账本项）且该任务后续
所有派发 → `DISPATCH_BLOCKED_OWNERSHIP_VIOLATION`；**历史 lease
不豁免接线证据**：permit_A 的 lease 覆盖路径 X 的派发完成后，另一笔
permit_B（无 lease）派发实际写 X → violation 落账；
`run_assignment` 的 `actual_changed_paths` 越出
`assignment.allowed_paths` 与本次 permit 的 lease 闭集 → 同规。

- [ ] **Step 5: 写 legacy 与防绕过的失败测试**

legacy-mode 新建任务：声明 `selected_route` 等于
`decide_route(task, request, mode="legacy", legacy_router=...)`
的推导结果且先有冻结 route decision；历史 fixture（手工造的任务目录
+ 非空账本、无 route-decision.json）派发 → fail-closed，不产生任何
自动补造的声明文件；删除声明文件后再次派发仍
`ROUTE_DECLARATION_MISSING`，不是静默补写放行；崩溃窗口场景（声明
文件在、`ROUTE_DECLARED` 事件缺失）→ 入口锁内恢复补记事件且声明
字节不变。

- [ ] **Step 6: Verify RED**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow \
  tests.test_ai_workflow_dispatch_policy \
  tests.test_ai_workflow_team_call \
  tests.test_ai_workflow_scheduler \
  tests.test_ai_workflow_repairs \
  tests.test_ai_workflow_construction_execution -v
```

Expected: 新增门控测试失败（执行汇点尚未接线）。

- [ ] **Step 7: 最小实现**

按 Interfaces 逐点接线；入口建声明是显式代码路径（唯一创建阶段），
不是门控的兜底补写；门控只验证不补写。旧 fixture 批量改为正式创建
流程。同步 import-graph 允许边（`repairs → dispatch_policy`）。

- [ ] **Step 8: Verify GREEN + 全量回归 + 基线复核**

```bash
python3.11 -m unittest discover -s tests
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
```

Expected: 基线清单用例保持通过，新增全绿。

- [ ] **Step 9: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(dispatch-policy): gate all dispatch paths with single-transaction permits at run_codex and run_assignment sinks"
```

**验收标准:**

```bash
python3.11 -m unittest discover -s tests
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 八条路径门控、spawn 证明、认领与释放语义（锁外释放只经
helper 且 `spawned` 语义正确）、退休重放、所有权接线（含历史 lease
不豁免与 violation 事件唯一权威来源）、AST 范围静态断言测试全绿；
全量 0 失败；输出 `PLUGIN_SYNC_OK`。

---

### Task 14: P1-1a 按路由预检与任务内多键缓存（PreflightContext 全权威重算、安全入口内部重算）

**依赖:** 02

**分支:**

```bash
git worktree add ../wt-sol-adopt-14-preflight -b feat/sol-adopt-14-preflight
cd ../wt-sol-adopt-14-preflight
```

**Files:**

- Create: `config/ai_workflow_preflight_record.schema.json`
- Create: `scripts/ai_workflow_preflight.py`
- Create: `tests/test_ai_workflow_preflight.py`
- Modify: `scripts/sync_plugin.py`（`CONFIG_FILES` 增 `ai_workflow_preflight_record.schema.json` 与生成的 `ai_workflow_runtime_files.json`；`RUNTIME_FILES` 增 `ai_workflow_preflight.py`；`--write` 重新生成分发 manifest，`--check` 重算比对）
- Modify: `tests/test_ai_workflow_distribution.py`
- Modify: `tests/test_ai_workflow_import_graph.py`（允许边表增 preflight 行）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/config/ai_workflow_preflight_record.schema.json`、`plugins/ai-workflow/config/ai_workflow_runtime_files.json`、`plugins/ai-workflow/runtime/ai_workflow_preflight.py`

**Interfaces:**

- Consumes: `scripts/ai_workflow_artifacts.py` 的 `ROLES`、
  `WorkflowError`、`TaskStoreProtocol`、`PROCESS_GENERATION`；
  `scripts/ai_workflow_declarations.py` 的
  `load_route_declaration_locked`（模块级 import——依赖方向
  preflight → declarations，无环，由 import-graph 测试锁定）。
  **禁止** import `ai_workflow`、`ai_workflow_repairs`、`sync_plugin`。
- Produces:
  - `PREFLIGHT_RECORD_SCHEMA_VERSION = "ai-preflight-record-1"`
  - `LAUNCHER_VERSION = "ai-workflow-launcher-1"`
  - `RUNTIME_MANIFEST_FILENAME = "ai_workflow_runtime_files.json"`（分发 manifest 数据文件：`{"schema_version": "ai-runtime-files-1", "files": [{"name", "sha256"}...], "aggregate_sha256"}`，由 `sync_plugin.py --write` 从 `RUNTIME_FILES` 实际内容生成，`--check` 重算比对）
  - `@dataclass(frozen=True) class PreflightContext`（`task_id`、`route_config_hash`、`runtime_profile_hash`、`install_version`、`launcher_version`、`cwd`、`worktree_id`、`process_generation`），含 `cache_key() -> str`
  - `compute_runtime_profile_hash(role_config: Mapping[str, object]) -> str`（对角色钉死字段 model/reasoning_effort/sandbox/permission 派生档规范化哈希）
  - `compute_install_version() -> str`——经 `Path(__file__).resolve()` 定位安装根（仓库 `scripts/` 或插件 `runtime/` 两种布局向上各退一层），读取 `config/ai_workflow_runtime_files.json` 的 `aggregate_sha256`；文件缺失或畸形 → `INSTALL_MANIFEST_UNAVAILABLE`
  - `compute_preflight_context(store: TaskStoreProtocol, task_id: str, *, role: str) -> PreflightContext`——第一行 `store._assert_lock_held(task_id)`；**零调用方可控因子**：`route_config_hash` 经 `load_route_declaration_locked` 读取已存声明（含崩溃恢复；返回 None → `ROUTE_DECLARATION_MISSING`）；`runtime_profile_hash` 从安装根 `config/ai_workflow.toml`（tomllib 解析）该角色钉死字段计算；`install_version` 取 `compute_install_version()`；`launcher_version` 为模块常量；`cwd = os.getcwd()`；`worktree_id` 从任务信封仓库路径 `git rev-parse --show-toplevel` 解析；`process_generation = PROCESS_GENERATION`。签名用 `inspect.signature` 内省测试钉死：无 `route_config_hash`/`root`/`process_generation` 等形参
  - `_run_preflight_checks(role: str, context: PreflightContext) -> Mapping[str, object]`——**模块私有纯函数**（下划线前缀、无 store 形参、不做任何 I/O）：宿主静态检查（角色 ∈ `ROLES`、角色配置钉死 model/effort/sandbox/permission、runtime sessions 要求、schema 文件齐备；不调用模型，无 executor 参数）；只供下列公开入口内部调用与测试直调
  - `_preflight_record_matches(records: tuple[dict[str, object], ...], role: str, cache_key: str) -> bool`——**模块私有纯函数**（同上约束）
  - `run_role_preflight_locked(store: TaskStoreProtocol, task_id: str, role: str) -> Mapping[str, object]`——第一行 `_assert_lock_held`；**内部** `context = compute_preflight_context(store, task_id, role=role)` → `_run_preflight_checks(role, context)` → 追加 `preflight-records.jsonl` 记录（含 `cache_key`）→ 返回结果；**签名无 `context` 形参**
  - `run_role_preflight(store: TaskStoreProtocol, task_id: str, role: str) -> Mapping[str, object]`——自取锁包装（仅取锁委派 `run_role_preflight_locked`）
  - `is_role_preflighted_locked(store: TaskStoreProtocol, task_id: str, role: str) -> bool`——第一行 `_assert_lock_held`；**内部重算当前 context** 后以 `_preflight_record_matches` 匹配账本；签名无 `context` 形参
  - `is_role_preflighted(store: TaskStoreProtocol, task_id: str, role: str) -> bool`——自取锁包装（仅取锁委派 `is_role_preflighted_locked`）
  - `require_role_preflighted_locked(store: TaskStoreProtocol, task_id: str, role: str) -> None`——第一行 `_assert_lock_held`；内部重算并匹配，未命中 → `ROLE_NOT_PREFLIGHTED`；签名无 `context` 形参
  - `require_role_preflighted(store: TaskStoreProtocol, task_id: str, role: str) -> None`——自取锁包装（仅取锁委派 `require_role_preflighted_locked`）

缓存记录写任务目录 `preflight-records.jsonl`（append-only，**无
seq**，重放 fail-closed：截断尾记录、非对象行、跨任务 →
`PREFLIGHT_LEDGER_CORRUPT`，不做「重复 seq」检查；支持同一任务多角色、
多 key 版本；键失配即视为未预检，重检追加新记录，不覆盖；按行序取
最新匹配）。任一上下文因子变化即不命中；缓存不跨任务。

- [ ] **Step 1: 写静态检查的失败测试**

未知角色 → 拒绝；角色配置缺钉死字段（stub 配置）→ 记录 `FAIL`；
合法角色 → 记录 `PASS` 且记录含 `cache_key`；静态扫描断言模块无
executor/模型调用参数与相关 import；import-graph 测试断言 preflight
不 import `ai_workflow`/`sync_plugin`。

- [ ] **Step 2: 写缓存键的失败测试**

同 context 第二次 `is_role_preflighted` 命中且不重跑检查（
`_run_preflight_checks` 调用计数断言）；`route_config_hash`、
`runtime_profile_hash`、`install_version`、`launcher_version`、`cwd`、
`worktree_id`、`process_generation` 任一变化 → 不命中；换任务目录
（同键不同任务）→ 不命中（缓存不跨任务）；同任务两个角色各有独立
记录、互不命中；失效后重检追加新记录、旧记录字节不变。

- [ ] **Step 3: 写上下文权威性与安全入口内部重算的失败测试**

声明中 `route_config_hash` 为 A 时，调用方环境即使「以为」是 B，
context 仍取 A（篡改声明文件后重算 → 哈希跟随声明变化）；
`inspect.signature` 断言 `compute_preflight_context` 无形参可传
route_config_hash/root/process_generation，且 `run_role_preflight` /
`run_role_preflight_locked` / `is_role_preflighted` /
`is_role_preflighted_locked` / `require_role_preflighted` /
`require_role_preflighted_locked` **均无 `context` 形参**（调用者
无法注入构造的 PreflightContext）；模块源码静态扫描：六个公开入口
内部都调 `compute_preflight_context`；`_run_preflight_checks` 与
`_preflight_record_matches` 为模块私有（下划线前缀）且无 `store`
形参；删除 `config/ai_workflow_runtime_files.json` →
`INSTALL_MANIFEST_UNAVAILABLE`；`sync_plugin.py --write` 后 manifest
内容与 `RUNTIME_FILES` 实际文件 sha256 逐条一致（对拍），手改任一
runtime 文件后 `--check` 失败；锁外调任一 `_locked` 变体 →
`LOCK_REQUIRED`。

- [ ] **Step 4: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_preflight -v
```

Expected: 新测试失败（模块尚不存在）。

- [ ] **Step 5: 最小实现**

新建模块与 schema；`sync_plugin.py` 增加分发 manifest 生成/校验；
同步清单与 import-graph 允许边。

- [ ] **Step 6: Verify GREEN + 基线复核**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_preflight \
  tests.test_ai_workflow_distribution \
  tests.test_ai_workflow_import_graph \
  tests.test_ai_workflow_baseline_manifest -v
```

Expected: 全部通过。

- [ ] **Step 7: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(preflight): add host-static preflight with internally recaptured context at every safety entry"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_preflight tests.test_ai_workflow_distribution tests.test_ai_workflow_import_graph tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；安全入口无 context 形参的内省证据齐备；输出
`PLUGIN_SYNC_OK`。

---

### Task 15: P1-1b 预检生产接入与升级补预检

**依赖:** 13、14

**分支:**

```bash
git worktree add ../wt-sol-adopt-15-preflight-wiring -b feat/sol-adopt-15-preflight-wiring
cd ../wt-sol-adopt-15-preflight-wiring
```

**Files:**

- Modify: `scripts/ai_workflow.py`（入口编排：建声明后对 `active_roles` 显式预检；升级路径 `activate_role` → `run_role_preflight` → 许可）
- Modify: `scripts/ai_workflow_dispatch_policy.py`（如 Step 暴露编排缺口，只允许加编排函数，禁止把预检调用移入 `require_dispatch_permit_locked`）
- Modify: `tests/test_ai_workflow_preflight.py`
- Modify: `tests/test_ai_workflow.py`
- Modify: `tests/test_ai_workflow_scheduler.py`
- Modify: `tests/test_ai_workflow_team_call.py`
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/runtime/ai_workflow.py`、`plugins/ai-workflow/runtime/ai_workflow_dispatch_policy.py`

**Interfaces:**

- Consumes: Task 12、13、14 全部 Produces；
  `scripts/ai_workflow_runtime.py` 的
  `verify_runtime_identity(requested, observed) -> RuntimeEvidence`
  （`:412`）。
- Produces:
  - 生产编排顺序（接进 `run_until_gate`、`run_enforced_construction`、
    `run_team_call` DIRECT_L1、`_run_live_luna`）：冻结 route decision
    → `ensure_declaration_for_task` → 仅对声明 `active_roles` 调
    `run_role_preflight(store, task_id, role)`（**无 context 传参**；
    context 由预检模块内部现场重算）→ 进入派发循环（每次派发经
    `require_dispatch_permit_locked`，其内部只验证预检命中，不隐式
    补做）
  - 升级编排：合法转换先 `activate_role`，再对新激活角色
    `run_role_preflight(store, task_id, role)`，再许可派发

- [ ] **Step 1: 写初始预检接入的失败测试**

`run_until_gate` 正常路径：事件流中 `active_roles` 各角色的预检记录
早于其首条 `RESERVED`；`allowed_roles` 内但未激活的角色**没有**预检
记录（裁剪断言）；声明含角色但未预检（手工删除预检记录）→ 派发被
拒 `ROLE_NOT_PREFLIGHTED`，`dispatch-permits.jsonl` 不增长。

- [ ] **Step 2: 写升级补预检的失败测试**

按 `allowed_transitions` 从角色 A 激活角色 B：事件流中先有
`ROLE_ACTIVATED`、再有 B 的预检记录、再有 B 的首条 `RESERVED`（账本
序断言）；跳过补预检直接派发 B → `ROLE_NOT_PREFLIGHTED`；
`activate_role` 非法转换 → `ROUTE_TRANSITION_BLOCKED`。

- [ ] **Step 3: 写预检不替代身份验收的负向测试**

预检缓存命中的派发中，以 spy 包裹
`scripts/ai_workflow_runtime.verify_runtime_identity`，断言每次实际
rollout 仍恰好调用一次；预检记录存在与否不影响 S3/S4 的调用次数。

- [ ] **Step 4: Verify RED**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_preflight \
  tests.test_ai_workflow \
  tests.test_ai_workflow_scheduler \
  tests.test_ai_workflow_team_call -v
```

Expected: 新增编排与负向测试失败。

- [ ] **Step 5: 最小实现**

入口按上序接线；授权函数保持只验证。

- [ ] **Step 6: Verify GREEN + 全量回归 + 基线复核**

```bash
python3.11 -m unittest discover -s tests
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
```

Expected: 基线清单用例保持通过，新增全绿。

- [ ] **Step 7: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(preflight): wire initial active-role preflight and upgrade re-preflight into entries"
```

**验收标准:**

```bash
python3.11 -m unittest discover -s tests
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 拒发、补预检、身份验收不被跳过均有测试证据；全量 0 失败；
输出 `PLUGIN_SYNC_OK`。

---

### Task 16: P1-2a 身份前置探针契约（双钥匙入接口、权威每调用输出上限）

**依赖:** 00

**分支:**

```bash
git worktree add ../wt-sol-adopt-16-identity-probe-contract -b feat/sol-adopt-16-identity-probe-contract
cd ../wt-sol-adopt-16-identity-probe-contract
```

**Files:**

- Create: `config/ai_workflow_identity_probe_manifest.schema.json`
- Create: `scripts/ai_workflow_identity_probe.py`（契约、校验、dry-run）
- Create: `tests/test_ai_workflow_identity_probe.py`
- Modify: `scripts/sync_plugin.py`（仅 `CONFIG_FILES` 增 `ai_workflow_identity_probe_manifest.schema.json`；**不得**加入 `RUNTIME_FILES`）
- Modify: `tests/test_ai_workflow_distribution.py`
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/config/ai_workflow_identity_probe_manifest.schema.json`

**Interfaces:**

- Consumes: 无生产模块（与 `scripts/ai_workflow_router_probe.py` 同级
  的独立实验脚本约定）。
- Produces:
  - `IDENTITY_PROBE_MANIFEST_SCHEMA_VERSION = "identity-probe-manifest-1"`
  - `IDENTITY_PROBE_PROTOCOL_VERSION = "identity-probe-1"`
  - `IDENTITY_PROBE_ARMS = frozenset({"NO_OP", "ONE_TURN", "TWO_TURN"})`
  - `EXECUTOR_KINDS = frozenset({"DRY_RUN", "FAKE", "LIVE"})`
  - `IDENTITY_FIELD_SOURCES = frozenset({"SERVER_METADATA", "RUNTIME_EVIDENCE"})`
  - `IDENTITY_PROBE_BUDGET_FIELDS = ("max_calls", "max_output_tokens", "max_output_tokens_per_call")`——三个预算字段均为**必填正整数**；`max_output_tokens_per_call` 是每次 executor 调用前预算 reservation 使用的**权威每调用最大输出额度**（manifest 明文，非估算值）
  - `validate_identity_probe_manifest(value: Mapping[str, object]) -> None`（含三预算字段必填正整数校验；缺任一或为非正整数/布尔 → fail-closed）
  - `load_identity_probe_config(path: Path) -> Mapping[str, object]`（`identity_probe.enabled` 缺省为 `false`）
  - `require_identity_probe_authorized(config: Mapping[str, object], *, allow_live_model: bool, executor_kind: str) -> None`（`LIVE` 缺任一钥匙 → `IDENTITY_PROBE_NOT_AUTHORIZED`，必须先于任何 executor 调用；`executor_kind` 越出闭集 → 拒绝）
  - `build_identity_probe_manifest(*, batch_id: str, arm: str, model: str, effort: str, seed: int, max_calls: int, max_output_tokens: int, max_output_tokens_per_call: int, created_at_utc: str) -> Mapping[str, object]`（显式分开 `requested_launch_intent` / `observed_runtime_identity` / `model_text_output` 三段；身份字段只允许 `identity_source ∈ IDENTITY_FIELD_SOURCES`）
  - `main(argv: list[str] | None = None) -> int`（本卡只支持 `dry-run`）
  - 模块契约（文本 + 静态扫描测试锁定）：**全模块唯一**接受 executor
    可调用对象的公开函数是 `run_identity_probe`（Task 17 实现），其
    签名必须同时携带 `config` 与 `allow_live_model`；不存在任何
    「可直接传 LIVE callable 且无双钥匙参数」的次级入口

- [ ] **Step 1: 写 manifest 校验的失败测试**

合法 manifest round-trip；`arm` 越出 `IDENTITY_PROBE_ARMS`、未知
模型/推理档、多余字段、`protocol_version` 不等于
`identity-probe-1`、缺三预算字段任一、预算非正整数/布尔 →
fail-closed；`identity_source` 为 `MODEL_TEXT` 或任何闭集外值 →
拒绝。

- [ ] **Step 2: 写双钥匙的失败测试**

配置 `identity_probe.enabled=false` 时即使带 `--allow-live-model`
也拒绝 live；`enabled=true` 但缺 `--allow-live-model` 同样拒绝；
双钥匙齐备才进入 live 分支（本卡只验证门控，不发起真实调用）；
`DRY_RUN`/`FAKE` 不需要钥匙；负向断言：被拒时 executor 可调用对象
的调用计数为零。

- [ ] **Step 3: 写身份来源的负向测试（字段污染）**

构造模型文本声称身份的样例（文本内含看似合法的
model/effort/sandbox 字段）：manifest/记录中 model、effort、sandbox、
permission、fork/nested 字段不从 `model_text_output` 取值；缺权威
元数据时观测身份字段为 `AUTHORITY_UNAVAILABLE`，不得以自报补齐。

- [ ] **Step 4: 写不接生产链的负向测试**

静态扫描 `scripts/ai_workflow_identity_probe.py` 的 import 列表，
断言不出现 `ai_workflow`（生产 store）与 `ai_workflow_repairs`；
`tests/test_ai_workflow_distribution.py` 断言 `RUNTIME_FILES` 不含
`ai_workflow_identity_probe.py` 且插件 runtime 目录无此文件；
manifest 字段集与 `ai_workflow_router_probe_manifest.schema.json`
的交集断言为空协议（版本字段不同），证明不与 R1–R3 合并。

- [ ] **Step 5: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_identity_probe -v
```

Expected: 新测试失败（模块尚不存在）。

- [ ] **Step 6: 最小实现**

新建脚本与 schema；dry-run 只打印解析后的 manifest 与实验计划，
退出码 0。同步 `scripts/sync_plugin.py` 的 `CONFIG_FILES`。

- [ ] **Step 7: Verify GREEN + 基线复核**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_identity_probe \
  tests.test_ai_workflow_distribution \
  tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/ai_workflow_identity_probe.py dry-run --manifest <样例路径>
```

Expected: 测试全绿；dry-run 退出码 0 且输出 manifest 摘要。

- [ ] **Step 8: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(identity-probe): add isolated identity-probe-1 manifest contract with dual-key gate and authoritative per-call output cap"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_identity_probe tests.test_ai_workflow_distribution tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；输出 `PLUGIN_SYNC_OK`；runtime 清单不含探针
脚本。

---

### Task 17: P1-2b 身份探针 runner 与 A/B 报告（唯一 runner、逐次预算 reservation）

**依赖:** 16

**分支:**

```bash
git worktree add ../wt-sol-adopt-17-identity-probe-runner -b feat/sol-adopt-17-identity-probe-runner
cd ../wt-sol-adopt-17-identity-probe-runner
```

**Files:**

- Modify: `scripts/ai_workflow_identity_probe.py`（runner、聚合、报告）
- Modify: `tests/test_ai_workflow_identity_probe.py`

**Interfaces:**

- Consumes: Task 16 全部 Produces。
- Produces:
  - `run_identity_probe(manifest: Mapping[str, object], *, config: Mapping[str, object], allow_live_model: bool, executor: Callable[[Mapping[str, object]], Mapping[str, object]], executor_kind: str, experiment_root: Path) -> list[Mapping[str, object]]`——**第一条语句**调 `require_identity_probe_authorized(config, allow_live_model=allow_live_model, executor_kind=executor_kind)`；逐次执行前做预算 reservation，预约束只使用 manifest 的权威字段：`calls_made >= max_calls` → 停止（`stop_reason="MAX_CALLS"`）；`tokens_used + manifest["max_output_tokens_per_call"] > max_output_tokens` → 停止（`stop_reason="MAX_OUTPUT_TOKENS"`），**该次 executor 调用不发生**（调用次数恰好停在预算线）；**禁止**使用任何「预计输出」参与预约束。调用返回后按权威 usage 三段实报实销累加 `tokens_used`；单次实际输出超过 `max_output_tokens_per_call` → 该次记录标记 `PER_CALL_CAP_EXCEEDED` 并立即停止（有界且显式记录的 fail-closed 事件）；逐次原始记录追加写 `experiment_root` 下 jsonl（权威 usage 三段、arm 配置哈希、runtime metadata、缓存状态、失败记录）
  - `aggregate_identity_probe_results(records: list[Mapping[str, object]]) -> Mapping[str, object]`（每臂 `uncached_input_tokens`/`cached_input_tokens`/`output_tokens` 的样本数、总量、均值、min、max、p50、p90、缓存命中比例、失败计数、arm 配置哈希；配对差值 `ONE_TURN − NO_OP`、`TWO_TURN − ONE_TURN`）
  - `render_identity_probe_report(summary: Mapping[str, object]) -> str`（含协议版本、实验根目录、预算消耗与停止原因）

- [ ] **Step 1: 写 runner 双钥匙与预算的失败测试**

fake executor 下三臂各跑规定次数，产物（manifest + 逐次原始记录 +
聚合 summary）只写 `experiment_root`；`LIVE` executor 缺双钥匙 →
`IDENTITY_PROBE_NOT_AUTHORIZED` 且 executor 调用计数为零（第一道
语句即门控）；预算触线：构造 `max_calls=2` → executor 调用计数恰好
为 2，第 3 次不发生，`stop_reason == "MAX_CALLS"`；
**权威每调用上限触线**：`max_output_tokens=100、
max_output_tokens_per_call=40` → 第 3 次调用前 `60 + 40 > 100`
停止，executor 调用计数恰好为 2（预约束用的是 manifest 上限而非
任何预计值——构造实际输出恒小于上限的 fake executor，证明停止
只能由上限预约束触发）；单次实际输出超过
`max_output_tokens_per_call` → 记录 `PER_CALL_CAP_EXCEEDED` 且不再
发起下一次调用；executor 返回缺 usage 三段任一 → 该次记录标记无效
且不计入聚合。

- [ ] **Step 2: 写 A/B 报告的失败测试**

聚合输出按 `NO_OP`/`ONE_TURN`/`TWO_TURN` 分组，含每臂三段 usage 的
均值/min/max/p50/p90/样本数与配对差值（能判断多回合是否重放前缀、
缓存命中比例）；样本数为 0 的臂标记 `OBSERVATION_ONLY` 而不出结论；
报告含协议版本与预算消耗。

- [ ] **Step 3: 写不接生产链的负向测试**

fake executor 跑完整批后断言仓库 `git status` 干净（任务目录、
`events.jsonl`、`dispatches.jsonl` 均未被触碰）；summary 不含
R1–R3 manifest 的字段名（协议隔离）；报告不出现任何生产路由或
`effective_route` 相关键；模型文本声称身份的记录其身份字段仍为
`AUTHORITY_UNAVAILABLE`；静态扫描断言模块中除 `run_identity_probe`
外无其他公开函数接受 executor 形参，且 `run_identity_probe` 源码不
含「预计输出」语义的估算表达式（预约束只读
`max_output_tokens_per_call` 与 `max_output_tokens`）。

- [ ] **Step 4: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_identity_probe -v
```

Expected: 新增 runner/报告测试失败。

- [ ] **Step 5: 最小实现**

三态 executor（dry-run / fake / live 双钥匙）；live 分支只留接口
与门控，真实调用需另行批准。

- [ ] **Step 6: Verify GREEN + 全量回归 + 基线复核**

```bash
python3.11 -m unittest tests.test_ai_workflow_identity_probe tests.test_ai_workflow_baseline_manifest -v
python3.11 -m unittest discover -s tests
```

Expected: 全部通过，基线清单用例不变红。

- [ ] **Step 7: 提交**

```bash
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(identity-probe): add single dual-keyed runner with per-call authoritative budget reservation"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_identity_probe tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: runner、聚合、报告与全部负向断言全绿；输出
`PLUGIN_SYNC_OK`。

---

### Task 18: P1-3a 证据链事件生产者（launch intent、ai-runtime-evidence-2、fork/nested 枚举）

**依赖:** 02、13、14（14 提供 `LAUNCHER_VERSION`/`compute_install_version`；13 提供执行汇点与许可单事务）

**分支:**

```bash
git worktree add ../wt-sol-adopt-18-evidence-producers -b feat/sol-adopt-18-evidence-producers
cd ../wt-sol-adopt-18-evidence-producers
```

**Files:**

- Create: `config/ai_workflow_runtime_evidence_v2.schema.json`
- Create: `scripts/ai_workflow_evidence.py`（launch intent 与 runtime-evidence-2 的生产者原语；只依赖 declarations/preflight/artifacts）
- Create: `tests/test_ai_workflow_launch_intent.py`
- Modify: `scripts/ai_workflow.py`（`run_codex`：许可单事务临界区内、spawn 前接 `record_launch_intent`（Task 13 预留的扩展点）；既有 `RUNTIME_EVIDENCE_RECORDED` 落账点（`:1906`）后追加 runtime-evidence-2 sidecar 记录）
- Modify: `scripts/ai_workflow_repairs.py`（`run_assignment`：汇点 2 临界区内 spawn 前同规接 `record_launch_intent`；v2 controller 证据落账点同规追加）
- Modify: `tests/test_ai_workflow.py`
- Modify: `tests/test_ai_workflow_repairs.py`
- Modify: `scripts/sync_plugin.py`（`CONFIG_FILES` 增 `ai_workflow_runtime_evidence_v2.schema.json`；`RUNTIME_FILES` 增 `ai_workflow_evidence.py`）
- Modify: `tests/test_ai_workflow_distribution.py`
- Modify: `tests/test_ai_workflow_import_graph.py`（允许边表增 evidence 行）
- Mirror（仅经 sync 生成）: `plugins/ai-workflow/config/ai_workflow_runtime_evidence_v2.schema.json`、`plugins/ai-workflow/runtime/ai_workflow_evidence.py`、`plugins/ai-workflow/runtime/ai_workflow.py`、`plugins/ai-workflow/runtime/ai_workflow_repairs.py`

**Interfaces:**

- Consumes: Task 12 的 `DispatchPermit`；Task 14 的
  `LAUNCHER_VERSION`、`compute_install_version`；Task 02 的
  `load_route_declaration_locked`；`scripts/ai_workflow_artifacts.py` 的
  `content_id`、`verify_content_id`、`WorkflowError`、
  `TaskStoreProtocol`；`scripts/ai_workflow_runtime.py` 的
  `inspect_agent_runtime`（`:534`，既有，fork/nested 观测来源，本卡
  不改）。
- Produces:
  - `LAUNCH_INTENT_EVENT_TYPE = "LAUNCH_INTENT_RECORDED"`
  - `LAUNCH_INTENT_EVENT_FIELDS: frozenset[str]`（`event_type`、`event_id`、`task_id`、`envelope_hash`、`permit_id`、`role`、`command_sha256`、`tool_mapping_sha256`、`route_config_hash`、`launcher_version`、`install_version`、`timestamp_utc`）
  - `LAUNCH_INTENT_ID_EXCLUDE = frozenset({"event_id"})`；`event_id = content_id("ai-launch-intent-1", _launch_intent_preimage(event), exclude=LAUNCH_INTENT_ID_EXCLUDE)`；验证只经 `verify_content_id("ai-launch-intent-1", _launch_intent_preimage(event), exclude=LAUNCH_INTENT_ID_EXCLUDE, id_field="event_id")`（同一 exclude、同一模块私有投影——compute 与 verify 共用）
  - `RUNTIME_EVIDENCE_V2_SCHEMA_VERSION = "ai-runtime-evidence-2"`
  - `RUNTIME_EVIDENCE_V2_FIELDS: frozenset[str]`（`schema_version`、`evidence_id`、`task_id`、`envelope_hash`、`event_index`、`observed_agent_type`、`native_agent_id`、`native_thread_id`、`fork_state`、`nested_state`、`recorded_at_utc`）
  - `RUNTIME_EVIDENCE_ID_EXCLUDE = frozenset({"evidence_id"})`；`evidence_id = content_id("ai-runtime-evidence-2", _evidence_preimage(record), exclude=RUNTIME_EVIDENCE_ID_EXCLUDE)`；验证只经 `verify_content_id(..., _evidence_preimage(record), exclude=RUNTIME_EVIDENCE_ID_EXCLUDE, id_field="evidence_id")`（同一 exclude、同一模块私有投影）
  - `FORK_STATES = frozenset({"VERIFIED_NONE", "VERIFIED_PRESENT", "AUTHORITY_UNAVAILABLE"})`
  - `NESTED_STATES = frozenset({"VERIFIED_NONE", "VERIFIED_PRESENT", "AUTHORITY_UNAVAILABLE"})`
  - `record_launch_intent(store: TaskStoreProtocol, task_id: str, *, permit: DispatchPermit, role: str, argv: tuple[str, ...], tool_mapping: Mapping[str, object]) -> None`——第一行 `store._assert_lock_held(task_id)`（写入点位于许可单事务临界区内、spawn 之前）；`route_config_hash` 经 `load_route_declaration_locked` 读取已存声明（含恢复）；`install_version` 经 `compute_install_version()`；事件形状由 golden 测试冻结
  - `derive_fork_nested_states(observed: Mapping[str, object]) -> tuple[str, str]`——观测元数据齐备且表明无 fork/nested → `VERIFIED_NONE`；齐备且表明存在 → `VERIFIED_PRESENT`；字段缺失/不可得 → `AUTHORITY_UNAVAILABLE`（**缺字段 ≠ 没有 fork**）
  - `append_runtime_evidence_v2(store: TaskStoreProtocol, task_id: str, *, event_index: int, observed: Mapping[str, object], recorded_at_utc: str) -> None`——紧随 `RUNTIME_EVIDENCE_RECORDED` 落账追加 `runtime-evidence-v2.jsonl`（本账本**无自身 seq**；`event_index` 指向 events.jsonl 行序）
  - `replay_runtime_evidence_v2(store: TaskStoreProtocol, task_id: str) -> tuple[dict[str, object], ...]`——重放 fail-closed：截断尾记录、非对象行、跨任务记录、逐条 verify（上述同一 exclude）、重复 `event_index`、`event_index` 指向的事件不是 `RUNTIME_EVIDENCE_RECORDED` → `EVIDENCE_LEDGER_CORRUPT`（不做「重复 seq」检查——完整性 = 内容 ID 唯一性 + `event_index` 引用完整性 + 行序，见设计「JSONL 账本完整性策略」）

链节生产者对照（本卡把每一链节钉到具体生产位置）：路由声明 → Task
02 唯一创建阶段（崩溃恢复挂 load/ensure/resume 权威入口）；启动意图
→ 本卡（`run_codex` 与 `run_assignment` 两处执行汇点的许可临界区
内、spawn 前）；rollout 有效身份 → 既有
`RUNTIME_EVIDENCE_RECORDED`（事件形状不变）；fork/nested → 本卡
`runtime-evidence-v2.jsonl`（观测而非请求配置）；终验裁决 → Task 04。

- [ ] **Step 1: 写 launch intent 事件的失败测试**

`run_codex` 路径：事件在许可临界区内、子进程启动前写入（与 spy 的
调用顺序断言：intent 事件先于 Popen、晚于 `RESERVED`），字段集与
golden 完全一致；`event_id` 满足 canonical preimage（预填 `event_id`
不影响输出）；`command_sha256` 与实际 argv 规范化哈希一致；
`permit_id` 与本次许可一致；`envelope_hash` 与存储任务一致。
`run_assignment` 路径同样断言。

- [ ] **Step 2: 写版本与证据钉板的失败测试**

事件的 `launcher_version == LAUNCHER_VERSION`、
`install_version == compute_install_version()`；既有
`RUNTIME_EVIDENCE_RECORDED` 事件字段集不变（负向钉板）；
`runtime-evidence-v2.jsonl` 记录与事件 `event_index` 对得上；
重放对重复 `event_index`、`event_index` 指向非
`RUNTIME_EVIDENCE_RECORDED` 事件、截断尾、跨任务混入均
`EVIDENCE_LEDGER_CORRUPT`（测试名不出现「重复 seq」声称——本账本
无自身 seq）。

- [ ] **Step 3: 写 fork/nested 枚举的失败测试**

观测元数据齐备且无 fork/nested → `VERIFIED_NONE`；齐备且存在 →
`VERIFIED_PRESENT`；缺 `observed_agent_type`/缺 `native_*` 字段 →
`AUTHORITY_UNAVAILABLE`（逐字段缺失各一例）；取值越出闭集 → 拒绝；
请求配置 `fork_turns=none` 但观测缺失 → 仍 `AUTHORITY_UNAVAILABLE`
（请求值不能单独作为运行事实）。

- [ ] **Step 4: 写无许可不意图的失败测试**

许可门拒绝（缺声明/越权/未预检/超预算/退休 ID/已认领 ID）时不写
`LAUNCH_INTENT_RECORDED`（意图事件序列与许可账本一致）。

- [ ] **Step 5: Verify RED**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_launch_intent \
  tests.test_ai_workflow \
  tests.test_ai_workflow_repairs -v
```

Expected: 新增事件测试失败。

- [ ] **Step 6: 最小实现**

两处执行汇点接 `record_launch_intent`（Task 13 预留的临界区扩展点，
spawn 前）；两处证据落账点接 `append_runtime_evidence_v2`；不改任何
既有事件形状。同步 `scripts/sync_plugin.py` 与 import-graph 允许边。

- [ ] **Step 7: Verify GREEN + 全量回归 + 基线复核**

```bash
python3.11 -m unittest discover -s tests
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
```

Expected: 基线清单用例保持通过，新增全绿。

- [ ] **Step 8: 同步镜像并提交**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(evidence): record launch intent and versioned fork/nested runtime evidence at execution sinks"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_launch_intent tests.test_ai_workflow tests.test_ai_workflow_repairs tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 两汇点事件 golden、fork/nested 枚举、无许可不意图、
`event_index` 引用完整性全绿；输出 `PLUGIN_SYNC_OK`。

---

### Task 19: P1-3b 证据链读取器与只读审计 CLI

**依赖:** 04、18

**分支:**

```bash
git worktree add ../wt-sol-adopt-19-evidence-chain -b feat/sol-adopt-19-evidence-chain
cd ../wt-sol-adopt-19-evidence-chain
```

**Files:**

- Create: `scripts/ai_workflow_evidence_chain.py`
- Create: `tests/test_ai_workflow_evidence_chain.py`
- Modify: `tests/test_ai_workflow_distribution.py`（断言 `RUNTIME_FILES` 不含 `ai_workflow_evidence_chain.py`）

**Interfaces:**

- Consumes: Task 02 的 `load_route_declaration`；Task 03 的
  `capture_candidate_state`、`CandidateState`；Task 04 的
  `load_verdict_history`、`evaluate_verdict_freshness`；Task 18 的
  `LAUNCH_INTENT_EVENT_FIELDS`、`FORK_STATES`、`NESTED_STATES`、
  `replay_runtime_evidence_v2`；`scripts/ai_workflow_artifacts.py` 的
  `verify_content_id`、`TaskStoreProtocol`。
- Produces:
  - `EVIDENCE_CHAIN_LINKS = ("route_declaration", "launch_intent", "rollout_identity", "fork_state", "final_verdict")`
  - `EVIDENCE_CHAIN_GAP_CODES: frozenset[str]`（`CHAIN_MISSING_ROUTE_DECLARATION`、`CHAIN_ENVELOPE_MISMATCH`、`CHAIN_MISSING_LAUNCH_INTENT`、`CHAIN_MISSING_ROLLOUT_IDENTITY`、`CHAIN_FORK_STATE_UNVERIFIED`、`CHAIN_MISSING_FINAL_VERDICT`、`CHAIN_VERDICT_STALE`、`CHAIN_EVIDENCE_ORPHAN`）
  - `@dataclass(frozen=True) class EvidenceChain`（每链节含 `task_id`、`envelope_hash`、来源路径/事件索引）
  - `build_evidence_chain(store: TaskStoreProtocol, task_id: str) -> EvidenceChain`——**签名无 `current` 参数、无 `baseline_commit` 参数**：终验链节新鲜度由读取器内部权威重算——`load_verdict_history` 为空 → 记录 `CHAIN_MISSING_FINAL_VERDICT`（不求 baseline）；否则 baseline = 最新裁决的 `candidate_state.baseline_commit`，证据集合 = 本任务 `events.jsonl` 重读的 `runtime_evidence_sha256` 集合，再 `capture_candidate_state(store, task_id, baseline_commit=<账本基线>, runtime_evidence_ids=<重读集合>)` 后评估；证据 ID 按集合规范化排序比较，并逐条 `verify_content_id` 验证归属与内容哈希，不信任裁决自报 ID
  - `validate_evidence_chain(chain: EvidenceChain) -> tuple[str, ...]`
  - `main(argv: list[str] | None = None) -> int`

**分发决定（进卡理由）**：证据链构建器只被本审计 CLI 使用，生产
workflow 从不 import 它；为守住最小生产插件面，
`ai_workflow_evidence_chain.py` **不进** `RUNTIME_FILES`（与
router probe、identity probe 同规），只随仓库分发脚本。distribution
测试断言插件 runtime 目录无此文件。

- [ ] **Step 1: 写完整链的失败测试**

走完一次带声明、launch intent、运行时证据、fork/nested v2 记录与
新鲜裁决的 fake 任务：`build_evidence_chain` 五链节齐备且全部以同一
`task_id + envelope_hash` 连接；`validate_evidence_chain` 返回空
元组。

- [ ] **Step 2: 写缺口检测的失败测试**

删除声明 → `CHAIN_MISSING_ROUTE_DECLARATION`；篡改某链节
`envelope_hash` → `CHAIN_ENVELOPE_MISMATCH`；缺 launch
intent/rollout 身份/终验裁决 → 各自对应代码；`fork_state` 为
`AUTHORITY_UNAVAILABLE` → `CHAIN_FORK_STATE_UNVERIFIED`（缺失不
当作 `VERIFIED_NONE`）；裁决 `STALE` → `CHAIN_VERDICT_STALE`；裁决
引用的证据 ID 不属于本任务（伪造引用）→ `CHAIN_EVIDENCE_ORPHAN`
（不信任自报 ID）；`inspect.signature(build_evidence_chain)` 断言
无 `current`/`baseline_commit` 形参（新鲜度 baseline 来自读取器内部
重放，不来自 CLI 调用者）。

- [ ] **Step 3: 写只读与 CLI 的失败测试**

构建+校验全程不写任务目录（前后对比任务目录文件列表与哈希）；
CLI `python3.11 scripts/ai_workflow_evidence_chain.py --root <repo> --task-id <id>`
完整链退出码 0、打印五链节状态行；有缺口时退出码 1 并逐行打印
缺口代码。

- [ ] **Step 4: Verify RED**

```bash
python3.11 -m unittest tests.test_ai_workflow_evidence_chain -v
```

Expected: 新测试失败（模块尚不存在）。

- [ ] **Step 5: 最小实现**

新建只读模块与 CLI；不改 `scripts/sync_plugin.py` 的
`RUNTIME_FILES`。

- [ ] **Step 6: Verify GREEN + 基线复核**

```bash
python3.11 -m unittest \
  tests.test_ai_workflow_evidence_chain \
  tests.test_ai_workflow_distribution \
  tests.test_ai_workflow_baseline_manifest -v
```

Expected: 全部通过，含 runtime 清单负向断言。

- [ ] **Step 7: 提交**

```bash
python3.11 scripts/sync_plugin.py --check
git add -A && git commit -m "feat(evidence-chain): add read-only evidence chain auditor outside the runtime distribution"
```

**验收标准:**

```bash
python3.11 -m unittest tests.test_ai_workflow_evidence_chain tests.test_ai_workflow_distribution tests.test_ai_workflow_baseline_manifest -v
python3.11 scripts/sync_plugin.py --check
```

Expected: 退出码均 0；CLI 完整链退出 0、缺口链退出 1 有测试证据；
插件 runtime 不含审计脚本；输出 `PLUGIN_SYNC_OK`。

---

### Task 20: 收口：baseline 复核、wire golden、负向回归、文档

**依赖:** 00–19

**分支:**

```bash
git worktree add ../wt-sol-adopt-20-closeout -b docs/sol-adopt-20-closeout
cd ../wt-sol-adopt-20-closeout
```

**Files:**

- Create: `tests/test_ai_workflow_wire_golden.py`
- Modify: `tests/test_ai_workflow_distribution.py`（分发清单总检查）
- Modify: `docs/superpowers/specs/2026-08-28-sol-review-adoption-design.md`（标记各项落地状态）
- 无生产代码改动，除非新失败测试证明缺陷。
- **明确禁止**：本卡不得首次生成或修改 `tests/baseline_manifest.json`；
  只能运行 Task 00 的 checker 复核。baseline 相关测试的任何变红都
  必须回溯到引入漂移的施工卡修复，不得在本卡更新 manifest。

**Interfaces（本卡必须逐项落实的负向断言清单）：**

- [ ] **Step 1: baseline 复核**

```bash
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest -v
```

Expected: 三个 checker 全绿——基线清单内测试 ID 无一删除/改名、skip
语义逐条一致；全量 `discover` 零失败（基线用例无变红）。manifest
文件的 git 历史在本卡无提交（`git log -- tests/baseline_manifest.json`
断言最后提交属于 Task 00 分支合并）。

- [ ] **Step 2: 写冻结 wire golden 测试**

`tests/test_ai_workflow_wire_golden.py`：`ai-task-1`、`ai-result-1`
字段集 golden；`ai-route-decision-1` 九字段
（`ROUTE_DECISION_FIELDS`）golden；`adversarial-acceptance-1` 六类
事件（`_ACCEPTANCE_EVENT_TYPES`）的字段集 golden；
`OWNER_DECISIONS` 闭集 golden；`EFFECT_KINDS` 闭集 golden（**不含**
`OWNERSHIP_VIOLATION_RECORDED`）；`OWNERSHIP_VIOLATION_EVENT_FIELDS`
字段闭集 golden；`AUTHORIZATION_ID_EXCLUDE` /
`RECORD_ID_EXCLUDE` / `VERDICT_ID_EXCLUDE` /
`LAUNCH_INTENT_ID_EXCLUDE` / `RUNTIME_EVIDENCE_ID_EXCLUDE` 各 exclude
常量 golden（逐集合逐元素，防再合并）。任一漂移即红。

- [ ] **Step 3: 写 effective_route 与生产面负向测试**

全部新工件（声明、裁决、登记器、授权、快照、预检、许可、launch
intent、runtime-evidence-2）存在时：probe summary 的
`effective_route == "UNCHANGED"`；路由声明不改变既有 route decision
或默认映射（对比 `persist_or_reuse_route_decision` 前后存储 wire
一致）；成本字段不进入 `evaluate_optimization_gate`；
identity probe 与 evidence chain 不在 `RUNTIME_FILES`；
**四条直调路径**（`_run_live_luna`、team-call L1 生产 controller、
`CodexConstructionRunner.run_construction`、`run_assignment`）缺声明
均被门控且 executor 调用计数为零（回归套件形式固化）；**DIRECT_L0
不触达模型**：spy 断言 `_run_trusted_team_call_l0` 全程不到达
`run_codex`、不创建任务目录、不消耗许可；许可状态机终态断言：任一
任务重放 `dispatch-permits.jsonl` 后，对终态 permit 的同 ID 再进
一律拒绝（`DISPATCH_PERMIT_ALREADY_STARTED` /
`DISPATCH_IDENTITY_RETIRED` / `DISPATCH_PERMIT_UNCLAIMED`）。

- [ ] **Step 4: 写 README 免责声明负向检查**

断言 `README.md` 仍包含非实测免责声明现状表述（当前为「这不是实测
成本赢家，也不改生产 `effective_route`，实际仍以使用者选择为准」
一句）；被删除或改写为实测推荐 → 测试变红。

- [ ] **Step 5: 同步与全量回归**

```bash
python3.11 scripts/sync_plugin.py --write
python3.11 scripts/sync_plugin.py --check
python3.11 -m unittest discover -s tests
```

Expected: `--check` 输出 `PLUGIN_SYNC_OK`；基线清单用例全部通过、
skip 语义不变；00–19 与本卡新增全部 pass。

- [ ] **Step 6: 全量校验脚本**

```bash
sh scripts/verify_all.sh
```

Expected: 退出码 0。

- [ ] **Step 7: 文档状态更新**

在设计文档各项末尾标注落地卡片编号与状态；backlog（P2/P3）保持
未实施标记。

- [ ] **Step 8: 提交**

```bash
git add -A && git commit -m "docs(superpowers): mark sol review adoption items landed and record regression evidence"
```

**验收标准:**

```bash
python3.11 scripts/sync_plugin.py --check
python3.11 -m unittest discover -s tests
python3.11 -m unittest tests.test_ai_workflow_baseline_manifest tests.test_ai_workflow_wire_golden -v
sh scripts/verify_all.sh
```

Expected: 全部退出码 0；基线清单零回归、skip 语义不变、manifest 未
被本卡触碰；wire golden、`UNCHANGED` 负向、README 检查、DIRECT_L0
不触达模型、四条直调路径无声明拒发回归、许可状态机终态断言全绿。
