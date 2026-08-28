# Sol xhigh 裁定采纳设计（Sol medium 第四次复审修订版）

## 来源与目标

本设计落地 Sol xhigh 对常驻路由器研究及五条工程启发的复核裁定
（2026-08-28）。裁定总判断：第 2、4 条启发以「宿主确定性生成、运行时
强制校验」的修改版形态优先落地；第 1 条只支持带快照的成本估算，不得把
机制候选升级为真实成本赢家；第 3 条改造为宿主前置证据而非子模型自报；
第 5 条仅保留为架构 ADR。在此之前 `effective_route` 继续保持
`UNCHANGED`。

本版为**第五版**：第四版设计与 21 张施工卡经 Sol medium 第四次复审
判定 **FAIL**（14 条中 10 项 CLOSED、4 项 PARTIAL、0 项 OPEN），并
给出 5 条「施工前必须再改」清单。本版逐条闭合该 5 条清单，对照表见
文末「第四次复审 5 条闭合对照」。**已 CLOSED 项不得回退**：第四次
复审 CLOSED 的 10 项——① 许可状态机合法转换与终态后同 ID 一律拒绝
（无幂等返回窗口）；③ 单事务步骤顺序（1–7）与 spawn 前后崩溃语义；
④ `require_verdict_fresh[_locked]` 删除调用者可控 baseline/current、
门内重放 baseline 权威重算；⑤ `verdict_source_role` 从 issuer 证据
派生、签名无角色参数；⑦ `COMMAND_PRODUCERS` 双 producer 与
construction 冻结步骤 `producer_ref`；⑨ `RATE_UNITS`/
`RATE_UNIT_BASE` 与 `decimal.localcontext` 口径、minor-unit int；
⑩ Task 12 依赖 02/07/09/14 且不与 09 并行；⑪ preflight 安全入口
内部重算 context；⑫ identity probe 权威 `max_output_tokens_per_call`
预约束；⑭ 逐账本 seq 策略二选一冻结；第三次复审 CLOSED 的 4 项——
② CandidateState 权威 root 与 manifest 双采；⑨ 模块循环消除；
⑫ Task 00 基线先行；⑬ 证据链 fork/nested 版本化枚举；以及更早
CLOSED 的 7 项——① `envelope_hash` 强制等于 `task_sha256`、唯一
创建阶段、多入口幂等；② 八条派发路径全量清单与两个最低模型执行汇点；
④ 声明与派发先后只用锁内账本序，不做 UTC 墙钟比较；⑤ legacy 历史
任务 fail-closed、不自动补造声明；⑪ 控制面工件不触发所有权锁定；
⑯ `allowed_roles`/`active_roles` 分离；⑱ 模型文本不得填充任何身份
字段。

目标：按裁定「最终排序与最小落地」实施 P0 四项与 P1 三项，全部以
fail-closed、纯标准库、sidecar 扩展的方式落地，不改动任何冻结 schema。
P2（代理预算闭集、事务化安装与前向 CI）与 P3（角色/模型解耦 ADR）只进
backlog，本轮不出施工卡。

## 裁定红线（不得回退）

1. 路由声明必须由确定性宿主生成；不采纳由主控模型自由输出路由模式、
   自然语言路由决定或风险理由。模型只能提供不具授权效力的观察。
2. 不采纳子模型自然语言握手作为身份证明；只研究「宿主权威元数据前置
   门控」，且先在隔离实验中量化其 17k 级前缀成本。
3. 不采纳「有费率快照即可宣布真实成本赢家」；机制候选、快照下估算、
   生产反事实和质量调整成本必须分层，永不合并成赢家标签。
4. 不把 Luna/Terra/Sol 排成单一升降等级；路由变化只走闭集状态机与显式
   允许转换图，非单调改路仅允许 owner 签名覆盖。
5. 不采纳跨任务预检缓存；预检不得替代每次 rollout 的 S3/S4 身份验收。
6. 不以「owned 文件首次写入」作为唯一副作用边界；命令生成文件、未跟踪
   文件与外部副作用同样使安全移交失效。

## 关键定义

- **信封哈希（envelope_hash）**：冻结任务信封的 sha256，取值与
  `ai-route-decision-1.task_sha256` 完全一致；所有新 sidecar 用它与
  `task_id` 联合绑定任务身份。声明、裁决、登记器写入时都必须重新读取
  存储的 `task.json` 与 `route-decision.json` 并强制验证该等式，不接受
  调用者自报哈希。
- **sidecar**：任务目录下与 `events.jsonl` 平级的独立 JSON/JSONL 文件，
  各自带独立 `schema_version`。单文件 sidecar 走 `write_json_once`
  原子写，冲突即 fail-closed；需要历史或多次写入的 sidecar（裁决、授权、
  副作用、预检记录、派发许可、runtime evidence v2）一律 append-only
  JSONL，不覆盖、不就地改价、不追溯历史。
- **宿主**：`scripts/ai_workflow.py` 及其确定性协作模块，即不调用模型的
  控制面代码。所有门控判断都在宿主侧完成。
- **内容寻址 ID 与 canonical preimage（写读投影一致）**：所有内容寻址
  标识（`verdict_id`、`authorization_id`、consumption/transfer_lease
  记录 `record_id`、runtime evidence v2 的 `evidence_id`、
  `LAUNCH_INTENT_RECORDED` 的 `event_id`）统一由宿主内核函数
  `content_id(kind, fields, *, exclude)` 生成、由
  `verify_content_id(kind, record, *, exclude, id_field)` 验证：

  1. 哈希输入 = `{"kind": <域分隔串>, "fields": <去掉 exclude 字段后的
     记录投影>}`；**exclude 必须包含且只包含该类自身的 ID 字段**，禁止
     自引用定义（与既有 `_v2_event_id` 先 `pop("event_id")` 再哈希的
     做法同构，见 `scripts/ai_workflow_repairs.py:1257`），也禁止把
     其他 record 类的 ID 字段拉进 exclude——那会解除该字段与内容 ID
     的密码学绑定。
  2. 序列化 = canonical JSON：`json.dumps(..., ensure_ascii=False,
     sort_keys=True, separators=(",", ":"))`，UTF-8 编码；集合语义的
     列表（`runtime_evidence_ids`、`allowed_paths` 等）在进 preimage
     前必须排序去重；整数与字符串类型不得混用。
  3. **生成与验证接受完全相同的 exclude 集，并共用同一 canonical
     projection**：`verify_content_id` 的签名为 `(kind, record, *,
     exclude, id_field)`——按 exclude 投影重算后与
     `record[id_field]` 比对，不符即 `CONTENT_ID_MISMATCH`。每一类
     record 在各自模块冻结一个 exclude 常量并写进接口：

     | record 类 | exclude 常量 | id_field |
     |---|---|---|
     | final verdict | `VERDICT_ID_EXCLUDE = frozenset({"verdict_id"})` | `verdict_id` |
     | authorization | `AUTHORIZATION_ID_EXCLUDE = frozenset({"authorization_id"})` | `authorization_id` |
     | consumption | `RECORD_ID_EXCLUDE = frozenset({"record_id"})` | `record_id` |
     | transfer_lease | `RECORD_ID_EXCLUDE = frozenset({"record_id"})` | `record_id` |
     | launch intent | `LAUNCH_INTENT_ID_EXCLUDE = frozenset({"event_id"})` | `event_id` |
     | runtime evidence v2 | `RUNTIME_EVIDENCE_ID_EXCLUDE = frozenset({"evidence_id"})` | `evidence_id` |

     **ID 排除集按 record 类拆分（冻结）**：authorization ID 只排除
     `authorization_id` 自身；consumption/transfer_lease 的
     `record_id` 只排除 `record_id` 自身——这两类记录的
     `authorization_id` 是对被消费/租用授权的真实引用，**必须进入**
     `record_id` preimage，篡改 `authorization_id` 必然使
     `verify_record_id` 失败。禁止继续共用任何跨类 exclude 常量
     （旧 `OWNER_AUTH_ID_EXCLUDE = {"authorization_id", "record_id"}`
     组合废止）。
     **每类专用 canonical projection，compute 与 verify 共用**：每类
     record 有模块私有投影函数（authorizations 模块的
     `_authorization_preimage(record)` / `_record_preimage(record)`，
     其余各类同规），负责把集合语义字段（`allowed_paths` 等）经
     `sorted_strs` 排序去重；专用 compute 与 verify 都先经同一投影再
     调内核 `content_id`/`verify_content_id`——禁止「compute 规范化、
     verify 对原始列表直接哈希」的组合。写、读、重放三条路径调用同一个
     verify 函数。
     **不适用 ID 字段强制不存在**：wire 上某 record 类不适用的另一类
     ID 字段（如 authorization 记录的 `record_id`）必须**不存在**于
     记录中（不是 null、不是空串），由各类 `validate_*` 按 record 类
     闭集字段强制；禁止填占位值再靠 exclude 排除。
     golden preimage 测试按类冻结：固定字段冻结记录 → 钉死哈希字面量；
     预填本类 ID 字段的垃圾值不改变输出（证明生成与验证排除的是同一
     字段集）；**负向 golden**：consumption/transfer_lease 固定记录只
     修改 `authorization_id` → `verify_record_id` 必须
     `CONTENT_ID_MISMATCH`。禁止「生成排除两个字段、验证只排除一个
     字段」的组合。
- **CandidateState**：宿主对候选工作树的权威快照 `{candidate_commit,
  baseline_commit, tree_digest, diff_digest, runtime_evidence_ids}`。
  root 一律从冻结任务信封内部派生（REMEDIATION → `source_worktree`，
  其余 → `repository_root`），接口不接收调用方传入的 repo/root。快照
  准原子性由**完整候选 manifest 双采比对**保证：计算前采集全量条目
  （路径 + mode + 内容 sha256），计算后复读，HEAD 或 manifest 任一漂移
  即 `CANDIDATE_STATE_UNSTABLE`——文件内容在计算期间变化但 porcelain
  状态字母不变（始终 `M`）的竞态必然被内容哈希比对捕获。终验裁决绑定
  整个 CandidateState；新鲜度比较整体比较，调用方不可能漏传
  `diff_digest`。
- **DispatchPermit / dispatch_id 与许可状态机**：每次执行器启动前在任务
  锁内原子预留的派发许可。`dispatch_id` 为确定性内容哈希（generic 路径
  由 `task_sha256 + role + attempt_id` 派生；construction 路径复用
  `ai_workflow_planning.dispatch_id`（`scripts/ai_workflow_planning.py:723`）；
  v2 验收路径由 `task_sha256 + assignment_id + attempt_id` 派生）。
  **许可状态闭集**：`RESERVED`（已预留、未启动认领）、`STARTED`（启动
  已认领，终态）、`RELEASED_BEFORE_START`（启动前释放，终态）。
  **合法转换**：`∅ → RESERVED`；`RESERVED → STARTED`；`RESERVED →
  RELEASED_BEFORE_START`；`STARTED` 与 `RELEASED_BEFORE_START` 为终态，
  之后同 ID 的任何账本记录、reservation、claim、release 一律拒绝。
  **同 ID 再进 `require_dispatch_permit[_locked]` 一律拒绝，无幂等返回
  窗口**：早失败层为只读预检（`precheck_dispatch_permit`，不追加任何
  记录），每条派发只有一个真实预留点，因此不存在合法的同 ID 二次
  reservation；同 ID 再进只可能意味着崩溃孤儿或调用错误——state 为
  `RESERVED` → `DISPATCH_PERMIT_UNCLAIMED`；`STARTED` →
  `DISPATCH_PERMIT_ALREADY_STARTED`；`RELEASED_BEFORE_START` →
  `DISPATCH_IDENTITY_RETIRED`。这一设计比「幂等只允许发生在认领之前」
  更强：连认领前的幂等窗口也一并消除（认领前同 ID 再进同样拒绝）。
  技术重试必须使用新 `attempt_id`，从而得到新 `dispatch_id`；既有
  `_claim_attempt_context` 的 O_EXCL 认领（`scripts/ai_workflow.py:967`，
  重复 attempt_id 即 `ATTEMPT_CONTEXT_REUSED`）保证同一调用链之外不会
  出现合法的同 ID 再现。
- **授权 sidecar（ai-owner-authorization-1）**：版本化、不可变、带完整
  作用域的 owner 授权工件。承载 `VERDICT_STALE_OVERRIDE`（单次消费）
  与 `OWNERSHIP_TRANSFER`（scoped lease，按 `max_dispatches` 锁内原子
  扣减）两类授权；**不**扩张 `apply_owner_decision` 的
  `OWNER_DECISIONS` 闭集。
- **锁协议与 `_locked` 变体**：`WorkflowStore.lock(task_id)` 是
  非重入 `flock`（`LOCK_EX | LOCK_NB`，`scripts/ai_workflow.py:2545`），
  已持锁时再次获取必然 `TASK_ALREADY_RUNNING`。凡可能在任务锁内被
  调用的函数必须提供 `*_locked` 变体（与既有 `record_dispatch` /
  `_record_dispatch_locked` 约定一致，`scripts/ai_workflow.py:2488`），
  变体第一行调用 `store._assert_lock_held(task_id)`（锁内注册表探测，
  未持锁 → `LOCK_REQUIRED`）；同名无后缀函数是「自取锁再委派」的
  包装，**包装内除取锁与委派外不得有任何其他逻辑**；已持锁路径调用
  自取锁包装必然 `TASK_ALREADY_RUNNING`（负向测试锁定）。violation
  查询同规：`has_unresolved_ownership_violation_locked` 第一行
  `store._assert_lock_held(task_id)`，同名无后缀函数仅取锁委派；
  `require_dispatch_permit_locked` **只能**调用 `_locked` 版本。
  **传递调用图闭合检查**：对 `require_dispatch_permit_locked` 与
  `require_write_ownership_locked` 的完整传递调用图做 AST/源码检查——
  从两函数出发沿新业务模块（dispatch_policy/ownership/authorizations/
  declarations/preflight/side_effects）内调用边遍历可达闭包，断言闭包
  内不含任何自取锁包装（包装按「函数体仅含 `with store.lock(...)` 与
  委派」结构特征自动识别）——持锁路径进入自取锁包装在结构上不可能。
  纯读取函数（只经 `store.read_task_ledger`、永不取锁，如
  `load_owner_authorization`/`replay_authorizations`/
  `count_transfer_leases`/`leases_for_permit`）可在临界区内安全调用，
  其不取锁性质由同一 AST 检查锁定。授权校验、消费记录与被授权动作
  必须处于**同一临界区**（同一段 `store.lock` 块），禁止校验后出锁
  再执行动作。两个最低模型执行汇点的 `with store.lock(...)` **语法
  范围**内禁止调用自取锁版本（AST 范围断言，冻结包装名清单）；spawn
  前失败的锁外释放统一经 helper `release_permit_if_never_spawned(
  store, permit, *, spawned, reason)`（仅 `spawned` 为假时调自取锁
  包装 `release_permit_before_start`），汇点源码不直接出现自取锁
  包装调用。

### JSONL 账本完整性策略（seq 二选一，逐账本冻结）

每个 append-only 账本在「存储 seq」上二选一并冻结如下；有 seq 的重放
必须验证重复、从固定起点 1 连续及（许可账本）状态机合法转换；无 seq 的
账本**不得**声称做「重复 seq」检查，其完整性由内容 ID 唯一性、状态机
约束与行序承担：

| 账本 | seq 策略 | 重放完整性规则（全部 fail-closed） |
|---|---|---|
| `dispatch-permits.jsonl` | **有 `seq`**（任务内从 1 连续递增，追加时取当前行数 + 1） | 截断尾、非对象行、跨任务记录、重复 seq、seq 断档/不从 1 开始、非法状态转换（`∅→RESERVED`、`RESERVED→STARTED`、`RESERVED→RELEASED_BEFORE_START` 之外）、同 ID 两条 `RESERVED`、终态后再有同 ID 记录 → `DISPATCH_PERMIT_LEDGER_CORRUPT` |
| `final-verdicts.jsonl` | **无 seq** | 截断尾、非对象行、跨任务记录、逐条 `verify_verdict_id`、重复 `verdict_id` → `VERDICT_LEDGER_CORRUPT`；最新裁决 = 行序最后一条 |
| `owner-authorizations.jsonl` | **无全局 seq**；`transfer_lease` 携带按 `authorization_id` 局部从 1 连续的 `dispatch_seq` | 截断尾、非对象行、跨任务记录、按 `record_kind` 逐条 verify（authorization → `verify_authorization_id`；consumption/transfer_lease → `verify_record_id`）、重复 `record_id`、同一 `authorization_id` 出现两条 `authorization` 记录、`dispatch_seq` 局部断档/重复 → `AUTHORIZATION_LEDGER_CORRUPT` |
| `side-effects.jsonl` | **无 seq** | 截断尾、非对象行、跨任务记录、`effect_kind` 越出闭集 → `SIDE_EFFECT_LEDGER_CORRUPT`；无唯一性声称，多条同类记录合法，语义按行序。**`EFFECT_KINDS` 闭集不含、也永不加入 `OWNERSHIP_VIOLATION_RECORDED`**——violation 的唯一权威持久来源是 `events.jsonl` 的同名事件（见 P0-3「聚焦转让」节），本账本不承载 violation 项 |
| `preflight-records.jsonl` | **无 seq** | 截断尾、非对象行、跨任务记录 → `PREFLIGHT_LEDGER_CORRUPT`；同一任务多角色、多 cache_key 记录合法，按行序取最新匹配 |
| `runtime-evidence-v2.jsonl` | **无自身 seq**；`event_index` 指向 `events.jsonl` 行序 | 截断尾、非对象行、跨任务记录、逐条 `verify_evidence_id`、重复 `event_index`、`event_index` 指向的事件不是 `RUNTIME_EVIDENCE_RECORDED` → `EVIDENCE_LEDGER_CORRUPT` |

## 模块依赖方向（无循环，施工卡不得偏离）

宿主内核原语（canonical JSON、append/read JSONL、`write_json_once`、
`WorkflowError`、`content_id`/`verify_content_id`、`TaskStoreProtocol`、
`PROCESS_GENERATION`）全部位于叶子模块 `ai_workflow_artifacts.py`；
`ai_workflow.py` 改为从 artifacts 回导这些符号（artifacts 已是其既有
依赖，`scripts/ai_workflow.py:160`），routing.py 既有
`_write_json_once` 惰性 seam 删除、改为模块级 import artifacts。
新业务模块**只**通过 `TaskStoreProtocol` 声明的 store 方法
（`lock` / `_require_task` / `append_event` / `write_task_artifact_once`
/ `append_task_ledger` / `read_task_ledger` / `_assert_lock_held`）
做 I/O，运行时（模块级与函数级）一律不得 import `ai_workflow` 或
`ai_workflow_repairs`。

```text
artifacts（叶子；宿主内核原语 + TaskStoreProtocol + PROCESS_GENERATION）
routing        → artifacts
planning       → artifacts
declarations   → routing, artifacts
candidate_state→ artifacts, planning
authorizations → artifacts
verdicts       → candidate_state, authorizations, artifacts
ownership      → planning, authorizations, artifacts
                 （Task 09 起消费 authorizations 的 lease/授权读取原语）
side_effects   → ownership, candidate_state, artifacts
preflight      → declarations, artifacts（读已存声明取 route_config_hash；
                                     禁止 import sync_plugin）
evidence       → declarations, preflight, artifacts（launch intent 与
                 runtime-evidence-2 生产者原语）
dispatch_policy→ declarations, preflight, routing, ownership,
                 side_effects, artifacts
ai_workflow    → dispatch_policy, side_effects, evidence, artifacts
                 （内核原语回导；无环）
repairs        → verdicts, candidate_state, authorizations, evidence,
                 dispatch_policy（Task 13 起：执行汇点 2 许可原语与释放
                 守卫 helper；既有 _workflow() 惰性 seam 保持不变）
scheduler      → declarations（只读校验）
evidence_chain → declarations, candidate_state, verdicts, evidence,
                 artifacts（只读审计 CLI；不进 RUNTIME_FILES）
```

`preflight` 与 `dispatch_policy` 不互 import；编排（声明 → 预检 →
授权 → 派发）只发生在 `dispatch_policy` 与入口层。模块级与函数级
import 图均无环；由 `tests/test_ai_workflow_import_graph.py` 用 AST
扫描真实锁定（含函数体内部的局部 import），而不只是文档箭头。
install version 的权威来源是**数据文件**
`config/ai_workflow_runtime_files.json`（由 `sync_plugin.py --write`
生成、`--check` 校验，进 `CONFIG_FILES` 镜像清单），preflight 读取该
文件而不是 import `sync_plugin`。

## P0-1 宿主前置路由声明与派发门控

### 声明 schema 与唯一创建阶段

新增 sidecar `ai-route-declaration-1`（单文件，`route-declaration.json`）。
字段闭集：`schema_version`、`task_id`、`envelope_hash`、
`router_version`、`route_config_hash`、`selected_route`、
`allowed_roles`、`active_roles`、`rule_ids`、`reason_codes`、
`max_dispatches`、`allowed_transitions`、`declared_at_utc`。

- `allowed_roles`：任务生命周期允许出现的角色上界闭集；
  `active_roles`：当前由 `selected_route` 确定的激活角色集；
  `allowed_transitions`：未来可激活角色的允许转换图（元素为
  `from_role`/`to_role` 闭集对，空列表表示禁止任何角色变更）。
  三个概念分离，禁止用同一个列表同时承担权限上界与当前执行集。
- `envelope_hash` 不由调用者传入：`build_route_declaration` 只接收冻结的
  `RuntimeRouteDecision`，从 `decision.task_sha256` 派生
  `envelope_hash`，从 `decision.decided_at_utc` 派生
  `declared_at_utc`。同一冻结 route decision 重建的声明字节级一致，
  多入口幂等。
- `record_route_declaration` 在 `store.lock(task_id)` 内重新读取
  `task.json` 与 `route-decision.json`，强制验证：
  `artifact_sha256(task.json) == envelope_hash ==
  route-decision.task_sha256`，且 `dispatches.jsonl` 与
  `dispatch-permits.jsonl` 均为空（账本为空是顺序证据，**禁止**用
  `declared_at_utc` 与账本时间戳做墙钟比较），然后
  `write_json_once` 落盘并追加 `ROUTE_DECLARED` 事件。
- **唯一创建阶段**：`dispatch_policy.ensure_declaration_for_task` 是唯一
  创建者，只在各入口的声明创建点调用（见下）。其余任何代码路径只能
  经 `load_route_declaration` 读取并验证；`ensure_route_declaration`
  幂等：文件已存在时逐字节比对规范化 JSON，一致则返回既有声明，不一致
  即 `ROUTE_DECLARATION_CONFLICT`，绝不带新时间戳重写。
- **崩溃窗口恢复挂到权威入口（不是旁路 API）**：写入顺序冻结为「先
  `write_json_once` 落声明文件，后追加 `ROUTE_DECLARED` 事件」。
  **唯一原始字节读取者（冻结）**：模块私有
  `_read_route_declaration_bytes(store, task_id) -> bytes | None`
  是 declarations 模块内**唯一**对 `route-declaration.json` 做原始
  读取的函数（经 `store._require_task` 定位任务目录；文件缺失返回
  `None`；不做解析、不做事件 I/O）。恢复函数
  `recover_route_declaration_event`（第一行
  `store._assert_lock_held(task_id)`）只在文件存在而事件缺失时，经该
  helper 读取**既有文件字节**派生事件内容并补记（**不得**重建或改写
  声明内容）；事件存在而文件缺失 → `ROUTE_DECLARATION_CORRUPT`，
  fail-closed。**调用关系冻结**：`load_route_declaration_locked` 的
  第一条 I/O 语句就是调用 `recover_route_declaration_event`，恢复
  返回或确认无需恢复后，经**同一 helper** 读取并解析声明；自取锁包装
  `load_route_declaration` 仅取锁委派；`ensure_route_declaration` /
  `ensure_declaration_for_task` 的既有分支同样先经
  `load_route_declaration_locked`（即先恢复）再比对或创建；resume
  权威入口（`_resume_stored_task` 两分支）经所属入口的
  `ensure_declaration_for_task`/`load_route_declaration` 进入，必然
  经过恢复。**静态测试冻结的读取点不变式**：允许读取点 =
  `{recover_route_declaration_event, load_route_declaration_locked}`，
  二者都必须经 `_read_route_declaration_bytes`；AST 扫描断言该 helper
  之外无任何函数直接读取声明文件，且 recover 与 load_locked 源码均
  调用该 helper、load_locked 的 recover 调用先于读取——**禁止**「除
  load 外无人可读文件」这类把 recover 自己禁掉的断言。恢复补记事件
  失败（I/O 错误）即向上抛出 `WorkflowError`，派发链条不得继续。

### 完整派发调用图与两个最低模型执行汇点

派发路径全量清单（每条都给出门控位置，逐一有测试）：

| # | 入口 | 调用链 | 是否启动模型 | 门控 |
|---|------|--------|--------------|------|
| 1 | CLI `run --runner fake` | `run_until_gate` → `_run_role_with_technical_retry` → `FakeRunner.run` | 否 | 入口建声明 + 编排层对 `active_roles` 预检 + `_run_role_with_technical_retry` fake 分支的许可单事务（含启动认领；fake runner 执行与生产相同的授权函数与状态机，以测试真实控制流） |
| 2 | CLI `run --runner live` | `_run_live_luna` → `run_codex` | 是 | 入口建声明 + `run_codex` 顶部执行汇点许可单事务 |
| 3 | CLI `run --construction-*` | `run_enforced_construction` → `_run_role_with_technical_retry` → `CodexConstructionRunner.run_construction` → `run_codex` | 是 | 入口建声明 + `_run_role_with_technical_retry` 只读预检（早失败）+ `run_codex` 执行汇点单事务 |
| 4 | CLI `resume` | `_resume_stored_task` → `run_enforced_construction` / `run_until_gate` | 视分支 | 同 1/3；resume 入口经 `ensure_declaration_for_task`/`load_route_declaration` 先完成崩溃恢复 |
| 5 | CLI `team-call` DIRECT_L1 | `run_team_call` → 建任务 → `_run_trusted_team_call_l1` → `TeamCallProductionController.run_l1` → `run_codex` | 是 | 建任务后建声明 + `run_codex` 执行汇点单事务 |
| 6 | CLI `team-call` DIRECT_L0 | `_run_trusted_team_call_l0` → `controller.run_l0`（`L0_FIXED_ARGV` 闭集，`scripts/ai_workflow_team_call.py:74`） | 否（固定 argv、无任务、无模型） | 现有 argv 闭集 + controller 类型闭集；负向测试证明 DIRECT_L0 永远到不了 `run_codex`；若未来改动使 L0 触达模型执行，执行汇点因无任务信封必然 fail-closed |
| 7 | CLI `schedule-batch` | `dispatch_ready_batch` → `_dispatch_step_locked`（只写派发提案，不启动模型，`scripts/ai_workflow_scheduler.py:854`） | 否 | 记录提案前逐任务校验声明存在且计划角色 ⊆ `allowed_roles`；提案的后续执行走路径 3 的执行汇点；禁止 batch 级声明替代逐任务声明 |
| 8 | v2 验收 `run_assignment` | `run_assignment` → `codex exec resume` 子进程（不经 `run_codex`，`scripts/ai_workflow_repairs.py:2802`、spawn 于 `:2914`） | 是 | `run_assignment` 内子进程启动前的第二执行汇点许可单事务（许可身份绑定 `assignment_id + attempt_id`） |

**两个最低模型执行汇点**：许可单事务在 `run_codex` 顶部
（`_require_attempt_accounting_context` 调用点
（`scripts/ai_workflow.py:1703`）之后开始的临界区）与 `run_assignment`
子进程启动前各执行一次；上层入口（`_run_role_with_technical_retry` 的
live 分支等）的 `precheck_dispatch_permit` 只是只读早失败，不是安全
边界，不产生任何账本写入。所有能启动模型的路径收敛到这两个执行汇点；
上表每条路径都有「缺声明拒发、角色越权拒发、未预检拒发、预算超额
拒发、executor 未被调用（调用计数为零）」的测试。

### 许可状态机、单事务步骤顺序与崩溃语义

**状态闭集**（`dispatch-permits.jsonl` 记录字段 `state`，账本带
`seq`，完整性规则见「JSONL 账本完整性策略」）：
`RESERVED` / `STARTED`（终态）/ `RELEASED_BEFORE_START`（终态）。

**合法转换**：`∅ → RESERVED`（仅在单事务内追加）；`RESERVED →
STARTED`（spawn 成功返回后同一临界区内认领）；`RESERVED →
RELEASED_BEFORE_START`（仅当 spawn 标记可证明未置位）。终态之后同 ID
的任何记录或调用一律拒绝；对非 `RESERVED` 状态的许可调用 claim 或
release 原语即 `DISPATCH_PERMIT_STATE_ILLEGAL`（spawn 后永不释放）。

**许可单事务步骤顺序（冻结；两个执行汇点与 fake 分支共用同一段
`store.lock(task_id)` 临界区完成 1–6）**：

1. `require_dispatch_permit_locked`：第一行
   `store._assert_lock_held(task_id)`；锁内依次——声明存在（经
   `load_route_declaration_locked`，**先恢复后加载**），否则
   `ROUTE_DECLARATION_MISSING`；重读 `task.json` 与
   `route-decision.json` 验信封等式，不成立 →
   `ROUTE_DECLARATION_MISMATCH`；
   `has_unresolved_ownership_violation_locked(store, task_id)` 为真
   （**只能**调 `_locked` 版本）→
   `DISPATCH_BLOCKED_OWNERSHIP_VIOLATION`；角色 ∉
   `allowed_roles` → `ROLE_NOT_ALLOWED`；角色 ∉ 当前激活集（声明
   `active_roles` + `ROLE_ACTIVATED` 事件按账本序重放推导，**不**把
   「上一派发角色」当状态机当前状态）→ `ROUTE_TRANSITION_BLOCKED`；
   `require_role_preflighted_locked(store, task_id, role)`（context
   由预检模块**内部重算**，本函数不接收外部 context）→ 未命中
   `ROLE_NOT_PREFLIGHTED`；dispatch_id 生命周期检查（同 ID 任何状态
   再进一律拒绝：`RESERVED` → `DISPATCH_PERMIT_UNCLAIMED`、
   `STARTED` → `DISPATCH_PERMIT_ALREADY_STARTED`、
   `RELEASED_BEFORE_START` → `DISPATCH_IDENTITY_RETIRED`）；预算检查
   （最新状态 ∈ {`RESERVED`, `STARTED`} 的许可数 ≥ `max_dispatches`
   → `ROUTE_BUDGET_EXCEEDED`）；追加 `RESERVED` 记录（`seq` = 账本
   当前行数 + 1）；角色 ∈ `derive_effectful_roles(config)` → 同一
   临界区 `record_external_side_effect_locked(..., permit_id=...)`
   （EXTERNAL 在启动前接线，只读角色不产生）；
2. 写类角色（`TERRA_WRITE_ROLES` 及 v2 assignment）：同一临界区
   `require_write_ownership_locked(..., permit_id=permit.permit_id,
   paths=claimed_write_paths(...))`——声称路径门与 transfer lease
   锁内原子扣减，lease 记录绑定本次 `permit_id`；
3. 执行汇点在同一临界区追加 `LAUNCH_INTENT_RECORDED`（Task 18 接线；
   fake 分支无模型启动，不写意图事件）；
4. 完成全部可失败的 spawn 前置准备（schema materialize、输出路径
   新鲜性、runtime sessions 目录校验等既有检查）；
5. `subprocess.Popen(...)` spawn——**不可返回点**（既有
   `subprocess.run` 拆为 `Popen` + `communicate`，语义等价：
   `run` 本就是 `Popen`+`communicate` 的组合）；fake 分支无 OS
   spawn，直接进入步骤 6；
6. spawn 成功返回后**立即** `claim_permit_start_locked` 追加
   `STARTED`（claim 与 Popen 之间无任何其他语句）；fake 分支在
   `runner.run(...)` 调用前认领（runner 调用即视为启动）；
7. 出锁；`communicate(input=..., timeout=...)` 等待在锁外。

**崩溃语义（写进状态机，不是实现时注意）**：

- 步骤 1–5 任一异常（spawn 标记未置位，含 `Popen` 抛错——子进程
  不存在）→ 锁外守卫调 `release_permit_if_never_spawned(store,
  permit, spawned=False, reason=...)`（helper 内部才调自取锁包装
  `release_permit_before_start`）追加 `RELEASED_BEFORE_START` 并
  原样抛出；该 dispatch_id 永久退休，重试必须用新 `attempt_id`；
- 步骤 6 之后（spawn 标记已置位）的任何异常、超时、非零退出、进程
  崩溃 → 许可恒为 `STARTED` 终态，**永不释放**（锁外守卫
  `spawned=True` 时零动作）；无法确认副作用时按副作用观测节记
  `UNOBSERVED_ASSUMED_PRESENT`（fail-closed）；
- 步骤 6 的 claim 追加自身失败而子进程已存在 → 立即 kill 子进程、
  **不释放**、原样抛出；
- 硬崩溃（断电/SIGKILL）留下的孤儿 `RESERVED` 永久占用预算额度——
  fail-closed 方向，活性损失由 `max_dispatches` 推导上限（每角色
  `1 + 技术重试上限`）吸收。

**预算口径（显式决策）**：每次执行器启动消耗一个许可；消耗额度 =
最新状态 ∈ {`RESERVED`, `STARTED`} 的许可数；`RELEASED_BEFORE_START`
释放额度供新 attempt_id 的技术重试使用。并发派发由同一把任务锁串行
化，消除 TOCTOU。

**锁纪律**：`require_dispatch_permit` / `precheck_dispatch_permit` /
`release_permit_before_start` / `has_unresolved_ownership_violation`
为自取锁包装（仅取锁后委派 `_locked` 变体）；
`require_dispatch_permit_locked` / `precheck_dispatch_permit_locked` /
`release_permit_before_start_locked` / `claim_permit_start_locked` /
`has_unresolved_ownership_violation_locked` 第一行
`store._assert_lock_held(task_id)`；`claim_permit_start_locked` 无
自取锁包装（只存在于执行汇点与 fake 分支的临界区内）。
**许可 reservation、EXTERNAL 记录、ownership lease、启动认领四者由
上述单事务在同一 `store.lock` 临界区完成**；两个执行汇点的
`with store.lock(...)` 语法范围内禁止调用任何自取锁版本（AST 范围
断言）。spawn 前失败的锁外释放只经
`release_permit_if_never_spawned` helper（dispatch_policy 提供；该
helper 是模块源码中 `release_permit_before_start` 的唯一直接调用点，
由模块级静态扫描锁定）。

### legacy 规则

- 新建 legacy-mode 任务：先 `decide_route(mode="legacy")` 并经
  `persist_or_reuse_route_decision` 冻结 route decision，再从该冻结决定
  派生声明。`ROUTE_DECISION_FIELDS` 九字段冻结不动，新信息只走新
  sidecar。
- 已有历史任务（任务目录已存在、无 `route-decision.json`）：派发时
  fail-closed（`ROUTE_DECLARATION_MISSING`）。本轮**不提供**自动迁移；
  owner 迁移工具进 backlog，且未来实现时必须要求 owner 授权工件，禁止
  生产入口读取当前配置给历史任务自动补造声明。
- 依赖「无声明直调」的旧测试 fixture 必须改走正式创建流程（建任务 →
  冻结 route decision → 建声明），不为维持测试基线加入隐式兼容旁路。

声明写入同时向 `events.jsonl` 追加 `ROUTE_DECLARED` 事件，携带
`task_id`、`envelope_hash`、`selected_route` 与声明文件哈希，供证据链
回放。声明只是本地 JSON 写入，零模型调用，不破坏确定性路由的零模型
成本基线。

## P0-2 终验新鲜度门

### digest 规范（CandidateState）

新增模块 `ai_workflow_candidate_state.py`，固定 digest 口径：

- **root**：候选工作树根，由 `capture_candidate_state` 内部从冻结任务
  信封派生（REMEDIATION → `source_worktree`，其余 →
  `repository_root`），resolve 后必须是 git worktree，否则
  `CANDIDATE_REPO_INVALID`。**接口不接收调用方 repo/root 参数**；调用方
  持有的路径只能在断言场景作为比对值，比对不一致即
  `CANDIDATE_REPO_MISMATCH`，永远不能作为权威来源。
- **baseline_commit**：由调用方从权威上下文显式供给（验收账本任务为账本
  上下文的首个 candidate commit / `base_commit`）；写入前以
  `git merge-base --is-ancestor` 验证祖先关系，无法确定基线 →
  `CANDIDATE_BASELINE_INVALID`，fail-closed。禁止用可移动分支名推导
  基线。（注意：这是 `capture_candidate_state` 的取证口径；**放行门
  与证据链读取器不接受调用方供给的 baseline**，见「放行语义」。）
- **排除规则（控制面工件不进 digest）**：`.git/`、任务状态根
  （`data/state/ai-workflow/`，含 events/账本/全部 sidecar/attempts/
  logs）、runtime sessions 目录。裁决写入自身不会改变
  `tree_digest`/`diff_digest`，裁决不会自我失效。
- **tracked diff 的排除实现口径**：`git diff --binary --full-index
  <baseline_commit> -- .` 附加每个排除目录的 pathspec
  `:(exclude)<posix 前缀>/**`（路径一律 NFC UTF-8、POSIX 分隔符、字面
  量匹配、不做大小写折叠）；作为纵深防御，再对 diff 文本逐文件段解析，
  丢弃规范化路径命中排除集的段落后才进入哈希。控制面 tracked 文件因此
  在任何一条路径上都不会进入 `diff_digest`。
- **tree_digest**：候选范围内（tracked + untracked，排除上述目录）逐文件
  记录 `<mode> <type> <path> <content_sha256>`，路径 NFC UTF-8、POSIX
  分隔符、不做大小写折叠，按路径排序后整体 sha256；mode 只区分
  `100644`/`100755`/symlink；symlink 以目标字符串为内容；submodule →
  `CANDIDATE_DIGEST_UNSUPPORTED`。
- **diff_digest**：上述过滤后 `git diff --binary --full-index` 规范化
  输出，拼接候选范围内 untracked 文件的规范化条目（路径 + 内容
  sha256）后整体 sha256——仅未跟踪文件变化同样翻转 digest。
- **准原子性（manifest 双采比对）**：候选 manifest = 全量条目元组
  `(<path>, <mode>, <kind>, <content_sha256>)`。计算顺序：记录 HEAD₁
  与 manifest₁ → 计算 tree_digest 与 diff_digest → 复读 HEAD₂ 与
  manifest₂ → `HEAD₁ != HEAD₂` 或 `manifest₁ != manifest₂` 即
  `CANDIDATE_STATE_UNSTABLE`。porcelain 只比较状态字母，内容变而状态
  不变（`M`→`M`）的竞态检不出；manifest 比对逐文件内容哈希，必然
  检出。测试以钩子函数在两次扫描之间注入「内容变、status 仍为 M」的
  修改。
- 非 git 根、git 错误 → fail-closed，不降级为「跳过 digest」。

### 不可变裁决历史与 canonical preimage

终验裁决写 append-only `final-verdicts.jsonl`（**无 seq**，完整性规则
见「JSONL 账本完整性策略」），每条记录为 `ai-final-verdict-1`：

`schema_version`、`verdict_id`、`task_id`、`envelope_hash`、内嵌完整
`candidate_state`、`verdict`（闭集 `ACCEPT`/`REJECT`）、
`verdict_source_role`、`issuer_evidence_id`、`recorded_at_utc`。

`verdict_id = content_id("ai-final-verdict-1", <投影后记录>,
exclude=VERDICT_ID_EXCLUDE)`（`VERDICT_ID_EXCLUDE =
frozenset({"verdict_id"})`）——canonical preimage 排除 `verdict_id`
自身，无自引用；`candidate_state.runtime_evidence_ids` 排序去重后进入
preimage（compute 与 verify 共用同一投影）。写入、读取、重放统一经
`verify_verdict_id`（即 `verify_content_id(...,
exclude=VERDICT_ID_EXCLUDE, id_field="verdict_id")`）重验；截断尾
记录、重复 `verdict_id`、跨任务记录逐条 fail-closed
（`VERDICT_LEDGER_CORRUPT`）。

重新终验追加新记录，不覆盖旧记录；`evaluate_verdict_freshness` 选取账本
序最新裁决，与当前 CandidateState 整体比较（commit、baseline、tree、
diff、证据集合任一变化 → `STALE`；无裁决 → `MISSING`）。

### 签发者验真（角色从证据派生，签名无角色参数）

`record_final_verdict(store, task_id, *, verdict, candidate_state,
issuer_evidence_id, recorded_at)` 锁内强制（`ACCEPT` 与 `REJECT`
同规）：

1. 在本任务 `runtime-evidence.jsonl`（经 `store.read_task_ledger`；
   生产者 `write_runtime_evidence` 只落 VERIFIED 记录，
   `scripts/ai_workflow_runtime.py:659`）中找到 canonical JSON sha256
   == `issuer_evidence_id` 的记录，找不到 →
   `VERDICT_ISSUER_EVIDENCE_UNKNOWN`；
2. 该记录 `verification_status == "VERIFIED"`，否则
   `VERDICT_ISSUER_EVIDENCE_NOT_VERIFIED`；
3. **`verdict_source_role` 从证据派生**：取该记录的 `requested_role`
   作为签发角色；∉ `FINAL_VERDICT_ISSUER_ROLES`（闭集，当前仅
   `sol_medium_reviewer`）→ `VERDICT_ISSUER_ROLE_FORBIDDEN`；
4. 该记录的观测身份四元组 **(observed_model, observed_reasoning_effort,
   observed_sandbox_policy, observed_permission_profile)** 与
   `ISSUER_ROLE_CONTRACTS[role]` 钉死值**逐字段精确相等**（当前
   `sol_medium_reviewer` → `("gpt-5.6-sol", "medium", "read-only",
   "read-only")`；该常量与 `scripts/ai_workflow_repairs.py:1309-1315`
   既有验收映射的一致性由测试锁定）——签发者确已通过 S3/S4，身份从
   运行时证据读取，不从字段自报；
5. 本任务 `events.jsonl` 中存在一条 `RUNTIME_EVIDENCE_RECORDED` 事件
   其 `runtime_evidence_sha256 == issuer_evidence_id`，否则
   `VERDICT_ISSUER_EVIDENCE_ORPHAN`（证据必须同时落在事件流与证据
   账本两处）；
6. 裁决引用的全部 `runtime_evidence_ids` 属于同一 `task_id` 且内容哈希
   逐条匹配；`candidate_state.envelope_hash` 与存储信封一致。

函数签名**没有** `verdict_source_role` 参数：记录中的该字段由上述
派生值盖章写入，调用者无法凭参数构造或伪造签发角色；任何
`inspect.signature` 内省测试断言该形参不存在。

### 放行语义与出口全量接门

**放行语义（闭集，无例外）**：完成/放行成立当且仅当账本序最新裁决
**存在** 且 **`verdict == "ACCEPT"`** 且对门内权威重算的当前
CandidateState **FRESH**。

- 最新裁决为 `REJECT` 时，无论新鲜与否一律 `VERDICT_REJECTED` 阻断；
  `VERDICT_STALE_OVERRIDE` 授权**只覆盖「状态变化导致的过期」**，
  永远不能覆盖裁决值——override 的消费前提就是「最新裁决为 ACCEPT
  且 STALE」，对 REJECT 消费直接 `AUTHORIZATION_SCOPE_MISMATCH`。
- `require_verdict_fresh_locked(store, task_id, *,
  override_authorization_id=None)` 在任务锁内**自行权威重算**当前
  CandidateState——**签名没有 `current` 参数，也没有
  `baseline_commit` 参数**：baseline 由门内重放 `final-verdicts.jsonl`
  取最新裁决的 `candidate_state.baseline_commit`（无裁决即
  `VERDICT_MISSING`，无需 baseline）；`runtime_evidence_ids` 从本任务
  `events.jsonl` 重读 `RUNTIME_EVIDENCE_RECORDED` 的
  `runtime_evidence_sha256` 集合；然后
  `capture_candidate_state(store, task_id, baseline_commit=<账本基线>,
  runtime_evidence_ids=<重读集合>)`。安全放行接口不接收调用者提供的
  baseline 或 current，出口调用者不可能凭参数伪造新鲜度。
- 校验、override 消费与完成事件写入处于同一段 `store.lock` 临界区。

验收账本任务的「完成/放行」事件全部收敛于唯一状态提交点
`_v2_append`（`scripts/ai_workflow_repairs.py:1285`）：当事件属于终末
阶段完成闭集（`REPAIR_COMPLETED` / `REVIEW_COMPLETED` 且阶段为
`SOL_XHIGH_TERMINAL_REPAIR` 或整项目终验流程的终末阶段）时，在其既有
锁块内调 `require_verdict_fresh_locked`（MISSING/STALE/REJECT 即
fail-closed）。该提交点覆盖 `complete_acceptance_assignment`、
`record_adversarial_review`、`run_assignment` 驱动的完成写入
（`scripts/ai_workflow_repairs.py:2990`）与 `schedule-final` 的终验子
任务完成；`authorize_final_xhigh`（`:2299`，自带锁块 `:2305`）在签发
终验 ticket 前同样强制。非账本任务（generic pipeline）的放行权威是
owner `decide`，不属于本门范围，卡片逐一列明并给出每出口
MISSING/STALE/REJECT 阻断测试。

`STALE`（且最新裁决为 ACCEPT）的出路只有两条：重新终验追加新裁决；或
owner 签发并消费绑定当前 `CandidateState.state_digest()` 的
`VERDICT_STALE_OVERRIDE` 授权，放行后任何后续变化再次 `STALE`。

旧账本冻结：`adversarial-acceptance-1` 事件形状不变，
`replay_acceptance_ledger`、`_v2_append` 的既有事件字段集不变；新鲜度
判定叠加在账本写入路径内部，不向旧事件增删字段。

## P0-3 所有权与副作用门

### 登记与账本（控制面分离）

新增 sidecar `ai-ownership-registry-1`（不修改 `ai-task-1` 九字段）：
`schema_version`、`task_id`、`envelope_hash`、`path_owners`（由
`scope_owner_map(plan)` 生成，键经 `normalize_scope` 规范化）、
`registered_at_utc`。

副作用账本 `side-effects.jsonl`（append-only，**无 seq**，完整性规则见
「JSONL 账本完整性策略」），`effect_kind` 闭集分为两级：

- `CONTROL_PLANE_ARTIFACT`：声明、登记器、预检记录、账本、许可等宿主
  控制面写入——**不触发**所有权锁定；
- 锁定级：`OWNED_WRITE`、`UNTRACKED_WRITE`、`COMMAND_GENERATED`、
  `EXTERNAL`、`UNOBSERVED_ASSUMED_PRESENT`。

`has_ownership_locking_side_effect` 只统计锁定级，控制面工件不会把任务
在首次业务写入前自锁。`OWNERSHIP_VIOLATION_RECORDED` 是
`events.jsonl` 的事件类型，**不是** `effect_kind`；`EFFECT_KINDS`
闭集不含、也永不加入该值——violation 的持久化只走事件流（见
「聚焦转让」节），`side-effects.jsonl` 永不承载 violation 账本项。

### 真实副作用观测与 producer 契约

`record_side_effect` 只是记录 API，**不是**证据来源。权威观测由
`ai_workflow_side_effects.py` 提供：

- `capture_fs_snapshot` / `diff_fs_snapshots`：执行器前后对候选工作树做
  确定性快照与 diff（复用 CandidateState 的 digest 原语与排除规则），
  覆盖新增、修改、删除与未跟踪文件；
- **COMMAND_GENERATED 的 producer 是命令执行上下文，不是 FS diff**：
  `run_codex` 与 `run_assignment` 本来就用 `parse_codex_jsonl` 解析
  rollout 事件流（`scripts/ai_workflow_repairs.py:2929`）。观测层从
  **已解析的 rollout 事件**提取工具/命令执行记录（事件级，带
  `command_sha256`），若本次执行存在任何命令执行记录，追加一条
  `COMMAND_GENERATED` 账本项。`CommandExecution` 的 `producer` 取值
  闭集 `COMMAND_PRODUCERS = {"ROLLOUT_TOOL_EVENTS",
  "CONSTRUCTION_FROZEN_STEP"}`，不得恒定：generic/v2 路径为
  `ROLLOUT_TOOL_EVENTS`（`producer_ref` = 工具事件在事件流中的序号）；
  construction 路径为 `CONSTRUCTION_FROZEN_STEP`（`producer_ref` =
  `"<plan_sha256>:<subtask_id>"`），由 construction runner 把其冻结
  步骤上下文（`ConstructionExecutionContext.plan.plan_sha256` 与
  `.step.id`，`scripts/ai_workflow.py:2757`、`run_construction` 传参点
  `:4476`）作为结构化 producer metadata 传入观测挂钩；
  `COMMAND_GENERATED` 账本项字段携带 `producer`、`producer_ref` 与
  全部 `command_sha256s`。FS diff 只负责 OWNED/UNTRACKED/CONTROL 的
  逐路径分类，永远不被用来「猜测」命令生成（`classify_side_effect`
  静态扫描断言不产生 `COMMAND_GENERATED`）；
- **EXTERNAL 在许可时接线**：角色钉死 sandbox ≠ `read-only` 的派发属于
  `EFFECTFUL_ROLES`（可执行命令、可能产生外部副作用），许可单事务
  步骤 1 在追加 `RESERVED` 的同一临界区记录 `EXTERNAL`（可能已发生），
  发生在启动之前，不依赖事后观测；只读角色不产生 `EXTERNAL`；
- **实际变化集必须传给所有权复核**：`observe_execution_side_effects`
  返回完整 `FSChange` 元组；`run_codex` 与 `run_assignment` 把该返回
  值（不止写账本）继续传给 `verify_actual_write_paths`（绑定本次
  `permit_id`）做实际写路径复核（v2 路径已有 `actual_changed_paths`
  先例，`scripts/ai_workflow_repairs.py:2947`）；只允许在「证明未
  spawn」（spawn 标记未置位）时释放许可；
- 超时、异常、进程崩溃等无法确认副作用是否发生的情况，一律记录
  `UNOBSERVED_ASSUMED_PRESENT`（锁定级），fail-closed，不得假定零
  副作用；
- 控制面写入由宿主标记为 `CONTROL_PLANE_ARTIFACT`，与候选/外部持久
  副作用分流。

### 版本化授权 sidecar（替代 apply_owner_decision 扩张）

新增 `owner-authorizations.jsonl`（append-only，**无全局 seq**；
`transfer_lease` 的 `dispatch_seq` 为按 `authorization_id` 局部连续
序号，完整性规则见「JSONL 账本完整性策略」），schema
`ai-owner-authorization-1`。字段词汇表（并集）：
`schema_version`、`record_kind`（`authorization` / `consumption` /
`transfer_lease`）、`authorization_id`、`record_id`、
`authorization_type`（闭集 `VERDICT_STALE_OVERRIDE` /
`OWNERSHIP_TRANSFER`，两类不共用模糊通道）、`task_id`、
`envelope_hash`、作用域字段（stale override →
`candidate_state_digest`；transfer → `path`、`from_role`、`to_role`、
`allowed_paths` 闭集、`max_dispatches`）、consumption 的 `binding`、
lease 的 `permit_id`/`dispatch_seq`/`allowed_paths`、`actor`、
`owner_evidence_id`（指向 `human-decisions.jsonl` 中同 actor 的 owner
决定记录，复用既有 owner 认证通道）、`issued_at_utc`。

**wire 字段按 `record_kind` 闭集分型（冻结；不适用字段强制不存在——
不是 null、不是空串）**：

- `authorization`：必有 `schema_version`、`record_kind`、
  `authorization_id`、`authorization_type`、`task_id`、`envelope_hash`、
  `actor`、`owner_evidence_id`、`issued_at_utc` + 类型作用域
  （`VERDICT_STALE_OVERRIDE` → `candidate_state_digest`；
  `OWNERSHIP_TRANSFER` → `path`、`from_role`、`to_role`、
  `allowed_paths`、`max_dispatches`）；**禁止出现** `record_id`、
  `permit_id`、`dispatch_seq`、`binding` 与另一类型的作用域字段；
- `consumption`：必有 `schema_version`、`record_kind`、`record_id`、
  `authorization_id`、`task_id`、`envelope_hash`、`binding`（本次消费
  的绑定映射，随记录落账供审计）、`issued_at_utc`；其余字段禁止出现；
- `transfer_lease`：必有 `schema_version`、`record_kind`、`record_id`、
  `authorization_id`、`task_id`、`envelope_hash`、`permit_id`、
  `dispatch_seq`、`allowed_paths`（本次声称路径的规范化排序闭集，
  ⊆ 授权 `allowed_paths`）、`issued_at_utc`；其余字段禁止出现。

全部 ID 走 canonical preimage，**exclude 常量按 record 类拆分**：

- `AUTHORIZATION_ID_EXCLUDE = frozenset({"authorization_id"})`：
  `compute_authorization_id` / `verify_authorization_id`
  （id_field=`authorization_id`）专用，只排除自身；
- `RECORD_ID_EXCLUDE = frozenset({"record_id"})`：
  `compute_record_id` / `verify_record_id`（id_field=`record_id`）
  专用，只排除自身——consumption/transfer_lease 的 `authorization_id`
  **进入** `record_id` preimage（只修改 `authorization_id` 的负向
  golden 必然 `CONTENT_ID_MISMATCH`）；
- 两类记录各有模块私有 canonical projection（`_authorization_preimage`
  / `_record_preimage`），compute 与 verify 共用：集合语义字段
  （`allowed_paths`）经 `sorted_strs` 排序去重后进入 preimage；verify
  不得对未规范化的原始列表直接哈希；
- authorization 与 consumption/transfer_lease 分别做 golden preimage
  测试（固定字段冻结记录 → 钉死哈希字面量；预填本类 ID 字段垃圾值
  不改变输出；lease 的 `allowed_paths` 不同顺序得到同一 `record_id`
  且验证通过——证明 verify 与 compute 共用同一投影）。

**锁语义**：`consume_owner_authorization` 为自取锁包装；
`consume_owner_authorization_locked` 与
`record_transfer_lease_locked` 第一行 `store._assert_lock_held(task_id)`，
供已持锁的调用方（如 `_v2_append` 临界区内的放行门、执行汇点临界区
内的转让门）使用，消除嵌套锁/死锁。`load_owner_authorization` /
`count_transfer_leases` / `leases_for_permit` / `replay_authorizations`
为纯读取函数（只经 `store.read_task_ledger`，**永不取锁**），可在
临界区内安全调用。授权校验、消费记录与被授权动作同处一段
`store.lock` 临界区。

`apply_owner_decision` 的 `OWNER_DECISIONS` 闭集
（`approve_execution`/`authorize_rework`/`authorize_escalation`/
`defer`/`close`/`abort`）**不变**，有负向测试锁定。

### 聚焦转让：scoped lease 账本与持久 violation

- **当前所有者永不改写**：`resolve_path_owner` 只从不可变登记器按
  `normalize_scope` 规范化后的最长前缀推导；转让授权**不**改变登记器，
  也不永久改变任何路径的当前所有者。
- **scoped lease**：`OWNERSHIP_TRANSFER` 授权本身不直接放行派发；每次
  依据该授权派发时，在执行汇点许可单事务内（步骤 2）追加一条
  `record_kind="transfer_lease"` 记录，绑定
  `(authorization_id, permit_id, dispatch_seq)` 与本次声称路径
  （以 lease 记录的 `allowed_paths` 字段规范化排序落账）。
  扣减在锁内原子完成：重放既有 lease 数 ≥ `max_dispatches` →
  `AUTHORIZATION_EXHAUSTED`，该授权不得再继续使用；lease 数即已消耗
  额度。授权生命周期随 lease 用尽而终结，不构成路径级永久让渡。
- 首次锁定级副作用之后签发的转让授权必须携带聚焦修复闭集
  （`allowed_paths` + `max_dispatches`），且每次派发声称路径
  `paths ⊆ allowed_paths`；副作用后原所有者写自己名下路径（聚焦修复）
  不需要授权、不被误伤。
- 派发前检查声称路径（执行汇点内 `require_write_ownership_locked`，
  绑定本次 permit），执行后用观测到的实际写路径复核：
  `verify_actual_write_paths(store, task_id, role, *, permit_id,
  actual_paths)` **签名必须接收本次 `permit_id`**；允许集 = 该角色
  登记器名下路径 ∪ **绑定本次 permit_id 的 transfer_lease 记录**
  （经 `leases_for_permit` 重放过滤）的 `allowed_paths`；**禁止**使用
  所有历史 lease 的 `allowed_paths` 并集——历史 lease 不得为后续
  dispatch 提供路径豁免（负向测试：绑定旧 permit 的 lease 覆盖路径
  X，本次 permit 无 lease 而实际写路径含 X → 判越界）。
- **violation 持久化 wire shape（冻结，方案 2——events.jsonl 唯一
  权威来源）**：实际写路径超出允许集 → 向 `events.jsonl` 追加**持久**
  `OWNERSHIP_VIOLATION_RECORDED` 事件并抛 `OWNERSHIP_VIOLATION`。
  该事件是 violation 的**唯一权威持久来源**：**不得**向
  `side-effects.jsonl` 追加 violation 账本项（`EFFECT_KINDS` 闭集不
  含该值，写入即 `SIDE_EFFECT_LEDGER_CORRUPT` 方向 fail-closed）；
  实际写副作用仍按原 effect kind（`OWNED_WRITE` 等）由观测挂钩记录，
  violation 只作为独立事件。事件字段闭集（冻结，golden 测试锁定）：
  `OWNERSHIP_VIOLATION_EVENT_FIELDS = frozenset({"event_type",
  "task_id", "envelope_hash", "permit_id", "role", "paths",
  "timestamp_utc"})`——`paths` 为越界路径的规范化排序清单；
  `permit_id` 绑定本次派发。violation 不自动清除、无 API 清除。
- **violation 查询（`_locked` 对接口，冻结）**：

  ```python
  def has_unresolved_ownership_violation_locked(store, task_id: str) -> bool:
      store._assert_lock_held(task_id)
      ...

  def has_unresolved_ownership_violation(store, task_id: str) -> bool:
      with store.lock(task_id):
          return has_unresolved_ownership_violation_locked(store, task_id)
  ```

  `_locked` 版本**只重放** `events.jsonl` 的
  `OWNERSHIP_VIOLATION_RECORDED` 事件这一权威来源：存在任一字段闭集
  合法的事件即为真；violation 事件字段越闭集/类型错误、跨任务记录、
  账本截断尾/坏行/无法重放 → 一律 fail-closed 抛
  `WorkflowError("OWNERSHIP_VIOLATION_LEDGER_CORRUPT", ...)`（阻断
  方向，不得静默返回 False）。同名无后缀函数仅取锁委派。
  `require_dispatch_permit_locked` **只能**调用 `_locked` 版本：此后
  对**所有**后续派发一律 `DISPATCH_BLOCKED_OWNERSHIP_VIOLATION`
  （含「记录 violation 后所有后续 `require_dispatch_permit[_locked]`
  均拒绝」的测试）。未知实际写路径不得放行。
- 路径一律经 `normalize_scope` 规范化（POSIX、去 `..`、拒绝对逃逸；
  symlink 与生成文件按解析后路径判定）后再判断所有者；目录授权按前缀
  闭集匹配。

## P0-4 成本证据包（仅 router probe 研究面）

### 费率快照与归档链

新增不可变 sidecar 工件 `ai-rate-snapshot-1`，字段闭集：
`rate_snapshot_id`、`skus`（每 SKU 含型号、币种、**计费单位 `unit` ∈
`RATE_UNITS`**、计费渠道、uncached input / cached input / output 分项
单价、缓存写入与长上下文阶梯价适用性标记、**每 SKU 独立 `source_url`
与 `retrieved_at`**）、`effective_at`、`retrieved_at`、`archive`
（`archive_path` 内容寻址路径 `docs/rate-archives/<archive_sha256>`、
`archive_sha256`、`mime_type`、`retrieval_status`——哈希必须可解析到
实际归档文件，不是孤立字符串）、`approved_by`、
`approval_evidence_id`。

**费率单位闭集与基数（冻结）**：`RATE_UNITS = frozenset({"PER_TOKEN",
"PER_1K_TOKENS", "PER_1M_TOKENS"})`；`RATE_UNIT_BASE = {"PER_TOKEN": 1,
"PER_1K_TOKENS": 1_000, "PER_1M_TOKENS": 1_000_000}`。SKU 单价
`price_*` 为该单位基准数量的报价（主货币单位十进制字符串）。

使用规则不变：历史运行永远绑定原快照，不追溯改价；快照不可变；用于
当前预测时过期或缺必填字段 → `PRICE_STALE` / `PRICE_UNKNOWN` 并拒绝
任何成本赢家标签；单价区间只能来自费率不确定性、token 统计误差或工作
负载分布。

### 逐臂分型结果与总计规则

探针报告升级协议版本 `router-probe-summary-2`，`cost_estimate` 为
**每臂 discriminated union**，每臂恰好是以下三型之一：

- `{"type": "COST_ESTIMATE_UNDER_SNAPSHOT", "arm_id", "usage":
  {"uncached_input", "cached_input", "output"}, "usage_source":
  "BILLING_USAGE", "usage_evidence_ids", "sku", "currency", "unit",
  "estimated_cost_minor", "quality": {"retries", "escalations",
  "reviews", "failures"}}`——只有带证据 ID 的权威计费 usage 臂可为此
  型；
- `{"type": "TEXT_TOKEN_ESTIMATE", "arm_id", "tokens": {...},
  "usage_source": "TEXT_TOKEN_ESTIMATE"}`——文本 token 估计降级单列，
  **不含任何金额**，不混入账单口径；
- `{"type": "USAGE_AUTHORITY_UNAVAILABLE", "arm_id", "reason"}`——无
  权威 usage 的臂。

**wire shape 一致性（冻结）**：逐臂 `usage` 对象与任何汇总输入总量
字段使用同一个三键形状 `{"uncached_input": int, "cached_input": int,
"output": int}`；`TEXT_TOKEN_ESTIMATE` 臂的 `tokens` 亦同形。

**总计规则（fail-closed）**：

- 仅当**全部**实际运行臂都是 `COST_ESTIMATE_UNDER_SNAPSHOT`、且所有臂
  `currency` 与 `unit` 完全一致时，才输出
  `{"type": "COST_TOTAL_UNDER_SNAPSHOT", "total_cost_minor", ...}`；
- 任一臂降级或不可用、币种/单位不一致 → 总计为
  `{"type": "COST_TOTAL_UNAVAILABLE", "reason": "PARTIAL_AUTHORITY" |
  "CURRENCY_MISMATCH" | "UNIT_MISMATCH"}`，**不携带数值**；部分权威
  总计伪装全路线成本视为缺陷；
- **数值与换算（可实现口径，冻结）**：`input_tokens ==
  uncached_input + cached_input` 不成立即拒绝该臂 usage
  （`COST_INPUT_INVALID`）；任何 token 数或单价为负即
  `COST_INPUT_INVALID`。金额计算在
  `with decimal.localcontext(decimal.Context(prec=28,
  rounding=ROUND_HALF_EVEN)):` 内进行（**禁止** `Decimal(prec=28)`
  之类的不可实现表述——`Decimal` 构造器没有 `prec` 形参）；token 数
  → 报价单位换算公式：`cost = (Decimal(tokens) * Decimal(price)) /
  Decimal(RATE_UNIT_BASE[unit])`（`price` 为 SKU 分项单价的十进制
  字符串）；minor-unit 换算：`minor = (cost * (10 **
  CURRENCY_MINOR_UNITS[currency])).quantize(Decimal("1"),
  rounding=ROUND_HALF_EVEN)`，`estimated_cost_minor = int(minor)`——
  **输出类型为最小货币单位的整数计数（JSON int，如 USD 的美分）**，
  `total_cost_minor` 为逐臂 int 精确求和；禁止二进制浮点进入报告。

顶层 `type` 闭集不变：`COST_ESTIMATE_UNDER_SNAPSHOT` / `PRICE_STALE` /
`PRICE_UNKNOWN` / `UNAVAILABLE_WITHOUT_RATE_SNAPSHOT`（顶层不再用
`USAGE_AUTHORITY_UNAVAILABLE` 单串遮蔽逐臂状态；逐臂不可用时顶层仍可
为 `COST_ESTIMATE_UNDER_SNAPSHOT` 但总计按上规则不可用）。本轮不做
质量调整成本模型：报告禁止任何「路线成本比较/更便宜」结论；既有
`CACHE_MECHANISM_CANDIDATE_*` 判定与 `cost_comparison_status` 闭集逐
字段不变；`effective_route` 恒为 `UNCHANGED`；成本字段不进入
`evaluate_optimization_gate`。旧读取器对 `router-probe-summary-2` 新
字段的行为有兼容测试；新成本臂不与 R1–R3 样本合并为同一结论。

本工件只服务 router probe 研究面：不接生产路由、不进优化门、不改
`effective_route`、不宣布赢家。

## P1-1 按路由裁剪预检

新增模块 `ai_workflow_preflight.py`（依赖 declarations 与 artifacts，
**禁止** import `ai_workflow`/`sync_plugin`）：

- `PreflightContext`：`task_id`、`route_config_hash`、
  `runtime_profile_hash`、`install_version`、`launcher_version`、
  `cwd`、`worktree_id`、`process_generation`。
- **权威重算，零调用方可控因子**：`compute_preflight_context(store,
  task_id, *, role)` 在函数内部完成全部取值（第一行
  `store._assert_lock_held(task_id)`）——`route_config_hash` 从已存
  `route-declaration.json` 经 `load_route_declaration_locked` 读取
  （先恢复后加载；读不到 → `ROUTE_DECLARATION_MISSING`）；
  `runtime_profile_hash` 从安装根（`Path(__file__)` 定位）的
  `config/ai_workflow.toml` 解析该角色钉死字段（model/reasoning_effort/
  sandbox/permission 派生档）后哈希；`install_version` 读取数据文件
  `config/ai_workflow_runtime_files.json` 的聚合哈希；
  `launcher_version` 为模块常量；`cwd` 取 `os.getcwd()`；
  `worktree_id` 从任务信封仓库路径 `git rev-parse --show-toplevel`
  解析；`process_generation` 取宿主进程级常量（artifacts 内核在
  import 时生成一次）。
- **生产安全入口一律内部重算 context，不接收调用者构造的
  PreflightContext**：
  - `run_role_preflight(store, task_id, role)`（自取锁包装）/
    `run_role_preflight_locked(store, task_id, role)`：锁内先
    `context = compute_preflight_context(store, task_id, role=role)`，
    再调模块私有纯函数 `_run_preflight_checks(role, context)` 执行
    宿主静态检查，然后追加预检记录；
  - `is_role_preflighted(store, task_id, role)` /
    `is_role_preflighted_locked(...)`：内部重算当前 context 后与
    账本记录匹配；
  - `require_role_preflighted(store, task_id, role)` /
    `require_role_preflighted_locked(...)`：同上重算，未命中即
    `ROLE_NOT_PREFLIGHTED`；
  - 仅存的显式 context 入口是模块私有纯函数
    `_run_preflight_checks(role, context)` 与
    `_preflight_record_matches(records, role, cache_key)`（下划线
    前缀、无 store 形参、不做 I/O，只供测试直调）；三个公开安全
    入口的签名用 `inspect.signature` 内省测试钉死无 `context` 形参，
    模块源码静态扫描断言公开入口内部调用
    `compute_preflight_context`。
- 缓存记录 `preflight-records.jsonl`（append-only，**无 seq**，完整性
  规则见「JSONL 账本完整性策略」；支持同任务多角色、多 key 版本；
  失效后重检追加新记录，不覆盖）。任一上下文因子变化即不命中；缓存
  不跨任务。
- 预检只做宿主静态能力/可用性检查（角色闭集、角色配置钉死
  model/effort/sandbox/permission、runtime sessions 目录、schema 文件
  齐备），不调用模型；执行体签名不接受任何 executor 参数。

编排顺序（生产接入点，`dispatch_policy` 与入口层负责）：

1. 冻结 route decision；
2. 写/验证 route declaration（唯一创建阶段，含崩溃恢复）；
3. 仅对声明 `active_roles` 做宿主静态预检（`allowed_roles` 上界内但未
   激活的角色不预检）；
4. 锁内 `require_dispatch_permit_locked`（验证声明、角色、激活集、
   预检命中、预算；**只验证，不隐式补做预检**）；
5. 派发；每次 rollout 仍执行 S3/S4（`verify_runtime_identity`）身份
   验收，预检命中不得跳过（spy 测试锁定调用次数）。

升级：合法转换先 `activate_role` 落 `ROLE_ACTIVATED` 事件，再对新激活
角色显式 `run_role_preflight`，再许可派发；跳过补预检直接派发 →
`ROLE_NOT_PREFLIGHTED`。

## P1-2 身份前置原型（隔离实验）

以独立脚本加 manifest 的形式研究「宿主权威元数据前置门控」，协议版本
`identity-probe-1`，与 R1–R3 探针协议完全隔离、不合并。

- **双钥匙下沉到唯一 runner**：模块中**唯一**接受 executor 可调用对象
  的入口是 `run_identity_probe(manifest, *, config, allow_live_model,
  executor, executor_kind, experiment_root)`；其第一条语句即
  `require_identity_probe_authorized(config, allow_live_model=
  allow_live_model, executor_kind=executor_kind)`（配置
  `identity_probe.enabled`（默认 `false`）+ CLI `--allow-live-model`
  + executor 分类闭集 `DRY_RUN`/`FAKE`/`LIVE`），`LIVE` 缺任一钥匙即
  `IDENTITY_PROBE_NOT_AUTHORIZED` 且 executor 调用计数为零。模块不
  暴露任何「可直接传 LIVE callable 且无双钥匙参数」的次级 runner。
- **预算 reservation 在每次 executor 调用之前，且预约束使用权威
  上限**：manifest 必填三个正整数预算字段——`max_calls`、
  `max_output_tokens`（总输出预算）与 **`max_output_tokens_per_call`
  （每调用最大输出额度，权威来源，正整数）**。每次调用前检查：
  `calls_made >= max_calls` → 停止（`stop_reason="MAX_CALLS"`）；
  `tokens_used + max_output_tokens_per_call > max_output_tokens` →
  停止（`stop_reason="MAX_OUTPUT_TOKENS"`）且**该次 executor 调用不
  发生**（调用次数恰好停在预算线，负向测试断言为零/不超线）。禁止
  使用任何未定义或可低估的「预计输出」参与预约束。调用返回后按权威
  usage 实报实销累加 `tokens_used`；若单次实际输出超过
  `max_output_tokens_per_call`，记录 `PER_CALL_CAP_EXCEEDED` 并立即
  停止（一次超额是有界且显式记录的 fail-closed 事件）。manifest
  记录协议版本、实验根目录、预算消耗与停止原因。
- **身份字段权威来源**：manifest 与记录显式分开
  `requested_launch_intent`、`observed_runtime_identity`、
  `model_text_output` 三段；模型文本**不得**填充 model、effort、
  sandbox、permission、fork/nested 任何身份字段；缺权威元数据时结果
  为 `AUTHORITY_UNAVAILABLE`，不得以自报补齐。字段污染负向测试：即使
  模型输出包含看似合法的 model/effort/sandbox 字段，也不能进入
  observed identity。`NO_OP`/`ONE_TURN`/`TWO_TURN` 只用于测量成本，
  不用于证明身份。
- A/B 三臂逐次保留原始权威 usage（uncached/cached/output 三段）、arm
  配置哈希、runtime metadata、缓存状态与失败记录；聚合输出总量、均值、
  min/max、p50/p90 与配对差值，不只有均值；样本数为 0 的臂标记
  `OBSERVATION_ONLY`。
- 不接生产链：不 import 生产 store 写路径，不写 task store 或事件账本，
  产物只写独立实验根目录；脚本不进 `RUNTIME_FILES`，只分发 schema。
- 结论上限：前置证据只能降低错误配置进入施工的概率，永远不能替代
  S3/S4 事后 rollout 验证；未证明低开销前不设为全局握手。

## P1-3 证据链

### 生产者契约（先生产者，后读取器）

每个链节都有版本化契约与明确生产位置：

1. 路由声明：`ai-route-declaration-1` + `ROUTE_DECLARED` 事件
   （生产者：唯一创建阶段，P0-1；崩溃恢复挂在加载/ensure/resume
   权威入口）；
2. 启动意图：新事件 `LAUNCH_INTENT_RECORDED`，由两个执行汇点在许可
   单事务步骤 3（许可预留后、子进程启动前的同一临界区）写入（生产者：
   `run_codex` 与 `run_assignment`），携带 `task_id`、
   `envelope_hash`、`permit_id`、`role`、命令摘要 `command_sha256`、
   工具映射哈希 `tool_mapping_sha256`、`route_config_hash`、
   `launcher_version`、`install_version`；`event_id = content_id(
   "ai-launch-intent-1", <投影后事件>, exclude=LAUNCH_INTENT_ID_EXCLUDE)`；
   事件形状以 golden 测试冻结；
3. rollout 有效身份：既有 `RUNTIME_EVIDENCE_RECORDED` 事件
   （`scripts/ai_workflow.py:1906` 与 v2 controller 路径
   （`scripts/ai_workflow_repairs.py:2665`）已生产），事件形状不变；
   本卡补钉字段存在性测试；
4. fork/nested 状态：**版本化生产者契约**——新增 append-only sidecar
   `runtime-evidence-v2.jsonl`（schema `ai-runtime-evidence-2`，
   **无自身 seq**，`event_index` 指向事件行序，完整性规则见「JSONL
   账本完整性策略」），由上述同一生产点在 `RUNTIME_EVIDENCE_RECORDED`
   落账后立即追加，字段：`schema_version`、`evidence_id`
   （`content_id("ai-runtime-evidence-2", record,
   exclude=RUNTIME_EVIDENCE_ID_EXCLUDE)`）、`task_id`、
   `envelope_hash`、`event_index`（指向 events.jsonl 中的对应事件
   序号）、`observed_agent_type`、`native_agent_id`、
   `native_thread_id`、`fork_state`、`nested_state`、
   `recorded_at_utc`。`fork_state`/`nested_state` 取值闭集
   **`VERIFIED_NONE` / `VERIFIED_PRESENT` / `AUTHORITY_UNAVAILABLE`**：
   宿主依据 `inspect_agent_runtime`
   （`scripts/ai_workflow_runtime.py:534`）的观测元数据判定；观测字段
   缺失 → `AUTHORITY_UNAVAILABLE`，**缺字段 ≠ 没有 fork**，读取器不得
   把缺失当作 `VERIFIED_NONE`；`fork_turns=none` 之类的请求配置值不能
   单独作为运行事实；
5. 终验裁决：`final-verdicts.jsonl` 及其新鲜度状态（P0-2）。

### 只读读取器与审计 CLI

`build_evidence_chain(store, task_id)` 五链节全部以
`task_id + envelope_hash` 连接；终验链节的新鲜度由读取器内部权威
重算：**签名无 `current` 也无 `baseline_commit` 参数**——baseline
由读取器内部重放 `final-verdicts.jsonl` 取最新裁决的
`candidate_state.baseline_commit`（无裁决 →
`CHAIN_MISSING_FINAL_VERDICT`），证据集合从本任务 `events.jsonl`
重读，再经 `capture_candidate_state` 重算当前 CandidateState 后评估；
`runtime_evidence_ids` 按集合规范化（排序去重）比较，并逐条
`verify_content_id` 验证归属与内容哈希——不信任裁决自报的 ID。缺口
返回机器可读代码（`CHAIN_MISSING_ROUTE_DECLARATION`、
`CHAIN_ENVELOPE_MISMATCH`、`CHAIN_MISSING_LAUNCH_INTENT`、
`CHAIN_MISSING_ROLLOUT_IDENTITY`、`CHAIN_FORK_STATE_UNVERIFIED`、
`CHAIN_MISSING_FINAL_VERDICT`、`CHAIN_VERDICT_STALE`、
`CHAIN_EVIDENCE_ORPHAN`）。

**分发决定**：证据链构建器只被独立审计 CLI 使用，生产 workflow 从不
import 它；为守住「最小 sidecar 扩展」与最小生产插件面，
`ai_workflow_evidence_chain.py` **不进** `RUNTIME_FILES`（与
router probe、identity probe 同规），只随仓库分发脚本本身，插件镜像不
携带。证据链只读，不回写任何账本。

## Backlog（本轮不出卡）

- **P2 代理预算闭集**：任务信封级 `max_parallel_agents`、并发
  reservation、嵌套派发、fork 子任务预算继承、取消后预算回收。本轮
  P0-1 已把现有 `max_dispatches` 做成锁内原子，并发竞态不留 backlog。
- **P2 owner 迁移工具**：历史无信封任务的声明迁移，必须要求 owner 授权
  工件；本轮历史任务一律 fail-closed。
- **P2 事务化安装与前向 CI**：备份、原子替换、失败回滚，以及畸形
  rollout、字段冲突、POSIX/Windows 路径、升级迁移的前向场景测试。
  （digest 与所有权判断的最小路径规范已在本轮 P0 内，不延期。）
- **P2 质量调整成本模型**：人工时间、延迟、长期工作负载分布后续扩展；
  本轮成本输出已记录失败/重试/升级/复核量并禁止未经质量门的路线
  比较结论。
- **P2 来源与许可证核查**：本轮完全原创实现；一旦引用或搬运外部同名
  脚本，来源、许可证和提交历史核查立即升级为施工前 P0。
- **P3 角色/模型解耦 ADR**：仅记录迁移触发条件（首次模型代际替换、
  第二供应商接入、单模型承担多个稳定角色）与双字段 schema 方向
  （`role_contract_id` 与运行时钉死字段分离）；不改现有角色名、闭集
  映射或运行时钉死。

## 不采纳项（裁定原文，逐条保留）

1. 子模型自然语言握手作为身份证明。
2. 有费率快照即可宣布真实成本赢家。
3. 主控模型自由输出路由模式与风险理由。
4. Luna/Terra/Sol 单一升降等级。
5. 跨任务预检缓存、预检替代 rollout 身份验收。
6. 立即模型中立改名或取消模型钉死。
7. 仅以 owned 文件首次写入作为副作用边界。
8. 「同名同构即同源」及由此直接搬运代码。
9. 把其他项目的 Luna 默认值升级为本项目实测推荐；README 现有非实测
   免责声明继续保留（收口卡有负向文档检查）。

## 冻结面与工程约束

- `ai-task-1`、`ai-result-1`、`ai-route-decision-1` 九字段
  （`ROUTE_DECISION_FIELDS`）与 `adversarial-acceptance-1` 账本事件形状
  不动；新信息一律走新 sidecar。冻结面以 golden 序列化测试锁定。
- `OWNER_DECISIONS` 闭集不动；owner 新授权只走
  `ai-owner-authorization-1` sidecar。
- 纯标准库，Python 3.11；不引入新依赖。
- 新 schema 进 `scripts/sync_plugin.py` 的 `CONFIG_FILES`；被生产
  workflow 导入的新模块进 `RUNTIME_FILES`；identity probe、证据链审计
  CLI 与 router probe 一样不进 runtime，只分发 schema/脚本。改完必须
  `python3.11 scripts/sync_plugin.py --write` 且 `--check` 通过。
- **回归基线先行**：施工卡 **Task 00** 在任何生产卡之前，从固定 base
  commit 生成并提交 `tests/baseline_manifest.json`（全部测试 ID +
  skip 原因清单 + 采集命令与 base commit）；此后每卡验收都要求清单内
  用例保持通过、skip 语义不变，新增测试全绿；收口卡只能复核该
  manifest，不得首次生成。禁止用固定总数做回归判据。
- 所有门控默认 fail-closed；任何「缺声明自动补写」「预检替代验收」
  「快照改价」「调用者自报副作用/身份」「部分权威总计冒充全路线成本」
  之类的便捷路径一律视为缺陷。

## 停止线与回滚

出现以下任一情况立即停止：门控被发现可绕过（fail-open）、冻结 schema
被迫变更、旧账本事件形状被迫变更、live 实验成本超预算、缓存/计费行为
不可重复。所有新机制都是叠加 sidecar 与拒发逻辑，回滚按施工卡逆序撤
分支即可；`effective_route` 全程保持 `UNCHANGED`，生产路由从未改变。

## 第三次复审 14 条闭合对照

| # | 复审要求 | 闭合位置 |
|---|----------|----------|
| 1 | 许可状态机：合法转换 `∅→RESERVED`、`RESERVED→STARTED`、`RESERVED→RELEASED_BEFORE_START`，终态后同 ID 一律拒；同 ID 幂等返回窗口整体取消（比「只允许认领前幂等」更强）；启动后同 ID 再 reservation/再 spawn 均拒的负向测试 | 关键定义 DispatchPermit 节 + P0-1 许可状态机节（卡 12、13） |
| 2 | 冻结 `require_dispatch_permit_locked` / `release_permit_before_start_locked` / `claim_permit_start_locked`；wrapper 只取锁委派；汇点内禁止自取锁版本；reservation、EXTERNAL、lease、启动认领同一临界区；spawn 前崩溃可释放、spawn 后崩溃 fail-closed 不释放 | 关键定义锁协议节 + P0-1 单事务节（卡 12、13） |
| 3 | 单事务步骤顺序（1–7）与 spawn 前后崩溃状态写进状态机，不散落 | P0-1「许可状态机、单事务步骤顺序与崩溃语义」节（卡 12、13） |
| 4 | `require_verdict_fresh[_locked]` 删除调用者可控 `baseline_commit`；门内重放裁决账本取最新裁决 baseline 再 `capture_candidate_state`；证据集合锁内重读 | P0-2 放行语义节（卡 05、19 同规） |
| 5 | `record_final_verdict` 的 `verdict_source_role` 从 issuer 证据的 `requested_role` 派生并盖章，签名无角色参数；四元组精确匹配；证据须同时落在 runtime-evidence.jsonl 与事件流 | P0-2 签发者验真节（卡 04） |
| 6 | `verify_content_id(kind, record, *, exclude, id_field)` 与生成共用同一 exclude；各类 record 的 exclude 常量冻结进接口；authorization 与 consumption/lease 分别 golden preimage；禁止「生成排两个、验证排一个」（**第四次复审进一步要求 exclude 按 record 类拆分，见下表**） | 关键定义内容寻址节（卡 01 内核，04、08、18 各自落点） |
| 7 | construction 冻结步骤 producer：`COMMAND_PRODUCERS` 闭集、`producer_ref` 结构化字段、`construction_step_producer_ref`/`retag_command_executions` 提取接口；`CommandExecution.producer` 不恒定；`classify_side_effect` 仍不得猜测 `COMMAND_GENERATED` | P0-3 观测节（卡 07、13） |
| 8 | `verify_actual_write_paths` 签名接收本次 `permit_id`，只查绑定该 permit 的 lease；历史 lease 不提供豁免；负向测试（**violation 持久化 wire shape 由第四次复审进一步冻结为 events.jsonl 唯一权威来源，见下表**） | P0-3 聚焦转让节（卡 09、13） |
| 9 | `RATE_UNITS` 闭集与 `RATE_UNIT_BASE` 基数；token→报价公式；`decimal.localcontext(Context(prec=28, ROUND_HALF_EVEN))`；`estimated_cost_minor` 为最小货币单位整数计数（int）；逐臂与汇总 usage wire 同形 | P0-4 费率节与逐臂分型节（卡 10、11） |
| 10 | Task 12 依赖 02、07、09、14，不与 09 并行；索引、依赖表、并行批次全部修正 | 施工卡索引与 DAG（卡 12） |
| 11 | `run_role_preflight` / `require_role_preflighted` / `is_role_preflighted` 生产入口内部重算 context；显式 context 版本仅为 `_` 前缀模块私有纯函数；`require_dispatch_permit_locked` 内部经 `require_role_preflighted_locked` 重算 | P1-1（卡 12、14、15） |
| 12 | manifest 增加权威 `max_output_tokens_per_call`（正整数必填）；预约束 `tokens_used + max_output_tokens_per_call > max_output_tokens` 即停且不调用；禁止「预计输出」 | P1-2（卡 16、17） |
| 13 | `recover_route_declaration_event` 挂到 `load_route_declaration_locked`（第一 I/O 语句）、`ensure_route_declaration`/`ensure_declaration_for_task` 与 resume 权威入口；恢复只补事件；补记失败不得继续派发（**唯一读取点表述由第四次复审修正为「唯一原始字节读取者 + 两个受控读取点」，见下表**） | P0-1 声明节（卡 02、13、14） |
| 14 | 逐账本 seq 策略二选一冻结：仅 `dispatch-permits.jsonl` 带 `seq`（重复/断档/起点 + 状态机验证）；其余五个账本无 seq，删除虚假「重复 seq」声称，改内容 ID 唯一性 + 局部 `dispatch_seq` 连续性 + 行序 + `event_index` 引用完整性 | 关键定义「JSONL 账本完整性策略」节（卡 04、06、08、12、14、18） |

## 第四次复审 5 条闭合对照

| # | 复审要求 | 闭合位置 |
|---|----------|----------|
| 1 | exclude 按 record 类拆分：`AUTHORIZATION_ID_EXCLUDE = frozenset({"authorization_id"})`、`RECORD_ID_EXCLUDE = frozenset({"record_id"})`；consumption/transfer_lease 的 `authorization_id` 必须进入 `record_id` preimage；废止共用 `OWNER_AUTH_ID_EXCLUDE`；每类专用 canonical projection 由 compute 与 verify 共用（含 `allowed_paths` 排序去重）；wire 上不适用的另一类 ID 字段强制不存在（不是 null/空串）；负向 golden：只修改 `authorization_id` 必须导致 `record_id` 验证失败 | 关键定义内容寻址节 + P0-3 授权 sidecar 节（卡 01 内核、卡 08 落点） |
| 2 | 补齐 violation 查询 `_locked` 接口：`has_unresolved_ownership_violation_locked` 第一行 `_assert_lock_held`，同名包装仅取锁委派；`require_dispatch_permit_locked` 只能调 `_locked` 版本；对 `require_dispatch_permit_locked` 与 `require_write_ownership_locked` 的完整传递调用图做 AST/源码检查，持锁路径禁止进入任何自取锁 wrapper | 关键定义锁协议节 + P0-3 聚焦转让节（卡 09、12） |
| 3 | violation 合法持久化 wire shape（选定方案 2）：`events.jsonl` 的 `OWNERSHIP_VIOLATION_RECORDED` 事件为唯一权威持久来源；删除向 `side-effects.jsonl` 追加 violation 账本项的一切要求；`EFFECT_KINDS` 闭集不含该值；实际写副作用仍按原 effect kind 记录；事件字段闭集冻结（`event_type`/`task_id`/`envelope_hash`/`permit_id`/`role`/`paths`/`timestamp_utc`）；查询只重放该权威来源，坏记录/跨任务/无法重放 fail-closed；记录 violation 后所有后续 `require_dispatch_permit[_locked]` 均拒绝的测试 | 关键定义 JSONL 完整性策略节 + P0-3 登记账本节与聚焦转让节（卡 06、09、12） |
| 4 | recovery 唯一读取点自相矛盾修正：`_read_route_declaration_bytes` 为唯一原始字节读取者；`recover_route_declaration_event` 经该 helper 读既有声明字节、只补事件、不改写文件；`load_route_declaration_locked` 第一条 I/O 语句调 recover，然后经同一 helper 加载/解析；静态测试允许读取点 = `{recover, load_locked}` 且均经 helper；禁止「除 load 外无人可读」式断言；ensure/resume 仍必须经 load_locked，不得绕过 recover | P0-1 声明节（卡 02、13、14） |
| 5 | Task 13 释放守卫静态断言修正：持锁临界区（`with store.lock(...)` 语法范围）内禁止自取锁 wrapper（AST 范围断言）；spawn 前失败后的锁外守卫必须经 helper `release_permit_if_never_spawned`（仅 `spawned=False` 时调自取锁包装 `release_permit_before_start`）；helper 是该包装的唯一直接调用者；`inspect.getsource` 只许正向确认汇点含 `_locked` 与 helper；**删除并对施工卡禁止**对整个 `run_codex`/`run_assignment` 源码断言不含 `release_permit_before_start` | 关键定义锁协议节 + P0-1 锁纪律节（卡 12、13） |

第三次复审 CLOSED 的 4 项、第四次复审 CLOSED 的 10 项与更早 CLOSED
的 7 项语义全部保留，未回退；本版仅修改上述 5 条对应的接口、wire
shape 与测试冻结，卡号不变。
