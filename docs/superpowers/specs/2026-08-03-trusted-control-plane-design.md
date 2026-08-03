# 可信控制平面优化设计

> 日期：2026-08-03
>
> 状态：`APPROVED_FOR_SPECIFICATION`
>
> 目标：在不削弱现有人工门、证据门和 Git 安全边界的前提下，通过最小委派、可证明调度、可归因成本证据和可复现角色安装降低模型消耗并缩短交付时间。

## 1. 背景与取舍

本设计参考两个已固定提交的开源项目：

- [`DannyMac180/sol-advisor@d1f390c`](https://github.com/DannyMac180/sol-advisor/tree/d1f390c29191b96df14174b996ae0439a73d3e6a)：借鉴运行时身份验真、fresh reviewer、无静默 fallback 和失败关闭；
- [`yehyakin/codex-sol-luna@d702975`](https://github.com/yehyakin/codex-sol-luna/tree/d70297500277752a8cc729512efca35aa13910e5)：借鉴 Direct／Sol-only／委派分流、`goal/done_when/tasks/stages`、单文件单 owner、容量批次和成本证据分级。

只借鉴经过本地只读审计的设计思想，不复制上游代码或固定节省数字。`sol-advisor` 的所有实现统一走 Terra/High，会增加低风险工作的成本；`codex-sol-luna` 的 59%/65% 是特定样本投影，不能成为本项目的路由阈值或效果承诺。

已比较三种方案：

1. 只增加路由和成本统计：改动小，但无法证明运行身份、并发安全或节省来源；
2. 建设可信控制平面：同时补齐确定性路由、结构化计划、调度、运行时验真和成本证据；
3. 优先插件化和跨平台安装：扩大发布面，但核心合同尚未稳定。

采用方案 2，并把 Luna 纳管与最小开源发布面作为可信控制平面的组成部分：本期提供项目级 Agent、Codex Plugin、companion installer 和调用文档；通用公共目录投稿、跨平台完整生命周期和 UI 延后到控制平面合同稳定之后。

## 2. 设计原则与边界

### 2.1 必须保持的不变量

- 现有 `ai-task-1`、`ai-result-1` 保持兼容，不原地增加自由字段；
- 现有状态机是唯一执行真相，计划 artifact 只能提供经过验证的调度输入；
- 人工继续控制方案批准、整改授权、Sol xhigh、最终验收、merge 和 push；
- 固定 candidate、HEAD 漂移检查、dirty tree 检查、changed-path 守卫和独立 worktree 不得放宽；
- L0/L1/L2 证据要求、append-only 事件与决策账本、重试上限继续有效；
- 仓库内规范模板是 `luna_worker` 的版本事实来源，个人全局文件只能是可校验的安装副本；
- 缺失或冲突的信息一律失败关闭，不允许模型、角色或权限静默替代；
- 不自动 merge、push、删除 worktree，不扩大 sandbox 或外部副作用权限。

### 2.2 本版不做

- 机器学习路由、自由文本复杂度评分或按工作量比例强制分配；
- 共享 worktree 的并行写入、固定 worker 数或嵌套子代理；
- 通用公共 Plugins Directory 投稿、Windows 生命周期脚本、Web UI 或常驻服务；
- 用估算 token 冒充实测数据，或宣称固定节省比例；
- 由 `direct` 路径绕过风险策略、用户授权或现有安全检查。

## 3. 总体架构

```text
ai-task-1
  + ai-route-request-1
        |
        v
确定性路由器 ----> ai-route-decision-1
  | direct             | sol_only          | delegated          | blocked
  |                     |                   |                    |
  | 返回 Host 处理      | 进入只读 Sol 流程  | 校验 ai-plan-1      | 记录原因并停止
  | 不声明任务完成      | 不启动 worker      | stage/owner 调度    |
  |                     |                   v                    |
  |                     |           runtime-evidence-1          |
  |                     |                   |                    |
  +---------------------+-------------------+--------------------+
                                            v
                                  现有状态机、人工门、证据门
                                            |
                                            v
                                      cost-evidence-1
```

路由控制平面位于现有执行状态机之前。它决定任务是否值得进入编排，但不替代任务完成状态：

- `direct` 只返回 `DIRECT_HANDOFF`，表示不值得支付委派开销，由 Host 在原用户权限内处理；编排器不启动任何角色，也不声称完成；
- `sol_only` 只允许 Sol 规划、裁决或复审，不启动 Luna/Terra worker；
- `delegated` 必须有有效计划 artifact，并通过 owner、依赖、容量和运行时身份检查；
- `blocked` 不启动任何模型，记录闭集原因和修复提示。

## 4. 确定性入口路由

### 4.1 `ai-route-request-1`

路由请求是独立、严格、版本化 artifact，至少包含：

```text
schema_version: ai-route-request-1
task_id
work_class: SIMPLE | PLANNING_ONLY | BOUNDED | MULTI_STAGE | HIGH_CONSEQUENCE
execution_need: NONE | READ_ONLY | WRITE
decomposable: boolean
risk_flags
reason_codes
```

`risk_flags` 必须与对应 `ai-task-1` 完全一致。`reason_codes` 使用闭集值，不能用自由叙事替代路由输入。旧任务没有路由请求时按 `LEGACY` 运行，保证迁移期行为不变。

### 4.2 路由优先级

规则按以下顺序执行，前项优先于后项：

1. 请求缺失必填字段、与任务信封冲突或包含未知枚举：`blocked`；
2. 有风险标记或 `HIGH_CONSEQUENCE`：不得进入 `direct`；不需要施工时至少进入 `sol_only`，需要施工且边界完整时进入 `delegated`，边界不完整时进入 `blocked`；存在不可消解歧义时进入 `blocked`；
3. `PLANNING_ONLY + NONE/READ_ONLY`：`sol_only`；
4. `SIMPLE`、无风险且不需要跨文件协调：`direct`；
5. `BOUNDED` 或 `MULTI_STAGE` 且边界完整：`delegated`；
6. 无法唯一命中：`blocked`。

路由结果写入 `ai-route-decision-1`，包含选中路径、命中规则 ID、输入摘要哈希、决策时间、模式和证据级别。模型不得改写决定。

### 4.3 迁移模式

配置增加：

```text
routing_mode = legacy | shadow | enforced
```

- `legacy`：只使用当前固定角色链；
- `shadow`：同时计算新决定并记录，但仍执行旧角色链；
- `enforced`：由新决定控制是否进入编排。

默认从 `legacy` 开始。只有 shadow 样本通过预注册验收且所有安全回归保持绿色后，项目所有者才能切换到 `enforced`。

## 5. 版本化计划 artifact

`ai-plan-1` 不嵌入 `ai-result-1`，而是作为 Sol planner 产生、编排器验证并固定哈希的独立 artifact：

```text
schema_version: ai-plan-1
plan_id
task_id
goal
done_when[]
tasks[]:
  id
  owner_role
  read_scope[]
  write_scope[]
  do_not_touch[]
  depends_on[]
  expected_result
  verification_commands[]
  first_artifact
  evidence_level: L0 | L1 | L2
stages[][]
```

约束：

- ID、目标、完成条件、预期结果和首个 artifact 必须非空；
- 子任务 `id` 同时是该任务 write scope 的稳定 ownership identity；多个任务可以使用同一个 `owner_role`，但同一路径只能映射到一个子任务 ID；
- 路径必须是规范化的仓库相对路径，不允许 `..`、绝对路径、空路径或 glob；
- `write_scope` 必须落在父任务 `allowed_write_paths` 内；
- 每个子任务只能属于一个 stage，依赖只能指向已声明任务；
- stage 顺序必须满足所有依赖，不允许环；
- 同一运行中，两个任务的 write scope 不能相同、父子包含或存在目录前缀重叠；
- read scope 可以重叠，但不能覆盖父任务明确禁止读取的证据面；
- 共享集成文件只能归一个稳定 task owner；owner 一旦派发便不能静默转移；
- 计划哈希、任务信封哈希和 base/candidate 身份绑定后，任何变化都使旧调度与旧验证失效。

## 6. Stage、容量与幂等调度

调度器只消费已验证并冻结的 `ai-plan-1`：

1. 找出当前 stage 中依赖已完成且尚未派发的 ready tasks；
2. 从 Host 读取实时可用容量，不把容量写死在计划中；
3. 按稳定 task ID 排序，只启动前 `N` 个 ready tasks；
4. 其余任务留在同一 stage 的后续 batch；
5. 每次派发在 append-only 账本记录 `dispatch_id`、task owner、计划哈希、scope 哈希、attempt 和候选身份；
6. 恢复时相同 `dispatch_id` 不得重复启动；不同计划或 scope 哈希不得复用旧结果。

容量下降只改变批次大小，不改变冻结依赖或 owner。容量为零、scope 重叠、ownership identity 缺失、同一路径多 owner、循环依赖、提前跨 stage 或账本冲突均停止派发。

本版只允许并行处理互不重叠的 owner 范围；不引入共享 worktree 并行写。涉及写入的任务仍必须位于已授权的隔离 worktree，并遵守现有 changed-path 守卫。

## 7. 运行时身份与权限验真

每个真实角色调用在可信结果进入状态机前必须生成 `runtime-evidence-1`：

```text
schema_version: runtime-evidence-1
attempt_id
requested_role
execution_surface: NATIVE_SUBAGENT | CODEX_EXEC_ROLE_CONTRACT
observed_agent_type
observed_model
observed_reasoning_effort
observed_sandbox_policy
observed_permission_profile
observed_cwd
evidence_source: NATIVE_METADATA | LOCAL_ROLLOUT
observed_at_utc
verification_status: VERIFIED | FAILED
failure_reasons[]
```

检查顺序：

1. 优先读取 Host/native metadata；
2. metadata 缺少 model 或 effort 时，才允许使用本地 rollout inspector；
3. inspector 只输出上述 allowlist 字段，不输出 prompt、消息、环境变量、token、配置内容或任意 payload；
4. rollout 查找必须以精确 attempt/thread ID 唯一匹配，零个或多个匹配都失败；
5. 公共证据和本地证据同时存在时必须一致；
6. `NATIVE_SUBAGENT` 必须提供与请求完全一致的 `observed_agent_type`；`CODEX_EXEC_ROLE_CONTRACT` 的 `observed_agent_type` 必须为 `null`，以明确它不是 custom agent；
7. requested role、model、effort、sandbox、permission 或 cwd 任一缺失、冲突或超出合同，调用结果不得进入可信状态。

审查角色若实际权限比请求的 read-only 更宽，只能在任务不要求 OS 强隔离、prompt 明确禁写且主控验证调用前后 repository/artifact 快照完全一致时，标记为“行为只读”；否则阻断。

## 8. Luna 纳管与开源发布

### 8.1 纳管结论

`luna_worker` 即使早于本项目，也必须从个人配置资产转为项目管理资产。纳管完成需同时满足：

- 仓库保存规范 Agent 模板并为每个发布版本固定 SHA256；
- 项目级 Agent 镜像与规范模板逐字节一致；
- installer、`--check`、安全升级和安全卸载都有自动测试；
- 每次真实调用既校验静态模板，也生成第 7 节的运行时身份与权限证据；
- 交互式原生子代理与自动 `codex exec` 路径在文档、事件和指标中明确区分；
- 角色行为仍由项目合同约束，不允许个人副本发展为第二套规则。

完成这些条件前，只能宣称 Luna 行为合同和 L1 只读烟测已验证，不能宣称开源用户可以复现同一 `luna_worker` 运行环境。

### 8.2 仓库与 Plugin 结构

第一版开源分发使用一个 Git 仓库内的 repo marketplace 和 Plugin：

```text
.agents/plugins/marketplace.json
.codex/agents/luna-worker.toml
plugins/ai-workflow/
  .codex-plugin/plugin.json
  agents/luna-worker.toml
  skills/orchestration/SKILL.md
  scripts/install-agents.sh
  scripts/uninstall-agents.sh
  scripts/inspect-agent-runtime.sh
  scripts/verify.sh
  config/
  runtime/
```

首个开源目标版本为 `v0.2.0`，支持当前 Codex CLI/ChatGPT desktop、macOS 和 Linux 的 POSIX shell、Python 3.11+、Git、`jq` 与 SHA256 工具。Windows 原生生命周期属于后续发布门；README 必须显式列为未验证，而不是暗示兼容。

`plugins/ai-workflow/agents/luna-worker.toml` 是发布用规范模板；`.codex/agents/luna-worker.toml` 是方便贡献者在仓库内新建 Codex 任务时直接发现角色的项目级镜像。`verify.sh` 和合同测试必须证明两者逐字节一致，禁止手工维护两套语义。

Plugin ID 固定为 `ai-workflow@ai-workflow`，Skill 调用名固定为 `$ai-workflow:orchestration`。`.codex-plugin/plugin.json` 只声明 Codex 原生支持的 Plugin 组件；custom agent 不伪装成 manifest 原生组件，而是由 companion installer 单独注册。

本地克隆后的安装流程固定为：

```text
codex plugin marketplace add .
codex plugin add ai-workflow@ai-workflow
plugin_dir="$(codex plugin list --json | jq -r '.installed[] | select(.pluginId == "ai-workflow@ai-workflow") | .installedPath')"
test -n "$plugin_dir" && test -d "$plugin_dir"
sh "$plugin_dir/scripts/install-agents.sh"
sh "$plugin_dir/scripts/install-agents.sh" --check
重新启动 Codex 或新建任务
```

GitHub 发布使用同一 marketplace 和 Plugin 内容，只把 marketplace 来源从本地路径换成已固定 tag 或 commit 的 Git 来源。README 必须从发布元数据生成准确命令，不在代码中硬编码尚未确定的 GitHub owner。

### 8.3 Companion installer 合同

installer 的默认目标是已设置的 `CODEX_HOME/agents`，否则是 `~/.codex/agents`。它不得编辑 `config.toml`，也不得改变无关 Agent。

安装前必须把目标分类为：

- `missing`：可以原子安装；
- `current`：保持不变并报告已是当前版本；
- `known_legacy`：只有摘要命中发布清单中的已知旧版本时才允许原子升级；
- `conflict`：内容被用户修改或来自未知版本，拒绝覆盖；
- `unsafe`：目标或父目录是符号链接、非普通文件或异常路径，拒绝操作；
- `unreadable`：无法取得可靠摘要，拒绝操作。

`--check` 必须完全只读，验证规范模板、安装副本、文件类型、SHA256、TOML 字段和当前版本。普通安装先完成全量 preflight，再使用同目录临时文件与原子替换；preflight 后目标发生变化时停止，不能部分更新。

安全卸载只移除安装状态记录为本项目所有、且当前摘要仍等于已安装摘要的文件。摘要不一致时保留文件并报冲突；不得为了卸载执行递归删除。若安装器创建过备份，恢复也必须校验备份是普通文件、摘要已记录且目标状态仍允许替换。

第一版发布清单必须把当前已验证的个人文件摘要登记为已知版本：

```text
60f7240ea662cd27ea0f51f2e1efa8a2e788e16c76b04a13ab1c1df4f26ef024
```

### 8.4 开启与调用

安装成功并在新任务中完成 Agent 发现后，交互式主入口是：

```text
使用 $ai-workflow:orchestration 执行这个任务，按可信控制平面路由。
```

Skill 只在确定性路由选择 `delegated` 且计划明确分配 Luna 时生成 `agent_type: luna_worker`。用户也可以直接请求“调用 `luna_worker` 做有界只读盘点”，但 Agent 仍必须遵守任务信封、L0/L1/L2 和主控复核。

自动编排继续使用：

```text
python3 scripts/ai_workflow.py validate --task task.json
python3 scripts/ai_workflow.py new --task task.json
python3 scripts/ai_workflow.py run --task task.json --runner live --allow-live-model --role luna
```

自动路径通过 `codex exec -m gpt-5.6-luna` 注入项目角色合同；在 Codex CLI 提供并验证等价的 custom-agent 非交互选择接口以前，不得把它记录成原生 `luna_worker` 子代理调用。事件与成本账本分别记录 `NATIVE_SUBAGENT` 和 `CODEX_EXEC_ROLE_CONTRACT` 两种 execution surface。

### 8.5 发布与调用验收

- 新 clone 在仓库内新建任务时能发现项目级 `luna_worker`；离开仓库后只有完成 companion install 才能发现全局角色；
- Plugin 单独安装但 companion agent 缺失时，Skill 必须给出可执行错误并拒绝用默认 worker 替代；
- `--check` 对 missing、current、known legacy、conflict、unsafe 和 unreadable 六类目标都有正反测试；
- 安装、重复安装、已知版本升级、冲突拒绝和安全卸载在临时 `CODEX_HOME` 中验证，不污染用户真实配置；
- Agent TOML 的 name、model、effort 和 developer instructions 与项目合同一致；
- 原生子代理启动后实际 `agent_type`、model、effort、sandbox、permission 和 cwd 通过 runtime evidence 门；自动 exec 路径必须把 `agent_type` 记为不适用并单独验证其 model、effort、sandbox、permission 和 cwd；
- 原生子代理和自动 exec 的调用数、token、耗时和失败分别计量，不混合成同一身份；
- README 覆盖本地 clone、Git marketplace、安装检查、重启/新任务、显式 Skill 调用、直接 Agent 调用和卸载恢复。

## 9. 成本与效率证据账本

`cost-evidence-1` 以 attempt 为单位记录：

```text
route
role
duration_seconds
prompt_bytes
input_tokens
cached_input_tokens
output_tokens
retry_kind
verification_seconds
quality_outcome
paired_case_id
evidence_class: measured | sample_validated_projection | unavailable
rate_snapshot_id
```

规则：

- token 仅记录运行时明确提供的有限非负数；缺失保持 `null/unavailable`，不得推算；
- `prompt_bytes` 是实际提交内容的 UTF-8 字节数，不替代 token；
- 规划、复审、重复上下文、技术重试、语义返工和失败调用全部计入；
- 金额/credits 投影必须使用独立、带日期和来源的费率快照，并标记为 projection；
- Direct 基线、Sol-only 和 Delegated 使用预注册的 `paired_case_id` 比较；
- 报告分别展示 measured、projection 和 unavailable，禁止混合汇总成伪精确结论。

只有至少 30 个预注册、按风险和任务类型分层匹配的案例完成后，才评估启用效果。若新流程的首交通过率或最终质量相对基线下降超过 5 个百分点，不得宣称成功；只有质量不越过该非劣界且净成本下降，才能使用“降本”结论。否则只报告观察值、置信边界和缺失数据。

## 10. 错误处理与恢复

新增错误均采用闭集代码并写入现有 append-only 事件流：

- `ROUTE_INPUT_INVALID`、`ROUTE_CONFLICT`、`ROUTE_UNDECIDABLE`；
- `PLAN_INVALID`、`PLAN_CYCLE`、`SCOPE_OVERLAP`、`OWNER_CONFLICT`；
- `CAPACITY_UNAVAILABLE`、`DUPLICATE_DISPATCH`、`DISPATCH_IDENTITY_DRIFT`；
- `RUNTIME_IDENTITY_MISSING`、`RUNTIME_IDENTITY_CONFLICT`、`RUNTIME_PERMISSION_MISMATCH`；
- `COST_EVIDENCE_INVALID`。

错误不会自动降级为更便宜或相近的角色。恢复只能重放已验证计划和账本状态：未写文件的中断任务可以在新 attempt 下重派；已经写文件的 owner 不得静默替换；候选、计划或 scope 变化后必须生成新 dispatch identity 并重跑受影响验证。

## 11. 测试与验收

所有功能按 RED→GREEN→REFACTOR 实施。最低验收矩阵：

### 11.1 路由

- 四条路径的补集测试；
- simple direct 为零编排角色调用；
- planning-only 为零 worker 调用；
- 风险任务不能被提示或成本策略降级为 direct；
- legacy 与 shadow 模式不改变旧执行结果。

### 11.2 计划和调度

- 缺字段、未知字段、重复 ID、路径越界、scope 重叠、同一路径多 owner、循环依赖和提前跨 stage 全部确定性拒绝；
- 容量为 `N` 时最多启动 `N` 个 ready tasks；容量变化不改变 owner 和依赖；
- 相同 dispatch ID 幂等恢复，不重复启动；
- dirty tree、HEAD 漂移和 candidate 漂移继续阻断。

### 11.3 运行时验真

- role、model、effort、sandbox、permission、cwd 各自缺失或冲突的故障注入；
- 零个/多个 rollout、公共与本地证据不一致、allowlist 外泄漏全部失败；
- reviewer 权限被放宽时验证前后快照，不满足行为只读条件则阻断。

### 11.4 成本证据

- 缺 token 保持 unavailable；NaN、负值、字符串估算和混合证据拒绝；
- 重试、失败、Sol overhead 和重复上下文都进入同一 paired case；
- measured 与 projection 分栏，费率快照变化不改写历史原始数据；
- 30 个案例门和 5 个百分点质量非劣界由确定性测试覆盖。

### 11.5 回归与负向验证

- 现有完整测试套件必须全部通过；
- 保留并扩展 mutation/failure-injection，确认关闭 risk override、owner guard、runtime identity gate、candidate pin 或 retry limit 时测试会失败；
- `compileall`、Schema/TOML 解析、`git diff --check` 和工作树清洁检查通过；
- 真实 lane 启用前，先用 fake runner 完成 Direct、Sol-only、Delegated 和 Blocked 四条闭环。

## 12. 分阶段发布

1. **基线冻结**：记录旧固定路由的角色调用、耗时、质量和不可用字段；
2. **合同层**：增加 route、plan、runtime 和 cost 四类版本化 Schema 及兼容适配器；
3. **Shadow 路由**：只记录新决定，验证与旧行为的差异和风险覆盖；
4. **计划与调度**：先启用单 stage，再启用动态容量批次；
5. **身份门**：完成故障注入后才允许真实角色结果进入可信状态；
6. **Luna 发布面**：验证项目级 Agent、Plugin、companion installer、runtime inspector 和两种 execution surface；
7. **Enforced 路由**：由所有者显式开启实际分流；
8. **配对实验**：完成预注册样本后发布有证据边界的成本与质量报告。

任何阶段出现安全回归、质量越过非劣界、成本数据无法归因或恢复不幂等，都退回上一模式；回退只改变控制平面模式，不删除历史 artifact 或审计事件。

## 13. 成功定义

本优化完成不等于已经证明固定比例的节省。工程完成标准是：四条路由可确定性执行或交接、计划与并行不会发生 owner/scope 冲突、真实角色身份和权限可证明、Luna 可通过项目级或 companion 安装方式复现、原生子代理与自动 exec 身份不混淆、成本证据可区分实测与投影、现有安全不变量全部保持。

业务效果标准是：预注册配对实验中，在质量相对基线不下降超过 5 个百分点的前提下，观察到净成本下降或端到端时间下降；若没有达到，只报告真实结果并调整或关闭对应路由，不包装为成功。
