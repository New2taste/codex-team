# GPT 多模型协作工作流

> 一个面向 Codex 的、可审计的半自动多模型编排器：用确定性任务信封、运行时证据和人工闸门，把规划、施工、验收与整改串成可恢复的工作流。

| 项目状态 | 当前值 |
|---|---|
| Plugin 版本 | `0.2.0` |
| 发布形态 | Public preview；自用优先，不承诺生产 SLA |
| 默认 Luna 执行面 | 原生 `NATIVE_SUBAGENT`：`gpt-5.6-luna / max` |
| 默认施工 OS | Terra xhigh；Sol medium 负责最终集中验收 |
| 最近更新 | 2026-08-24 |
| 许可证 | 尚未声明；公开可见不等于授予再分发许可 |

## 项目简介

这个仓库不是一个常驻服务，也不是替用户自动决策的“AI 项目经理”。它提供一组标准库实现、JSON Schema、Plugin 镜像、CLI 和测试，用于在额度受限的环境中安全编排多模型协作。

核心原则是：低风险、有界的机械工作优先交给廉价的原生 Luna Max；复杂施工由 Terra xhigh 负责；所有工程小节完成后，由只读的 Sol medium 统一做最终、对抗式的整体验收。验收失败时，返工优先交给不同身份的 Sol medium；仍失败才进入 owner-authorized 的 Sol xhigh 终局升级。

这个项目适合：

- 希望把多模型协作规则固定成可验证契约的个人开发者或小团队；
- 需要保留任务、运行时身份、成本和验收证据的 Codex 工作流；
- 想先在假 runner 和历史任务上校准，再逐步启用真实模型调用的实验。

它不适合：

- 需要无人值守地修改、合并或推送生产仓库的流水线；
- 需要跨项目并发写入、常驻队列、数据库或 Web 控制台的系统；
- 尚未接受人工 owner gate 和最终验收约束的自动化场景。

### 快速开始

要求：Python 3.11+、Git、POSIX shell；Plugin 的运行时验证还需要 `jq`。项目不增加第三方 Python 依赖。

```sh
git clone https://github.com/New2taste/New2taste.git codex-team
cd codex-team

# 运行完整测试
python3.11 -m unittest discover -s tests

# 验证 Plugin、根目录与 Plugin 镜像的一致性
sh plugins/ai-workflow/scripts/verify.sh
```

可选的 Skill 检查（需要本机已安装 Codex skill-creator）：

```sh
python3 "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" \
  plugins/ai-workflow/skills/orchestration
```

### 最简单的调用：Codex Team

Codex Team 是工具名称；`team call` 是它接受的最简单消息指令。它不会把任意文本解释成 shell 命令：

```sh
codex team "team call 检查当前工作区状态"

codex team "team call 核对文件 README.md"
```

仓库内的 `team-call` CLI 是等价的本地测试入口：

```sh
python3 scripts/ai_workflow.py team-call \
  "team call 检查当前工作区状态" \
  --repository-root "$PWD"

python3 scripts/ai_workflow.py team-call \
  "team call 核对文件 README.md" \
  --repository-root "$PWD"
```

只有开头为 `team call` 的三种固定形式会被解析；状态默认写到仓库外的用户级 state 目录。`DIRECT_L0` 使用控制器固定命令，`DIRECT_L1` 只读抽取文件证据，其余安全目标回到需要人工闸门的规划流程。失败收据以退出码 `2` 返回，并保持 append-only 账本。

### 文档导航

- [架构与角色边界](docs/ARCHITECTURE.md)：执行面、状态机、证据链和安全边界；
- [开发与验证指南](CONTRIBUTING.md)：如何修改、测试、同步 Plugin 镜像；
- [变更记录](CHANGELOG.md)：公开版本的能力与限制；
- [Native Luna Max 设计](docs/superpowers/specs/2026-08-17-native-luna-max-design.md)：原生身份和迁移边界；
- [Codex Team 设计](docs/superpowers/specs/2026-08-13-team-call-natural-language-design.md)：入口 grammar、路由和收据契约。

## 1. 目标

本项目要验证：在 ChatGPT Plus 的额度约束下，通过四类固定角色分工，是否能在不降低高风险工程质量的前提下，减少 Sol 的无差别参与、重复验证和返工。

固定选角：

| 角色 | 模型与推理档 | 主要职责 |
|---|---|---|
| Luna Max | `gpt-5.6-luna` / `max` | 精确冻结 envelope 内的中初级/机械 coding、取证和分发 |
| Terra xhigh | `gpt-5.6-terra` / `xhigh` | 复杂施工、调试和集成；施工者完成冻结信封内的自检 |
| Sol medium | `gpt-5.6-sol` / `medium` | 一次集中最终验收；失败时优先做有界 Sol-medium 返工与复验 |
| Sol xhigh | `gpt-5.6-sol` / `xhigh` | owner-authorized 规划与终局升级 |

`Terra medium` 与 `Sol high` 没有默认角色（no default role），不得被隐式替换或注入流程。

任务量比例只可用于容量预估，不得成为路由的软硬约束。

## 2. 不是什么

第一版不建设通用 AI 项目经理，不实现：

- 全无人值守；
- 自动批准方案、宪法变更或最终验收；
- 自动合并、推送或删除 worktree；
- 基于工作量比例的强制模型分配；
- 机器学习路由、Web 控制台、常驻服务或数据库；
- 并行写入同一 worktree；
- 多层嵌套子代理。

它是一个可关闭、可恢复、可审计的“方案规划—工程验收—验收后整改”半自动编排器。

## 3. Luna 模型、原生子代理与 Luna Max

三个概念必须分开：

- `gpt-5.6-luna` 是模型；
- 子代理是由父会话派生的运行形态；
- `luna` 是工作流角色；Luna Max 的默认执行面是主控直接派发的原生子代理。

原生 Luna Max 的身份由受控调度参数与运行证据共同证明：
`execution_surface=NATIVE_SUBAGENT`、`model=gpt-5.6-luna`、
`reasoning_effort=max`、原生线程/代理 UUID、sandbox、permission 和 cwd。
缺失或冲突时必须 fail-closed；不能用自定义 Agent 名称补齐身份。

自动化独立会话仍可使用 `codex exec -m gpt-5.6-luna`，但它属于
`CODEX_EXEC_ROLE_CONTRACT`，必须单独记录，不能冒充原生子代理。因此：

- 默认工作流：由主控直接派发原生 `luna` 子代理，并验证模型与档位；
- 独立自动化：以 `codex exec -m gpt-5.6-luna` 启动独立会话，并注入项目角色契约。

当前开发 harness 可能显示内置 native subagent 标签；该平台标签不属于本仓库的
自定义 Agent 选项，也不改变 workflow role `luna`。

### 一次性清理迁移（cleanup-only migration）

旧版用户目录若包含 `luna_worker`（一次性 migration/迁移输入）或旧的 `luna-max.toml`，兼容的 install/uninstall 入口都只执行 cleanup：只能在验证
模板、状态和备份完整后执行一次性清理；迁移失败必须保留原有用户文件。该工具不参与
默认路由，不内嵌或发布完整 Agent TOML，也不会在缺失目录或空目录中创建或删除用户文件。

默认工作流不再依赖仓库或 Plugin 中的自定义 Agent 模板。

## 4. 自动化边界

采用半自动模式：

- 自动：任务信封校验、确定性路由、模型调用、证据收集、确定性门禁、指标记录、恢复与中止；
- 人工：方案批准、整改授权、Sol xhigh 调用、最终验收、宪法变更、合并与推送。

编排器不使用模型解释路由规则。它只读取任务类型、风险标记和闭集状态，然后执行预先登记的流程。

### Codex Team 自然语言入口（受限直达）

Codex Team 只接受位于消息开头、大小写不敏感的下列 grammar；`<objective>`
会压缩空白，但不会把任意自然语言解释为 shell 命令：

```text
team call <objective>
team call: <objective>
team call：<objective>
```

该入口默认 **single active worker**：全局收据锁尚有未终态请求时，下一次
调用会被阻断；它不承诺并行 agents，也不创建并行写入 worktree。路由只会得到
以下 disposition：

| Disposition | 受限行为 | 角色与边界 |
|---|---|---|
| `DIRECT_L0` | 仅精确 allowlist 中的只读目标可直达。 | **L0 controller/no model**：控制器执行固定 argv，不调用模型。 |
| `DIRECT_L1` | 仅 `核对文件 <repo-relative-path>` 可直达，并且路径必须安全、仓库相对。 | **L1 Luna read-only**：Luna 只读抽取固定证据，仍交付既有 L0/L1/L2 最小证据，不获得写权限。 |
| `PLAN_REQUIRED` | 任何其他安全目标都停止直达路径。 | **plan fallback**：回到既有、需要 human owner gates 的规划和任务信封流程；不自动启动 Sol xhigh。 |
| `BLOCKED` | 输入无效、已有 active receipt、缺失授权或执行失败。 | 保留阻断收据，不产生模型、任务、合并或推送承诺。 |

Codex Team 不授予 Luna review、approval 或 final acceptance 权限，也不改变现有
L0/L1/owner-gate 语言与角色边界；它不自动合并、不自动推送，且不替代人工批准或
最终整体验收。

CLI 的 `team-call` 在未显式传入 `--root` 时，会把按仓库路径摘要隔离的状态写到
仓库外的 `$XDG_STATE_HOME/ai-workflow/team-call/`（未设置时使用用户级 state
目录），因此不会先把待核对仓库变脏；显式 `--root` 仍按调用者给定的位置使用。
已终态失败的相同调用会重放为 `BLOCKED` 收据，CLI 返回退出码 2，不会把旧失败
误报为成功。显式选择 `--runner live --allow-live-model` 的合格 L1 会交给 Luna
Max；控制器只交付与已存 task 摘要、`luna` 角色、执行面和证据摘要绑定的只读
快照，并继续执行仓库、Git 控制面和零写入校验。live 路径仍要求可验证的
`--runtime-sessions-dir`，且不会因此获得 review、approval 或施工权限。

## 5. 三条主流程

### 5.1 方案规划

```text
用户目标
→ 任务信封校验
→ Luna Max 只读事实盘点（L1）
→ 证据定位机械校验
→ Terra xhigh Planner（只读）成案
→ 方案一致性机械检查
→ AWAITING_OWNER_DECISION
```

### 5.2 工程验收

```text
各工程小节：冻结信封内施工、自检、目标测试、负向检查、范围核对与运行时证据门
→ section_self_check_only（intermediate engineering sections 不派发独立对抗审查）
→ 全部小节完成并固定 base/candidate commit
→ 工作树与禁改面检查
→ Luna Max 有界 L2 证据抽取（若信封明确授权）
→ Sol-medium final acceptance（集中、只读、对抗式）
→ AWAITING_OWNER_DECISION
```

### 5.3 验收后整改

```text
Sol-medium final acceptance 的冻结 REWORK findings
→ 人工批准范围与一次性 assignment-scoped write 授权
→ different Sol-medium fixer 在原 candidate、允许路径和验证命令内修复
→ different Sol-medium recheck（与验收者、fixer 均不同身份；只读）
→ 若仍为 REWORK，才可 owner-authorized Sol-xhigh terminal repair
→ terminal repair 无 task-level review
→ AWAITING_OWNER_DECISION
```

中间工程小节取消独立对抗审查，不等于跳过验证：施工 owner 仍须完成信封内的
测试、负向检查、范围核对和运行时证据门。对宪法、PIT、幸存者偏差、公开 schema、
append-only、安全、数据污染及跨卡契约类整改，复杂语义由 Terra xhigh 停止并回交；
Sol xhigh 只处理 owner-authorized 的规划或终局升级。

## 6. Luna 最小证据与反证包

原“可低成本复核的对抗式独立质量证明”不再对所有 Luna 任务统一强制。替换为三档证据要求：

| 档位 | 任务 | 要求 |
|---|---|---|
| L0 | 命令执行、哈希、文件数、固定字符串搜索 | 无叙事性自检；由编排器保存命令、退出码和产物 |
| L1 | 事实盘点、文档对照、证据抽取 | 最多 5 条关键主张，每条最小证据，最关键结论 1 次交叉检查，列盲区 |
| L2 | 有界证据抽取、原阻断项负向验证 | 目标测试，1 个有效负向样例或变异，diff 范围核对，盲区 |

L3 不是 Luna 的更高自检档。复杂或高风险语义任务默认转 Terra xhigh；只有
owner-authorized Sol xhigh 规划，或最终 Sol-medium 验收的冻结返工梯级，才可改变角色路径。

Luna 只能返回：

```text
SUPPORTED
PARTIALLY_SUPPORTED
NOT_SUPPORTED
BLOCKED
```

不得返回 `ACCEPTED`、`REJECTED`、`MERGED` 或 `EFFECTIVE`。

## 7. 角色契约

### 7.1 Luna Max

- 只在 exact frozen envelope 中获得写权限；
- 只处理点名目标和文件，范围限于中初级/机械 coding、确定性验证和分发同步；
- 不重新定义验收标准；
- 不把相邻发现并入施工；
- 不承担 planning、review、semantic arbitration 或 final acceptance；
- 只有任务信封显式列出可写路径时才能修改；
- 交付 L0/L1/L2 对应的最小证据包。

Luna must never review, approve, or perform final acceptance. 任何需要开放式判断、复杂施工或高风险语义的任务都必须转 Terra xhigh 或 owner-authorized Sol xhigh。

### 7.2 Terra xhigh

- 只在独立 worktree 写入，负责 complex construction、调试和集成；
- 是复杂施工和整改任务的唯一施工所有者；
- 每个工程小节只执行冻结信封内的自检；不得把自检称为独立验收；
- 不合并、不推送、不自验；
- 遇到两种合理规格解释时返回 `NEEDS_CLARIFICATION`；
- 完成目标测试及至少一个负向检验；
- 交付固定 candidate commit。

### 7.3 Sol medium

Sol medium 的 final acceptance 始终只读，针对固定 base/candidate commit、任务契约、
证据包和自动门禁结果作集中对抗式语义审查。它不得承担普通 planning 或常驻
construction。

若 final acceptance 为 `REWORK`，控制器只能在人工批准后向 different Sol-medium
fixer 发出一次 assignment-scoped write 授权；其范围限于冻结 findings、candidate、
允许路径和验证命令。原验收者不得自修。随后 different Sol-medium recheck 必须与
验收者和 fixer 均为不同 runtime identity，且保持只读；仍为 `REWORK` 才可升级。
Sol medium 不能自动改变人工决策状态。

### 7.4 Sol xhigh

Sol xhigh 只接收 owner-authorized 的最小案卷，用于规划（planning），或在 different Sol-medium recheck 对最终验收返工给出 `REWORK` 后执行 terminal repair；不得自动启动或承担普通、常驻 construction，不得跳过 Sol-medium final acceptance。

只有上述 terminal repair 构成施工例外：它必须是 owner-authorized、assignment-scoped、一次性的 terminal repair。该 terminal repair 无 task-level review；不得据此泛化为普通 Sol-xhigh construction。

### 7.5 Task 4 冻结分配（Frozen role/lifecycle contract）

这份分配是分发和生命周期的唯一公开契约：

- Luna Max 仅在 exact frozen envelope 内做 bounded mechanical coding、确定性 verification 和 root→Plugin distribution；信封必须列出精确路径、命令、负向检查和交付物。
- Terra xhigh 负责复杂施工；每个工程小节完成冻结信封内自检后直接推进下一小节。
- Sol medium 负责 final whole-project acceptance；若为 `REWORK`，另一名 Sol-medium
  先做一次有界修复，再由第三名 Sol-medium 复验。
- Sol xhigh 负责 owner-authorized planning 和 terminal escalation；不得自动调用。
- Terra medium、Sol high 没有默认角色（no default role）。任何调用都必须先获得新的 owner-authorized、闭集信封；不得作为普通流程的隐式角色。

## 8. 任务信封

每项任务必须通过 JSON Schema，至少包含：

```text
schema_version
task_id
task_type: PLAN | ACCEPTANCE | REMEDIATION
objective
repository_root
source_worktree
base_commit
candidate_commit
authoritative_files
allowed_write_paths
forbidden_actions
risk_flags
acceptance_commands
verification_level
human_gates
```

不完整的任务不启动模型，不允许模型自行补义。

## 9. 风险路由

存在以下任一标记的整改，默认转 Terra xhigh 负责复杂施工、验证和独立对抗审查；复杂语义有歧义时必须停止并回交，不得直接注入普通 Sol medium：

```text
CONSTITUTION
PIT
SURVIVORSHIP_BIAS
PUBLIC_SCHEMA
APPEND_ONLY
SECURITY
DATA_CONTAMINATION
PUBLIC_API
CROSS_CARD_CONTRACT
```

Sol xhigh 只可在 owner-authorized 规划或终局升级案卷中被建议：

- 权威规则实质冲突；
- 同类语义缺陷返工后仍未关闭；
- 污染、PIT 或安全错误可能造成不可逆后果；
- 重大设计存在多个不可兼容方案。

Sol medium 不负责普通规划或开放式闭集裁定；它只在集中最终验收为 `REWORK` 后，按
冻结 findings 接收一次有界 fixer 授权，并由另一名 Sol-medium 复验。

编排器只能进入 `ESCALATION_PROPOSED`，项目所有者批准后才能调用 Sol xhigh。

## 10. 重试与升级

每个全局工程闭环最多：

```text
每个 attempt 1 次技术重试
工程小节完成信封内自检后直接推进；无中间独立对抗审查
1 次只读 Sol-medium final acceptance
若 REWORK：1 次 different Sol-medium fixer 的 assignment-scoped repair
随后：1 次 different Sol-medium recheck
recheck 再为 REWORK：1 次 owner-authorized Sol-xhigh terminal repair；无 task-level review
```

任何身份、candidate、finding 集、允许路径、验证命令或 receipt 绑定不成立，或达到
上述终局上限，均强制 `BLOCKED`。

v2 ledger 的终局事件仍是 `TASK_TERMINAL`，并明确
`whole_project_acceptance_required=PENDING`；Task 5 才执行独立的最终整体验收，本 Task 不新增终局 review phase。

进程异常、JSON 不合法、工具超时和命令未启动属技术失败，可原角色重试一次。规格误解、语义不变量漏失或多义解释属语义失败，不得让低阶角色重复碰运气。

## 11. 状态机

```text
DRAFT
→ TASK_VALIDATED
→ EVIDENCE_RUNNING
→ EVIDENCE_READY
→ PLAN_OR_REVIEW_RUNNING
→ PLAN_READY | REVIEW_READY
→ AWAITING_OWNER_DECISION
├─ APPROVED_FOR_EXECUTION
├─ REWORK_AUTHORIZED
├─ ESCALATION_AUTHORIZED
├─ DEFERRED
└─ CLOSED
```

施工支路：

```text
APPROVED_FOR_EXECUTION
→ WORKTREE_READY
→ IMPLEMENTATION_RUNNING
→ IMPLEMENTED_CANDIDATE
→ PRECHECK_RUNNING
→ PRECHECK_READY
→ REVIEW_READY
→ AWAITING_OWNER_DECISION
```

异常状态：

```text
BLOCKED
NEEDS_REPLAN
ESCALATION_PROPOSED
ABORTED
```

只有人工决策才能进入 `APPROVED_FOR_EXECUTION`、`REWORK_AUTHORIZED`、`ESCALATION_AUTHORIZED` 和 `CLOSED`。

## 12. 安全边界

- 规划和验收角色默认只读；
- 验收对象必须是固定 candidate commit；
- 写任务必须使用具名独立 worktree；
- 一个 worktree 同时只有一个写入所有者；
- 启动后 HEAD 变化立即停止；
- 只读角色产生文件变化立即 `BLOCKED`；
- 模型声明的 `changed_files` 必须与真实 diff 一致；
- 不使用 `--dangerously-bypass-approvals-and-sandbox`；
- 不自动删除 worktree、合并、推送或修改宪法；
- 业务项目密钥不传给 AI 编排子进程；
- 日志不记录环境变量和完整原始数据；
- 每个任务用独占锁防止重复运行。

## 13. 实现与目录结构

当前实现使用标准库处理 TOML、JSON、子进程、文件锁和哈希，不增加第三方 Python 依赖：

```text
config/                         # 任务、路由、计划、结果、运行时、成本 Schema
scripts/ai_workflow.py          # 主 CLI、状态机和 Codex Team 生产入口
scripts/ai_workflow_runtime.py  # native/exec 身份与 runtime evidence
scripts/ai_workflow_artifacts.py# 严格 artifact 校验和数据类
scripts/ai_workflow_routing.py  # Terra OS 闭集路由
scripts/ai_workflow_planning.py # 计划、施工信封和确定性门禁
scripts/ai_workflow_repairs.py  # acceptance repair ledger v2
scripts/ai_workflow_team_call.py# Codex Team grammar、分类和收据
plugins/ai-workflow/             # 对外 Plugin；runtime/config 与根目录同步
tests/                           # fake runner、负向注入和发布一致性测试
data/state/ai-workflow/          # gitignore；运行时生成
```

CLI 命令：

```text
new
validate
team-call
run
route
status
decide
resume
abort
report
```

`run` 遇到人工闸门立即以可识别退出码停止，不占用进程等待。`team-call` 是仓库内的开发入口；面向用户的工具名称是 `codex team`。

## 14. 运行记录

每项任务的本地状态目录：

```text
data/state/ai-workflow/<task_id>/
├── task.json
├── input-manifest.json
├── events.jsonl
├── human-decisions.jsonl
├── metrics.json
├── luna-result.json
├── terra-result.json
├── sol-plan.json
├── sol-review.json
└── logs/
```

运行目录不入 Git。实验结束后只生成一份脱敏总结，不维护多份实时进度文件。

若 `codex exec --json` 提供可靠的使用量，原样记录；否则留空，不估算或伪造 token 数据。

## 15. 测试要求

至少覆盖：

1. 任务缺字段或非法枚举时拒绝；
2. 合法集合及其补集；
3. Luna 不能返回最终验收状态；
4. 未经批准不能施工或调用 Sol xhigh；
5. 只读角色产生改动时阻断；
6. candidate HEAD 漂移时停止；
7. Terra 越过允许路径时阻断；
8. 技术失败只重试一次；
9. 语义失败不得无限循环；
10. 人工决策只追加；
11. 状态机非法跳转拒绝；
12. 日志密钥扫描；
13. 模型输出 schema 校验；
14. 命令参数不得被 shell 注入；
15. 单元测试默认使用假 runner，不消耗真实额度。

## 16. 实验设计

### 16.1 校准期

重放 3 个已完成任务：

1. 一个方案规划；
2. 一个工程验收；
3. 一个整改证据复验。

校准期只读，不根据结果修改业务项目。

### 16.2 试验期

使用 8–12 个真实工作单元，覆盖方案、验收及整改闭环。不设定 Luna、Terra 或 Sol 工作量比例。

记录：

- 模型和推理档；
- 墙钟时间；
- 技术重试和语义返工；
- Luna 证据的独有发现及 Sol 采纳数；
- Terra 首次施工是否通过；
- 自检、Sol 复核及重做的耗时；
- 重复读取和重复全量测试次数；
- 高阶模型参与次数；
- 最终提交和人工裁定。

### 16.3 成功判据

必须同时看到：

- 每个有效闭环的 Sol 参与量下降；
- Luna 证据产生被 Sol 采纳的独有价值；
- Terra 施工后 Sol 不再大规模重做代码；
- 重复全量测试减少；
- 编排开销没有显著拉长总工期；
- P0/P1 漏检不增加；
- 完成至少一个规划—施工—验收—整改—复验闭环。

若某类 Luna 自检连续没有独有发现，Sol 仍需从头复核，或自检成本接近重做，则取消该类任务的 Luna 反证步骤。

### 16.4 停止线

以下任一发生即暂停实验：

- 自动化误改 main 或范围外文件；
- 未批准调用 Sol xhigh；
- 将模型建议自动写成最终验收；
- 自动流程跳过必要审查导致 P0 漏检；
- 同一任务超过循环上限；
- 运行记录无法对应到具体输入、模型、提交和结果。

## 17. 实施分段与启用梯级

以下是设计阶段保留的启用梯级。当前仓库已完成假 runner、历史只读、证据门和低风险有界路径；真实 live rollout 仍必须由具体环境单独授权和验证。

### 阶段 1：Luna 有界实现

原生 Luna Max 只负责：

- TOML 配置骨架；
- task/result JSON Schema；
- 状态闭集；
- CLI 骨架；
- 假 runner 单元测试夹具；
- L0/L1/L2 最小证据包。

不接入真实 `codex exec`，不创建 worktree，不实现合并。

### 阶段 2：主控独立复核

检查 schema 合法集及补集、状态机非法跳转、权限边界、测试自证和 Luna 越界。

### 阶段 3：Terra 工程接入

负责安全调用 `codex exec`、JSONL 解析、超时和重试、worktree 创建及 HEAD 锁定、日志脱敏和运行账本。

### 阶段 4：Sol medium 验收

对固定 candidate commit 进行权限、失败注入及跨组件契约验收，建议是否进入校准期。

### 阶段 5：逐级启用

```text
假 runner
→ 历史 Luna 只读
→ 历史 Luna + Sol
→ 低风险 Terra 写任务
→ 真实活卡
```

任一阶段未通过不得自动进入下一阶段。

## 18. 原始规格完成条件

本节保留原始规格的审批准入条件，便于追溯设计来源；当前公开实现已经过本仓库测试和最终 Sol-medium 集中验收，不代表自动获得业务项目的 owner approval。

本文经所有者复核批准后：

1. 本文顶部的文档状态改为 `APPROVED_FOR_IMPLEMENTATION`（该状态不属于第 11 节的运行任务状态机）；
2. 制定详细实施计划；
3. 调用原生 Luna Max 施工阶段 1；
4. 主控独立复核后才能进入 Terra 工程接入。

本文未获批前，不创建任何运行代码，不修改现有全局 Agent 配置。
