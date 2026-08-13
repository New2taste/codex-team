# Sol-medium Final-Rework Default Design

> 状态：`APPROVED_FOR_IMPLEMENTATION`
> 日期：2026-08-13
> 范围：全局 AI Workflow 的默认施工、终局验收与返工梯级。

## 目标

在保持集中终局验收的前提下，将 Sol-medium 最终验收失败后的首次返工，改为由另一名
Sol-medium 优先完成，避免直接升级到 Sol xhigh 或重启中间工程审查循环。

## 默认流程

1. 每个工程小节由其施工 owner 按冻结信封完成实现、自检、目标测试与必要负向检查；
   小节之间不再派发独立对抗式验收。
2. 全部小节完成后，由一名只读 Sol-medium 对固定 base/candidate、任务契约、证据包和
   自动门禁进行一次集中、对抗式最终验收。
3. 若该验收返回 `REWORK`，控制器冻结该验收的 finding 集、候选提交、允许路径和
   验证命令，并将一次性、assignment-scoped 写入授权交给**不同身份的 Sol-medium
   fixer**。原验收者始终只读，不能修复自己的结论。
4. Sol-medium fixer 完成后，由又一名不同身份的 Sol-medium 对同一冻结案卷做一次
   最终复验。该复验只可 `ACCEPT`、`REWORK` 或 `BLOCKED`，不得扩展 scope。
5. 若复验仍为 `REWORK`，才可由 owner 明确授权 Sol xhigh 做一次终局修复；终局修复
   不再启动 task-level 审查，也不得成为 Sol xhigh 常驻施工权限。

## 不变量

- Sol-medium 的验收、修复和复验必须由三个不同的 runtime identity 执行；每个身份、
  模型、推理档、execution surface 和候选提交均进入 receipt/evidence。
- Sol-medium fixer 只能写冻结的失败项范围；不能合并、推送、扩大文件范围或变更任务
  契约。
- 中间工程小节的“无对抗审查”不等于无验证：施工 owner 仍必须执行信封内的测试、
  负向检查、范围核对和运行时证据门。
- `REWORK`、授权、修复、复验和终局升级均须 append-only；不得借由覆盖状态或重放
  receipt 改变顺序。
- `BLOCKED` 不能被解释为通过，也不能静默改派到另一角色。

## 公开配置与分发

- 根 `config/ai_workflow.toml` 将显式声明此终局返工梯级；Plugin 配置必须字节一致。
- README 与 Plugin `SKILL.md` 是公开权威说明；它们不得继续声称每个工程小节必有
  Terra 对抗审查，或把 Sol-medium 修复仅限于第二次 Terra 本地验收失败。
- 分发测试应固定检查根/Plugin 配置一致，以及公开说明中存在该梯级并禁止旧的逐小节
  审查表述。

## 边界

本次不重写已完成项目的历史计划、报告或 ledger。现有 `adversarial-acceptance-1`
实现继续保护 task-level assignment；若它尚未承载 whole-project final-acceptance
事件，本次不得伪称已将该历史 ledger 改写为新政策。运行时的全局编排器在实际派发
Sol-medium fixer 前必须由后续实现消费此配置和冻结 receipt。

## 验证

- 先写失败的公开策略测试：旧逐 task Terra review 或“Sol-medium 仅第二次 Terra
  失败后 fallback”的文本/配置必须失败。
- 更新根配置、Plugin 镜像、README、SKILL 与策略测试后，运行策略测试、完整分发测试、
  Plugin verifier、Skill validator、根/Plugin byte parity、全量 unittest、compileall
  与 `git diff --check`。
- 若没有真实 whole-project final-acceptance controller，最终报告必须将其列为后续运行时
  接口工作，而非宣称已可自动派发。
