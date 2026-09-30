# Codex Team 架构说明

Codex Team 是一个本地、可恢复、可审计的半自动编排层。它把“谁可以做什么”固化成配置、Schema、运行时证据和人工闸门，而不是让模型自由解释路由规则。

## 1. 执行面

| 执行面 | 默认用途 | 身份约束 | 权限边界 |
|---|---|---|---|
| `NATIVE_SUBAGENT` | Luna Max 默认路径 | `role=luna`、`gpt-6-luna/max`、`agent_type=null`、native agent/thread UUID | 由冻结 envelope 决定；通常只读或有界写 |
| `CODEX_EXEC_ROLE_CONTRACT` | 明确授权的独立 `codex exec` 会话 | 独立记录，不能冒充原生子代理 | 由任务信封和 assignment capability 决定 |

原生身份必须由控制器签发并由运行时证据闭环证明。模型、推理档、执行面、权限、沙箱、cwd 或 UUID 缺失/冲突时，流程 fail-closed；调用者自报不能补齐证据。

## 2. 默认角色

| 角色 | 默认模型 / 档位 | 主要职责 | 明确边界 |
|---|---|---|---|
| Luna Max | `gpt-6-luna / max` | 同类合并、范围明确、机械可验证的小任务，以及检查和证据整理 | 失败携带证据交回主施工角色；不反复试错、不规划、不验收 |
| 主施工角色 | `gpt-6.1-sol / medium` | 日常统筹、普通项目规划、复杂施工、调试、集成和首轮有界返工 | 不 merge、push 或自我验收 |
| 集中验收与困难升级 | `gpt-6.1-sol / high` | 全部小节完成后的独立、只读、对抗式终验；困难问题与授权终局修复 | 按需调用；只读终验与授权修复分属不同阶段 |
| Astra medium | `gpt-6-astra / medium` | 大型规划、顽固难题、审查分歧的补充独立复验，或授权终局修复 | 非普通项目必经步骤；复验只读，终局修复须 owner 授权 |

普通计划不固定转交 Astra。首轮返工由 Sol medium 优先承担，再由另一 Sol medium 独立复验；需要补充视角时显式选择 Astra medium。复验仍失败，只允许经既有 owner 授权执行一次 Sol high 或 Astra medium 终局修复，随后必须通过冻结功能检查和既有交付门。

内部历史 role/command ID 仅用于兼容：主施工仍使用 `terra_xhigh`，集中 Sol high 终验使用 `sol_reviewer`，Sol medium 返工/复验使用 `sol_medium_reviewer`，Astra medium 复验使用 `astra_medium_reviewer`；`sol_xhigh` 与 `authorize_final_xhigh` 是 Astra/终局授权的兼容句柄。实际身份以配置、模型、档位和运行时收据为准。


## 3. 生命周期

普通项目：确定性分类 → Sol medium 补齐必要计划 → 冻结信封与 owner gate → Luna Max 有界施工或 Sol medium 主施工 → 小节自检 → 全部完成后独立 Sol high 集中对抗式终验。

默认同时最多 2 个子代理，最多 1 个写入者、2 个只读者；调度器的批次与单步入口都核对容量。Team Call 保持单 active receipt。同类 Luna 小任务合并；失败交回证据，不反复派同一失败工作。

集中终验失败 → 冻结 findings / paths / commands → 不同实例的 Sol medium 一次有界修复 → 另一 Sol medium 独立复验，或显式选用 Astra medium → 仍失败才可 owner-authorize Sol high / Astra medium 一次终局修复。终局修复后控制器执行冻结功能检查；无额外 task-level model review，现有新鲜 verdict、权限、交付门仍须满足，不能自动声称验收通过。

通用 `ACCEPTANCE` 的本地审查不等于全工程终验。正常调度在全部 receipt 完成后创建唯一 whole-project `ACCEPTANCE` child；final candidate 必须是 clean HEAD、FrozenPlan candidate 的后代，并且 diff 落在授权 write union。parent 的 `acceptance_task_sha256` 和 child 的 `scheduler-parent.json` 绑定唯一 parent、plan、event 与 candidate。

零模型 CLI 链为 `schedule-batch` → `schedule-result` → `schedule-receipt` → `schedule-final`。结果身份、哈希、symlink/hardlink 拒绝和原子冻结规则保留。`schedule-final` 提供已验证 `--owner-receipt` 与 `sol_reviewer` 的 `--acceptor` 时，只签发一次 Sol high `REVIEW_1`；接口不启动模型、不 merge/push。终局使用兼容命令 `decide <child_id> authorize_final_xhigh`，仅一次，且不能与 `--resume` 合用。

观察后续 10–20 个真实任务的总消耗、耗时、首次通过率、返工和遗漏；沿用既有 cost/report，不新增评测框架。API 成本和订阅额度独立记录，成本收益待实测。

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
`KEEP_DETERMINISTIC_BASELINE`。项目说明建议常驻入口分类优先用 Luna max；
这只是文档建议，生产生效路由仍由确定性规则决定，live 探针与模型选择由使用者显式指定。
探针脚本是根目录的研究工具，刻意不进入 Plugin runtime 分发；Plugin 仅镜像生产运行时
以及探针所需的配置和 Schema。

### 恢复与终止

`resume <task_id>` 只从持久化状态继续。施工首次到达 owner gate 前，控制器会冻结最小恢复上下文（plan、route request、step、attempt）；后续恢复重新校验这些 artifact，并依赖已有 dispatch 记录防止重复派发。`decide ... --resume` 先完整预检恢复参数，再写入 owner decision；live 恢复不会继承上次授权。

`abort <task_id>` 是 owner 决策，可从 `TRANSITIONS` 中的非终态进入 `ABORTED`（这些状态都有 `ABORTED` 出边）。`BLOCKED`、`CLOSED` 与 `ABORTED` 是终态、无出边，不能再 abort。它只追加决策和状态事件，不删除 task、result、runtime evidence 或历史账本。`decide <task_id> authorize_final_xhigh` 是保留的兼容命令 ID，只写入 whole-project owner 授权票，由授权的 Sol high 或 Astra medium 执行一次终局修复，不改变 REMEDIATION 状态机，且拒绝 `--resume`。

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
scripts/ai_workflow_routing.py  # 主施工角色 闭集路由与 shadow/enforced advice wrapper
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
