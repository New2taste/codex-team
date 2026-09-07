# Astra medium 角色迁移实施计划

**日期：** 2026-09-07
**状态：** 按 Astra medium 增量规划实施
**目标：** 将 `gpt-6-astra / medium` 纳入 Codex Team 的默认闭集工作流，接替总体规划、闭集裁定和 owner-authorized 终局修复职责；保持 Terra xhigh 常驻施工、Luna max 有界工具执行和 Sol medium 集中最终验收的既定边界。

## 1. 当前角色契约

| 角色 | 模型 / 档位 | 当前职责 | 明确边界 |
|---|---|---|---|
| Luna max | `gpt-5.6-luna / max` | 冻结 envelope 内的低风险机械任务、确定性检查和证据抽取 | 不规划、不施工复杂任务、不审批、不终验 |
| Terra xhigh | `gpt-5.6-terra / xhigh` | 常驻施工 OS：复杂施工、调试、集成和开放式问题拆解 | 不 merge、push 或自我验收 |
| Sol medium | `gpt-5.6-sol / medium` | 监督总体规划实现、集中最终验收，并执行终验失败后的首轮有界修订 | 不承担常驻施工；不负责首轮修订后的独立 peer 验收 |
| Astra low | `gpt-6-astra / low`，canonical role：`astra_low_reviewer` | Sol-medium 首轮修订后的独立、只读 peer 验收 | 不规划、不写入、不批准、不执行终局修复 |
| Astra medium | `gpt-6-astra / medium` | owner-authorized 总体规划、闭集裁定，以及 peer 验收仍失败后的终局修复 | 不自动启动，不绕过 owner gate 或最终验收 |

Astra medium 的只读规划和闭集裁定能力可以使用只读权限投影；终局修复只能使用 assignment-scoped write capability。两种能力必须共享同一模型/档位身份，但不能共享越权权限。

## 2. 默认生命周期

```text
用户目标
  → task envelope / Schema / runtime identity 校验
  → 必要时使用 Luna max 做有界事实抽取
  → Astra medium 只读规划与闭集裁定（需要 owner gate 时）
  → Sol medium 监督总体规划实现、分解任务并制定施工计划
  → Luna max 有界施工或 Terra xhigh 复杂施工
  → 每个工程小节完成施工自检和运行时证据门
  → 固定 clean candidate
  → Sol medium 集中、只读、对抗式最终验收
  → ACCEPT：owner 关闭任务
  → REWORK：Sol medium 首轮有界修订
  → Astra low (`astra_low_reviewer`) 独立、只读 peer 验收
  → ACCEPT：owner 关闭任务
  → 再次 REWORK：owner 明确授权 Astra medium 一次性终局修复
  → 不再启动 task-level 终局复验，回到 owner 关闭边界
```

中间工程小节继续采用 `section_self_check_only`，不逐小节启动独立对抗式审查。终验仍必须深入分析、充分发现问题，并以冻结的 findings、candidate、路径和验证命令约束所有返工。

## 3. 实施范围

按以下边界分阶段落地：

1. 在角色配置、路由声明、运行时身份、结果/计划/证据 Schema 和 verdict/repair ledger 中登记 Astra medium 与 `astra_low_reviewer` 的闭集身份。
2. 将总体规划、闭集裁定和终局 repair transition 的执行身份切换为 Astra medium；普通施工路由保持 Terra xhigh 为默认 OS，低风险有界任务仍可使用 Luna max。
3. 将终验失败后的 repair ladder 固定为“Sol medium 首轮修订 → Astra low 独立只读 peer 验收 → owner-authorized Astra medium 终局修复”。接收者、修订者和 peer 验收者不得因同一会话或自报身份而合并。
4. 由 Terra xhigh 完成核心运行时、路由、repair/verdict 状态机和测试施工；由 Luna max 完成低风险闭集登记、文档、计划记录和 fixture 文本同步；最终由独立验收角色检查行为、身份和证据闭环。
5. root 文件完成后再执行唯一的 root-to-Plugin 同步，并通过 parity、Schema、运行时身份、repair ledger、路由和完整验证入口。

本迁移不新增自动 merge、自动 push、自动批准、常驻后台服务或未授权的并行写入。

## 4. 协议兼容

既有账本和已生成 artifact 需要可重放，因此以下 wire/ledger ID 保留为兼容标识。角色 key 同时是当前派发协议的稳定字段；在本迁移后，使用这些 key 创建的新 assignment 明确绑定 Astra medium，不再绑定旧 Sol 模型：

- `sol_xhigh`、`sol_xhigh_planner`：保留的 wire role key；旧 artifact 按原始 runtime evidence 读取，若恢复执行需重新通过当前 runtime contract，旧 Sol evidence 不会静默转换；新 assignment 按当前配置绑定 `gpt-6-astra / medium`；
- `SOL_XHIGH_TERMINAL_REPAIR`：历史 repair phase/event 命名，保持解析兼容；
- `authorize_final_xhigh`：历史 owner 授权命令 ID，实际授权后的当前执行角色为 Astra medium。

新生成的 runtime evidence 必须证明 `gpt-6-astra / medium` 或 `gpt-6-astra / low` 与分配的 role key、权限、沙箱、cwd 和会话绑定；role key 本身不能伪造实际模型身份。历史计划和 changelog 不回写，只在当前有效文档中说明兼容层。

## 5. 验收判据

迁移完成必须同时满足：

- 核心闭集能识别 `astra_low_reviewer`，并拒绝未登记的 Astra role/model/effort 组合；
- Astra low runtime contract 固定为 `gpt-6-astra / low`、read-only、不可写入，且 peer 验收与 Sol-medium fixer 身份独立；
- Astra medium planner contract 固定为 `gpt-6-astra / medium`、只读规划/裁定权限；terminal repair contract 仅接受 owner authorization、冻结 findings/paths/commands 和 assignment-scoped write；
- 普通 `PLAN_REQUIRED`、construction、final acceptance 路由不因本迁移隐式增加模型，不改变 Terra xhigh 常驻施工和 Luna max 有界任务边界；
- Sol medium 仍签发唯一集中 `REVIEW_1`，终验失败时仅产生首轮 Sol-medium 修订；首轮修订完成后由独立 `astra_low_reviewer` 复验，只有再次 `REWORK` 才能进入 Astra medium 终局修复；
- 兼容 ID 的旧 artifact 可解析并在身份不满足当前 contract 时 fail-closed；新 assignment 使用同一稳定 role key 时必须绑定 Astra runtime identity，且不改写旧 receipt；
- root 与 Plugin 的配置、runtime、Schema 和 Skill 镜像通过同步器保持一致，相关定向测试、负向测试、`git diff --check` 和完整验证入口通过；
- 不自动 merge、push 或删除用户文件；迁移期间的用户已有未跟踪文件保持不变。
