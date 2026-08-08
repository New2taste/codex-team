# Terra OS 默认调度设计

> 状态：`APPROVED_FOR_IMPLEMENTATION`  
> 日期：2026-08-08  
> 授权：项目所有者已明确指定“Terra 常驻、Sol 升级、Luna 降级”为默认，并授权直接采用 Sol 裁决继续实施。

## 1. 目标

将当前多模型工作流的默认执行关系固定为：Terra 是常驻执行 OS，Sol 是专家协处理器，Luna 是廉价工具进程。在不削弱原有人工闸门、Git 安全、运行身份、证据合同和重试上限的前提下，减少 Luna 主导实现和同一实现者反复返工。

## 2. 固定角色

- **Terra xhigh / 执行 OS**：默认持有施工、集成、调试、恢复、普通修复和跨文件协调；写入仍只能发生在已授权独立 worktree 和允许路径中。
- **Sol medium / 专家协处理器**：负责方案冻结、语义裁决、风险审查和验收。进入第二轮返工时，原验收 Sol 临时取得该轮最小修复所有权。
- **Sol xhigh / 升级专家**：只处理原方案规定的重大冲突、不可逆风险或重复语义失败；必须保留所有者授权，禁止因“Sol 升级”而自动调用。
- **Luna Max / 廉价工具进程**：只执行原方案定义的 L0/L1/L2 有界取证、机械核对、只读盘点和冻结规格窄域反证。Luna 不拥有主实现、跨文件集成、最终验收或开放式裁决。

模型身份和推理档保持原值；“升级/降级”指调度权限和参与位置，不改变模型名称。

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
| 纯规划/语义裁决 | Sol planner |
| 有界只读证据，明确要求 L0/L1/L2 | Luna → Sol（仅在结果需要语义结论时） |
| 普通实现/整改 | Terra → Sol reviewer |
| 高风险实现/整改 | Sol planner → Terra → Sol reviewer |
| 验收预审需要窄域反证 | Luna 工具步骤 → Sol reviewer；Luna 不是验收者 |
| 无法有界分解或权限不足 | BLOCKED |

Luna 步骤必须由计划显式列出，控制器不得因为“有空闲 Luna”自动插入。

## 4. 两轮返工与所有权移交

自动返工最多两轮：

1. 初次验收由 Sol reviewer 给出发现。
2. **修复轮 1**：Terra 修复；原验收 Sol 复验。
3. 若复验仍要求修复，进入 **修复轮 2**：原验收 Sol 从 reviewer 切换为最小范围 fixer，只能处理已登记的开放发现；不得扩大规格或自验。
4. 修复轮 2 完成后，必须派生另一个同模型、同推理档的 Sol reviewer 子代理独立验收。新 reviewer 不继承原 Sol 的实现上下文，只读取任务简报、发现、报告和 diff 包。
5. 同级 reviewer 仍发现承重缺陷时，自动返工停止并进入 `BLOCKED` 或所有者授权的 Sol xhigh 升级；不得出现第三轮自动修复。

事件必须记录 `repair_round`、`reviewer_identity`、`fixer_identity`、`peer_reviewer_identity`、开放发现和提交范围。第二轮的 Sol fixer 不能输出最终验收状态。

## 5. 安全与失败处理

- Terra、Sol fixer 的写入均受现有 worktree、allowed paths、candidate、HEAD/diff 和 owner authorization 守卫约束。
- Sol planner/reviewer 与 Luna 默认只读；只有第二轮显式 `SOL_REPAIR_AUTHORIZED` 事件能授予原 Sol 最小写范围。
- `automatic_xhigh=false`、`automatic_merge=false`、`automatic_push=false` 保持不变。
- 任一角色身份不匹配、复验代理与 fixer 身份相同、轮次超过 2、事件缺字段或 scope 扩大时 fail closed。
- 旧的 `max_implementation_reworks=1` 解释为 Terra 自主返工上限；新增 Sol 接管轮不增加 Terra 重试次数。

## 6. 分发与用户体验

- `$ai-workflow:orchestration` 默认说明 Terra OS 关系并执行确定性 preflight。
- Agent 模板仍只分发 `luna_worker`；Terra 与 Sol 使用平台模型/子代理选择，不伪装为自定义 Agent。
- README 同时说明默认策略、显式 legacy 回放、shadow 迁移、两轮返工和 Sol xhigh 人工门。
- Plugin runtime/config 必须继续与仓库源逐字节一致。

## 7. 验收标准

1. 普通写任务默认角色链没有 Luna，且 Terra 是唯一施工所有者。
2. Luna 只能在明确有界 L0/L1/L2 工具步骤出现，不能替代 Terra 或 Sol。
3. 第一轮修复属于 Terra；进入第二轮时由原验收 Sol 修复，另一个同级 Sol 独立复验。
4. 第三轮自动修复、同一 Sol 自修自验、自动 Sol xhigh、自动 merge/push 全部被测试拒绝。
5. legacy 回放仍保持旧角色链；shadow 不改变本次调用；新 Plugin/Skill 默认展示 `terra_os`。
6. 全量测试、Plugin/Skill validators、byte-parity verifier 和变异测试通过。

## 8. 非目标

- 不自动批准第二轮写权限；授权来自当前任务所有者已批准的工作流策略和具体任务范围。
- 不改变模型名称、推理档或 Luna 的 L0/L1/L2 内容合同。
- 不引入第三方运行依赖、数据库、常驻服务或第三轮自动返工。
- 不在本任务中 merge、push、删除 worktree 或发布到无关 Git remote。
