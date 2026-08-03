# GPT 多模型协作工作流

> 版本：v0.1  
> 状态：`DRAFT_FOR_OWNER_REVIEW`  
> 日期：2026-08-03  
> 性质：全局、跨项目的实验性 AI 工作流；不属于任何一个业务仓库的宪法或任务卡。

## 1. 目标

本项目要验证：在 ChatGPT Plus 的额度约束下，通过四类固定角色分工，是否能在不降低高风险工程质量的前提下，减少 Sol 的无差别参与、重复验证和返工。

固定选角：

| 角色 | 模型与推理档 | 主要职责 |
|---|---|---|
| Luna | `gpt-5.6-luna` / `max` | 取证、机械核对、冻结规格下的窄域工作 |
| Terra | `gpt-5.6-terra` / `xhigh` | 主力工程施工、调试和集成 |
| Sol medium | `gpt-5.6-sol` / `medium` | 方案定型、语义裁决和高风险验收 |
| Sol xhigh | `gpt-5.6-sol` / `xhigh` | 重大争议、不可逆风险和重复语义失败的终审 |

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

## 3. Luna 模型、子代理与 `luna_worker`

三个概念必须分开：

- `gpt-5.6-luna` 是模型；
- 子代理是由父会话派生的运行形态；
- `luna_worker` 是固定 Luna Max 及有界规则的自定义角色。

当前 Codex 本地客户端支持在 `~/.codex/agents/*.toml` 定义自定义子代理，并在文件中固定 `model` 与 `model_reasoning_effort`。交互式父会话可按名称生成 `luna_worker`。

`codex exec` 当前没有 `--agent luna_worker` 接口。因此：

- 交互式工作：直接生成 `luna_worker` 子代理；
- 自动编排：以 `codex exec -m gpt-5.6-luna` 启动独立 Luna Max 会话，并注入项目角色契约。

`luna_worker` 予以保留，但只是交互式轻量适配器，不得成为第二套项目规则。

## 4. 自动化边界

采用半自动模式：

- 自动：任务信封校验、确定性路由、模型调用、证据收集、确定性门禁、指标记录、恢复与中止；
- 人工：方案批准、整改授权、Sol xhigh 调用、最终验收、宪法变更、合并与推送。

编排器不使用模型解释路由规则。它只读取任务类型、风险标记和闭集状态，然后执行预先登记的流程。

## 5. 三条主流程

### 5.1 方案规划

```text
用户目标
→ 任务信封校验
→ Luna Max 只读事实盘点（L1）
→ 证据定位机械校验
→ Sol medium Planner 成案
→ 方案一致性机械检查
→ AWAITING_OWNER_DECISION
```

### 5.2 工程验收

```text
固定 base/candidate commit
→ 工作树与禁改面检查
→ Luna Max 卡面预审（L2）
→ 目标测试和必要变异
→ Sol medium Reviewer 语义验收
→ AWAITING_OWNER_DECISION
```

### 5.3 验收后整改

```text
已接受的阻断项
→ Sol medium 生成最小整改契约
→ 人工批准范围
→ 创建独立 branch/worktree
→ Terra xhigh 施工
→ Luna Max 仅针对原阻断项反证（L2）
→ Sol medium 复验
→ AWAITING_OWNER_DECISION
```

对宪法、PIT、幸存者偏差、公开 schema、append-only、安全、数据污染及跨卡契约类整改，Sol medium 必须先冻结规格。

## 6. Luna 最小证据与反证包

原“可低成本复核的对抗式独立质量证明”不再对所有 Luna 任务统一强制。替换为三档证据要求：

| 档位 | 任务 | 要求 |
|---|---|---|
| L0 | 命令执行、哈希、文件数、固定字符串搜索 | 无叙事性自检；由编排器保存命令、退出码和产物 |
| L1 | 事实盘点、文档对照、证据抽取 | 最多 5 条关键主张，每条最小证据，最关键结论 1 次交叉检查，列盲区 |
| L2 | 窄域实现、验收预审、原阻断项复验 | 目标测试，1 个有效负向样例或变异，diff 范围核对，盲区 |

L3 不是 Luna 的更高自检档。开放式、高风险语义任务直接转 Sol medium。

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

- 默认只读；
- 只处理点名目标和文件；
- 不重新定义验收标准；
- 不把相邻发现并入施工；
- 只有任务信封显式列出可写路径时才能修改；
- 交付 L0/L1/L2 对应的最小证据包。

### 7.2 Terra xhigh

- 只在独立 worktree 写入；
- 是整改任务的唯一施工所有者；
- 不合并、不推送、不自验；
- 遇到两种合理规格解释时返回 `NEEDS_CLARIFICATION`；
- 完成目标测试及至少一个负向检验；
- 交付固定 candidate commit。

### 7.3 Sol medium

Planner 和 Reviewer 是互斥模式。

Planner 输出事实基线、目标、边界、冻结不变量、实施段、验收标准和未决裁定。Reviewer 只对固定 base/candidate commit、任务契约、Luna 证据包及自动门禁结果作语义审查。

Sol 只能推荐通过、附注通过、返工、拒绝或升级；不得自动改变人工决策状态。

### 7.4 Sol xhigh

只接收包含唯一争议命题、闭集选项、各方证据及不可逆后果的最小案卷。永不自动启动。

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

存在以下任一标记的整改，必须由 Sol medium 先冻结规格：

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

Sol xhigh 只可在下列条件下被建议：

- 权威规则实质冲突；
- Sol medium 无法在闭集选项中稳定裁定；
- 同类语义缺陷返工后仍未关闭；
- 污染、PIT 或安全错误可能造成不可逆后果；
- 重大设计存在多个不可兼容方案。

编排器只能进入 `ESCALATION_PROPOSED`，项目所有者批准后才能调用 Sol xhigh。

## 10. 重试与升级

每项任务最多：

```text
1 次技术重试
1 次同角色实现返工
1 次跨模型升级
```

达到上限后强制 `BLOCKED`。

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

## 13. 最小实现

第一版只创建：

```text
config/
├── ai_workflow.toml
├── ai_workflow_task.schema.json
└── ai_workflow_result.schema.json

scripts/
└── ai_workflow.py

tests/
└── test_ai_workflow.py

data/state/ai-workflow/   # gitignore，运行时生成
```

不增加第三方依赖。使用标准库处理 TOML、JSON、子进程、文件锁和哈希。

CLI 最小命令：

```text
new
validate
run
status
decide
resume
abort
report
```

`run` 遇到人工闸门立即以可识别退出码停止，不占用进程等待。

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

## 17. 实施分段

### 阶段 1：Luna 有界实现

交互式生成现有 `luna_worker`，只负责：

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

## 18. 本规格的完成条件

本文经所有者复核批准后：

1. 本文顶部的文档状态改为 `APPROVED_FOR_IMPLEMENTATION`（该状态不属于第 11 节的运行任务状态机）；
2. 制定详细实施计划；
3. 调用现有 `luna_worker` 施工阶段 1；
4. 主控独立复核后才能进入 Terra 工程接入。

本文未获批前，不创建任何运行代码，不修改现有全局 Agent 配置。
