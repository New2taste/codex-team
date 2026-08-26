# Codex Team 架构说明

Codex Team 是一个本地、可恢复、可审计的半自动编排层。它把“谁可以做什么”固化成配置、Schema、运行时证据和人工闸门，而不是让模型自由解释路由规则。

## 1. 执行面

| 执行面 | 默认用途 | 身份约束 | 权限边界 |
|---|---|---|---|
| `NATIVE_SUBAGENT` | Luna Max 默认路径 | `role=luna`、`gpt-5.6-luna/max`、`agent_type=null`、native agent/thread UUID | 由冻结 envelope 决定；通常只读或有界写 |
| `CODEX_EXEC_ROLE_CONTRACT` | 明确授权的独立 `codex exec` 会话 | 独立记录，不能冒充原生子代理 | 由任务信封和 assignment capability 决定 |

原生身份必须由控制器签发并由运行时证据闭环证明。模型、推理档、执行面、权限、沙箱、cwd 或 UUID 缺失/冲突时，流程 fail-closed；调用者自报不能补齐证据。

## 2. 默认角色

| 角色 | 负责什么 | 明确不负责什么 |
|---|---|---|
| Luna Max | 冻结 envelope 内的机械 coding、确定性检查、证据抽取、分发同步 | planning、review、语义仲裁、final acceptance |
| Terra xhigh | 复杂施工、调试、集成、开放式问题拆解 | merge、push、自我验收 |
| Sol medium | 所有工程小节完成后的集中、只读、对抗式 final acceptance | 普通 construction、常驻 planning |
| Sol xhigh | owner-authorized planning；Sol-medium 梯级失败后的 terminal repair | 普通施工、绕过 final acceptance |

角色名称、模型和推理档是独立字段，必须来自闭集配置，不能用“同名路径”或调用者自报替代运行时身份。

## 3. 生命周期

### 正常路径

```text
用户目标
  → task envelope / Schema 校验
  → 控制器确定性检查或 DIRECT_L1 Luna 有界事实抽取（需要时）
  → Terra 只读规划（仅缺少可执行计划时）
  → Luna 有界施工或 Terra 复杂施工
  → 各工程小节完成自检
  → 固定 candidate commit
  → Sol medium 集中 final acceptance
  → 人工 owner decision
```

中间工程小节不再单独派发对抗式审查，但施工 owner 仍必须执行冻结信封内的目标测试、负向检查、范围核对和运行时证据门。这里的自检不能被写成“独立验收”。

`ACCEPTANCE` task 的 Terra xhigh reviewer 是显式本地审查入口，不代表全工程终验。正常计划调度在全部工程小节 receipt 完成后生成唯一 whole-project `ACCEPTANCE` child。final candidate 可以不同于 FrozenPlan 初始 candidate，但必须是当前 clean HEAD、初始 candidate 的 git 祖先后代，且 diff 落在授权 write union 内；parent ledger 用 `acceptance_task_sha256` 绑定完整 child，child 的 `scheduler-parent.json` 反向定向绑定唯一 parent task、plan、final event 和 candidate，分类时不扫描无关任务。`schedule-final` 通过现有 adversarial-acceptance-1 API 只签发一次 Sol-medium `REVIEW_1`，open 后 assignment 失败可续签。standalone `ACCEPTANCE` 入口保持不变。

生产 CLI 的零模型调度控制面是 `schedule-batch` → `schedule-result` → `schedule-receipt` → `schedule-final`。`schedule-result` 接收既有执行边界产出的 `ai-result-1`，由 controller 补齐并核对 `dispatch_id/task_id/step_id/attempt`，输出位置不能由调用方指定：controller 从已重放 dispatch 唯一确定 `<state_root>/<task_id>/scheduler-results/<dispatch_id>.json` 并原子冻结，再按该文件 bytes 生成 receipt。结果读取按已打开的目录 fd 定位，拒绝目录换绑、symlink、hardlink 和超限文件。`schedule-final` 先创建 child；同时提供已记录 runtime evidence 对应的 `--owner-receipt` 与 Sol-medium `--acceptor` 时签发 `REVIEW_1`。后续 repair ladder 到达 terminal 授权点后使用 `decide <child_id> authorize_final_xhigh`。

### Final acceptance 返工路径

```text
Sol medium REWORK
  → 人工批准冻结 findings / paths / commands
  → 不同身份的 Sol-medium fixer 有界返工
  → 另一不同身份的 Sol-medium read-only recheck
  → 再次 REWORK 才可 owner-authorize Sol-xhigh terminal repair
```

Sol-xhigh terminal repair 是一次性的例外，不自动启动，也不增加普通 Sol-xhigh construction 权限。

## 4. Codex Team 入口

工具名称是 `codex team`，消息 grammar 是：

```text
team call <objective>
team call: <objective>
team call：<objective>
```

入口只识别消息开头的固定指令，并把目标分成四种 disposition：

- `DIRECT_L0`：控制器执行固定 allowlist argv，不调用模型；
- `DIRECT_L1`：Luna 只读抽取一个安全的仓库相对文件；
- `PLAN_REQUIRED`：回到需要人工 owner gate 的完整规划流程；
- `BLOCKED`：输入、锁、权限或执行证据不满足要求。

Team Call 不授予 Luna review、approval、construction 或 final acceptance 权限，也不自动 merge、push。

## 5. 证据与状态

所有任务都围绕固定 `base_commit`、`candidate_commit` 和授权文件集合运行。关键证据包括：

- task / route / route-advice / plan / result 的严格 Schema；
- optimization 默认 shadow，由已验证 `[optimization]` 计算，与 routing mode 分开；建议只记 sidecar，不改 effective route/roles；enforced 需内部四门全过且推荐为成本降级，否则固定链回退；缺 miss 或缺省 period/origin 不能开门；
- compact prompt 是双钥匙 armed 字段投影：公开 builder 只读 pinned `[optimization]` 与 `aggregate_metrics(state_root)`，`compact_prompts=true` 且 `evaluate_optimization_gate==ALLOW_ENFORCED` 且 `mode=enforced` 且 compact bytes 小于 full 才生效；shadow、无 state_root 或缺/非法 metrics 回完整 prompt。只去掉可重建包装，不改角色、权限、worktree 或 acceptance；task_id、角色指令、objective、commits、scope、forbidden actions、commands、human gates、plan/step id、hashes、授权票与两条证据授权句存在则逐字保真。acceptance repair ladder 的 assignment prompt 明确不参与 compact，永远 full；
- runtime evidence（模型、推理档、执行面、sandbox、permission、cwd、native UUID）；
- cost evidence（实测、投影和 unavailable 明确区分）；
- router-probe manifest（Luna/Sol/Terra 热前缀臂、逐模型冷对照、固定 seed 与配对案例）；
- append-only events、human decisions 和 assignment capability；
- 真实 diff、工作树、Git 控制面和测试输出。

状态机遇到 HEAD 漂移、只读角色写入、范围越界、重复 attempt、证据缺失或非法跳转时停止并记录 `BLOCKED`，不依赖模型解释来“继续”。

只读 Team Call 的 Git 控制面快照比较持久状态：路径集合、文件 mode/size/hash
和引用内容。`git status` 可能用字节完全相同的新 index 原子替换旧 index，因此
单独的 inode/mtime/ctime 漂移不算持久修改；新增文件、内容变化、权限变化和
引用变化仍阻断。快照不能证明执行窗口内“修改后又恢复”的瞬态历史，第一安全
边界仍是已验证的 read-only sandbox 与 runtime permission。

### 常驻路由研究面

`ai_workflow_router_probe.py` 与生产状态机隔离：它不 import 任务存储，不写
events/task ledger，也不能调用 route application。常驻的最小定义是
“固定模型 + 冻结前缀 + 每次新会话”，避免线程污染。每个历史 intake 在 Luna、Sol、
Terra 的热前缀臂及对应冷对照中只执行入口分类；真实写任务不会重复执行。

runner 默认 `dry-run`，live 必须双重显式选择。批次先在输出根目录内写临时目录，
完成 manifest、cost evidence、summary 和 report 后再原子发布；同 batch id
write-once。分析只有在 measured、六臂完整、至少 32 个 paired cases、前缀稳定
且 token 完整、四个任务层各至少 8 个时才允许输出
`CACHE_MECHANISM_CANDIDATE_*`。它只比较热前缀的 uncached-input 机制，不冒充
真实成本赢家；缺少费率快照和下游反事实成本时 cost winner 明确 unavailable。
`effective_route=UNCHANGED`；缺证据返回 `OBSERVATION_ONLY`，无缓存收益返回
`KEEP_DETERMINISTIC_BASELINE`。

### 恢复与终止

`resume <task_id>` 只从持久化状态继续。施工首次到达 owner gate 前，控制器会冻结最小恢复上下文（plan、route request、step、attempt）；后续恢复重新校验这些 artifact，并依赖已有 dispatch 记录防止重复派发。`decide ... --resume` 先完整预检恢复参数，再写入 owner decision；live 恢复不会继承上次授权。

`abort <task_id>` 是 owner 决策，可从 `TRANSITIONS` 中的非终态进入 `ABORTED`（这些状态都有 `ABORTED` 出边）。`BLOCKED`、`CLOSED` 与 `ABORTED` 是终态、无出边，不能再 abort。它只追加决策和状态事件，不删除 task、result、runtime evidence 或历史账本。`decide <task_id> authorize_final_xhigh` 只写入 whole-project xhigh 授权票，不改变 REMEDIATION 状态机，且拒绝 `--resume`。

## 6. 安全边界

- 默认不自动 merge、push、删除 worktree 或修改全局配置；
- 写入必须在具名、隔离的 worktree 和冻结路径内进行；
- 不把项目密钥传给子进程，日志不记录环境变量和完整原始数据；
- 任务范围、运行时身份和证据不一致时立即停止，不依赖模型自行解释。

## 7. 代码地图

```text
config/                         # 任务、路由、计划、结果、运行时、成本与 route-advice Schema
scripts/ai_workflow.py          # 主 CLI、任务状态机、Team Call 生产入口
scripts/ai_workflow_runtime.py  # 原生/exec 运行时身份与证据
scripts/ai_workflow_artifacts.py# 严格 artifact 校验和数据类
scripts/ai_workflow_routing.py  # Terra OS 闭集路由与 shadow/enforced advice wrapper
scripts/ai_workflow_planning.py # 计划和施工信封
scripts/ai_workflow_scheduler.py# 计划调度与 final ACCEPTANCE child
scripts/ai_workflow_repairs.py  # acceptance repair ledger v2
scripts/ai_workflow_team_call.py# Codex Team grammar、分类和收据
scripts/ai_workflow_router_probe.py # 常驻路由器离线 shadow 探针、聚合和报告
scripts/sync_plugin.py           # 固定 manifest 的 Plugin 检查/原子同步
scripts/verify_all.sh            # 零模型完整验证入口
plugins/ai-workflow/             # 对外 Plugin；runtime/config 必须与根目录一致
tests/                           # 默认假 runner、负向注入和发布一致性测试
```

## 8. 当前限制

- 项目是 public preview，重点是自用和实验，不提供生产 SLA；
- 真实计费数据、模型服务可用性和 live rollout 仍必须由实际环境单独验证；
- Windows 原生生命周期不在当前验证范围内。
