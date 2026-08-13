# Team Call 自然语言调用指令设计

## 目标

提供一个最小、可审计的自然语言工作流入口：用户以一句
`team call <任务>` 调用团队。入口根据可验证的风险分类，直接执行
确定性的低风险检查，或将其交给现有的计划、施工、验收状态机；它
不得创建一条绕开既有角色、人工闸门或证据要求的旁路。

## 范围与非目标

本设计只新增调用指令、其收据、分流判定和发布文档。它保留当前的
`luna` / `luna_construction` 工作流角色、L0/L1/L2 证据要求、冻结施工
信封、重试阶梯、成本账本、独立 Terra xhigh task 验收和 Sol-medium
最终整体验收。

它不是一个任意 shell 命令入口、自动批准器、自动合并/推送器或并发
“拉群”功能。一次 team call 最多只启动一项活动工作；是否使用模型由
风险判定和既有角色边界决定，而不是由名称中的 team 决定。

## 指令语法

只有消息去除前导空格后以 ASCII、不区分大小写的 `team call` 开头才
触发。它必须紧跟空白、半角冒号或全角冒号，并携带非空任务正文：

```text
team call 检查当前工作区状态
team call: 为 README 增加安装示例
team call：核对 Plugin 根/镜像一致性
```

引用、代码块、普通正文或字符串中出现 `team call` 不触发。空正文返回
`TEAM_CALL_EMPTY`；无法解析为任务正文返回 `TEAM_CALL_INVALID`。系统保存
用户原文（仅按现有日志脱敏规则保留）及其 SHA-256，正文的展示性空白
可以规范化，但不得改变原文摘要。

## 受控风险分流

控制器将自然语言转换为草案 intake；草案不是执行授权。执行前必须通过
结构化校验，并且在不完整、矛盾或无法证明安全时一律升级，而不是猜测。

| 分流 | 条件 | 行为 |
|---|---|---|
| `DIRECT_L0` | 命中内建、无写入、无网络/凭据/外部副作用的确定性检查 | 控制器直接执行固定 argv，保存命令、退出码和产物；不启动模型。 |
| `DIRECT_L1` | 只读、范围精确、可形成 L1 证据包，且目标不涉及安全或外部系统 | 单个 Luna Max 只读执行，使用固定输入/输出契约。 |
| `PLAN_REQUIRED` | 任何代码或文档写入、范围不精确、多阶段、成本较高、外部系统、权限/安全事项，或分类不确定 | 创建任务和收据，交给现有计划/路由流程；施工仍需现有冻结信封和 owner gate。 |
| `BLOCKED` | 指令注入、无效任务、禁止的操作或无法验证的输入 | 不创建可执行 dispatch，不启动模型。 |

`DIRECT_L0` 的操作来自代码中显式定义的固定 allowlist，不接受用户提供的
shell 片段。`DIRECT_L1` 必须带有精确只读范围和验证项；否则降级为
`PLAN_REQUIRED`。因此一句话可以立即开始工作，但“立即”永远不等于绕过
风险检查：复杂任务会立即进入计划阶段，在既有人工闸门处停下。

## 身份、事件与幂等性

每次成功解析的调用生成 `TeamCall` 记录，至少包含：

```text
directive_version, call_id, raw_request_sha256, intake_sha256,
disposition, risk_reasons, task_id, created_at_utc
```

`call_id` 由指令版本、原文摘要和规范化 intake 的规范 JSON 计算。控制器
在同一状态根内对它加锁：相同 `call_id` 只返回既有收据/任务，绝不重复
启动 dispatch；同一调用标识却不同摘要或 intake 报
`TEAM_CALL_IDENTITY_DRIFT`。收据写为 append-only `TEAM_CALL_RECEIVED`，
随后以 `TEAM_CALL_ROUTED` 记录最终分流及其已存在的 task/route 绑定。

直接执行也必须将 `call_id` 绑定到其 L0/L1 尝试和结果。计划路径仅创建或
恢复既有 `WorkflowStore` 任务，后续状态转换仍由当前的 task、route、plan
和 dispatch 身份校验保护。

## 角色与成本边界

- L0 由控制器执行，避免为简单状态/一致性检查消耗模型额度。
- 合格 L1 优先使用 Luna Max；它只能只读并交付 L1 证据，不能 review、
  approve 或 final-accept。
- 写入与复杂施工沿用现有 Terra xhigh / Luna construction 信封选择，且每个
  task 继续由不同 Terra xhigh 对抗审查。
- Sol medium 仍只负责最终整体验收和既定的第二次 Terra 失败后的有界
  fallback；Sol xhigh 仍是闭案规划/终局升级角色。
- 因而 `team call` 默认串行，不会因为一条请求而创建多个并行子代理。

## 接口与发布

新增一个纯标准库的解析与编排 API（由 CLI 与对话层共同调用），概念接口为：

```python
parse_team_call(message: str) -> TeamCall | None
route_team_call(call: TeamCall, *, state_root: Path, controller: TeamCallController) -> TeamCallReceipt
```

`None` 表示不是调用指令；格式错误须抛出带稳定错误码的 `WorkflowError`。
CLI 提供等价的 `team-call` 子命令接受任务正文，便于可复现测试，但发布文档
将 `team call <一句任务>` 作为主入口。根运行时与 Plugin 镜像、README、
orchestration Skill 和分发验证必须同步更新。

## 失败处理

- 解析或 intake 校验失败：无 task、无 dispatch、无模型启动。
- allowlist 外命令、shell 元字符或任何外部副作用：`BLOCKED`。
- L1 结果不满足只读、证据或运行时身份要求：沿用既有 fail-closed guard，
  不自动升级为写入执行。
- 写入/复杂任务的计划失败、owner gate 未获批准或 route 不一致：沿用既有
  状态机记录并停在可识别状态。
- 同一调用重复：返回相同收据；任何身份或账本漂移均 fail closed。

## 测试与验收

测试必须先证明：前缀外文本不触发、空命令失败、解析后 L0 固定 argv 执行、
shell 注入被拒、L1 只读身份与证据被校验、写入/不确定输入进入计划、重复
调用不重复 dispatch、调用标识篡改被拒，以及 task/route/Plugin 镜像/文档
的发布契约同步。对抗式验收还要在复制的发布目录中篡改镜像和 allowlist，
确认 verifier 与调用入口拒绝它们。

完整验收仍须运行全量 unittest、compileall、Plugin verifier、Skill/plugin
validator、shell 语法检查和 `git diff --check`。不进行自动 merge、push 或
发布。
