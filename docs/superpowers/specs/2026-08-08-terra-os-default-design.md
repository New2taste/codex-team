# Terra OS 默认调度设计

> 状态：`APPROVED_FOR_IMPLEMENTATION`  
> 日期：2026-08-08  
> 授权：项目所有者已明确指定“Terra 常驻、Sol 升级、Luna 降级”为默认，并进一步冻结了轻量项目的推理档和返工交接规则。

## 1. 目标

将当前多模型工作流的默认执行关系固定为：Terra 是常驻执行 OS，Sol 是专家协处理器，Luna 是廉价工具进程。在不削弱原有人工闸门、Git 安全、运行身份、证据合同和重试上限的前提下，减少 Luna 主导实现和同一实现者反复返工。

## 2. 固定角色

- **Terra medium / 执行 OS**：常驻且默认持有施工、集成、调试、恢复及两轮修复；写入仍只能发生在已授权独立 worktree 和允许路径中。
- **Sol medium / 验收协处理器**：负责轻量项目的语义验收、风险审查和裁决。两轮 Terra 修复的报批仍不通过时，原验收 Sol 才临时取得最小修复所有权，并由另一位 Sol medium 验收。
- **Sol xhigh / 大型项目规划师**：只为已确认的大型、跨域项目制定整体方案和全局规划书；必须保留所有者授权，不能被普通实施、复审或返工自动调用。
- **Sol high**：默认没有调度角色，控制器不得自动选择。
- **Luna Max / 廉价工具进程**：只执行原方案定义的 L0/L1/L2 有界取证、机械核对、只读盘点和冻结规格窄域反证。Luna 不拥有主实现、跨文件集成、最终验收或开放式裁决。

模型身份和推理档是本策略的一部分：Terra medium 和 Sol medium 是轻量任务的唯一常驻模型档；Sol xhigh 仅用于授权的大型项目全局规划；Sol high 没有默认职责。

## 3. 默认策略

新增控制器策略名 `terra_os`，并将 Plugin Skill 和新任务的交互式编排默认设为该策略。为兼容历史证据：

- `legacy` 保留，只在显式回放或兼容测试中使用；
- `shadow` 同时记录 `terra_os` 选择和旧有效角色链，不改变本次模型调用；
- `terra_os` 控制新任务的实际角色链；
- 现有 `ai-task-1`、`ai-result-1` 与 `ai-route-decision-1` wire schema 不扩展字段；策略细节属于运行时兼容元数据和 append-only 事件。

确定性角色链：

| 工作类型 | 默认角色链 |
|---|---|
| 简单、低风险、无需模型 | host direct |
| 大型项目的整体方案与全局规划书 | Sol xhigh planner |
| 轻量语义裁决/验收 | Sol medium reviewer |
| 有界只读证据，明确要求 L0/L1/L2 | Luna → Sol（仅在结果需要语义结论时） |
| 普通实现/整改 | Terra → Sol reviewer |
| 已授权大型项目实施 | Sol xhigh planner → Terra → Sol medium reviewer |
| 验收预审需要窄域反证 | Luna 工具步骤 → Sol reviewer；Luna 不是验收者 |
| 无法有界分解或权限不足 | BLOCKED |

Luna 步骤必须由计划显式列出，控制器不得因为“有空闲 Luna”自动插入。

## 4. 两轮 Terra 返工与最终最小移交

Terra 自动返工最多两轮；Sol 只在该两轮都未通过后接管一次最小修复：

1. 初次验收由 Sol reviewer 给出发现。
2. **修复轮 1**：Terra 修复；原验收 Sol 复验。
3. 若复验仍要求修复，进入 **修复轮 2**：仍由 Terra 修复；原验收 Sol medium 再次复验。
4. 若第二轮报批仍要求修复，原验收 Sol medium 才切换为最小范围 fixer，只能处理已登记的开放发现；不得扩大规格或自验。
5. Sol 修复完成后，必须派生另一位 Sol medium reviewer 独立验收。新 reviewer 不继承 fixer 上下文，只读取任务简报、发现、报告和 diff 包。
6. 同级 reviewer 仍发现承重缺陷时，自动返工停止并进入 `BLOCKED`；只有所有者另行授权且项目已重新界定为大型项目时，Sol xhigh 才可制定新的全局规划。不得出现第三轮 Terra 修复或第二次 Sol 直修。

事件必须记录 `repair_round`、`reviewer_identity`、`fixer_identity`、`peer_reviewer_identity`、开放发现和提交范围。Sol fixer 不能输出最终验收状态。

## 5. 安全与失败处理

- Terra、Sol fixer 的写入均受现有 worktree、allowed paths、candidate、HEAD/diff 和 owner authorization 守卫约束。
- Sol planner/reviewer 与 Luna 默认只读；只有两轮 Terra 报批失败后显式 `SOL_REPAIR_AUTHORIZED` 事件能授予原 Sol medium 最小写范围。
- `automatic_xhigh=false`、`automatic_sol_high=false`、`automatic_merge=false`、`automatic_push=false` 保持不变。
- 任一角色身份不匹配、复验代理与 fixer 身份相同、Terra 轮次超过 2、事件缺字段或 scope 扩大时 fail closed。
- `max_implementation_reworks=2` 解释为 Terra 自主返工上限；最终一次 Sol 接管不增加 Terra 重试次数。

## 6. 分发与用户体验

- `$ai-workflow:orchestration` 默认说明 Terra OS 关系并执行确定性 preflight。
- Agent 模板仍只分发 `luna_worker`；Terra 与 Sol 使用平台模型/子代理选择，不伪装为自定义 Agent。
- README 同时说明默认策略、显式 legacy 回放、shadow 迁移、两轮 Terra 返工、一次 Sol medium 最小接管和 Sol xhigh 大型规划人工门。
- Plugin runtime/config 必须继续与仓库源逐字节一致。

## 7. 验收标准

1. 普通写任务默认角色链没有 Luna，且 Terra 是唯一施工所有者。
2. Luna 只能在明确有界 L0/L1/L2 工具步骤出现，不能替代 Terra 或 Sol。
3. 两轮修复均由 Terra；只有第二轮报批仍失败后才由原验收 Sol medium 修复，另一位 Sol medium 独立复验。
4. 第三轮 Terra 修复、第二次 Sol 直修、同一 Sol 自修自验、自动 Sol high/xhigh、自动 merge/push 全部被测试拒绝。
5. legacy 回放仍保持旧角色链；shadow 不改变本次调用；新 Plugin/Skill 默认展示 `terra_os`。
6. 全量测试、Plugin/Skill validators、byte-parity verifier 和变异测试通过。

## 8. 非目标

- 不自动批准 Sol 写权限；授权来自当前任务所有者已批准的工作流策略和具体任务范围。
- 不为轻量任务调用 Sol high 或 Sol xhigh，不改变 Luna 的 L0/L1/L2 内容合同。
- 不引入第三方运行依赖、数据库、常驻服务或第三轮自动返工。
- 不在本任务中 merge、push、删除 worktree 或发布到无关 Git remote。
