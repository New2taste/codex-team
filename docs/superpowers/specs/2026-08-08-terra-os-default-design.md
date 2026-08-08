# Terra / Luna 任务编排与对抗式验收设计

> 状态：`APPROVED_FOR_IMPLEMENTATION`
> 日期：2026-08-08
> 授权：项目所有者已明确指定本设计为默认，并授权直接采用 Sol 的规划裁决继续实施。

## 1. 目标

以 Terra xhigh 保留复杂软件工程、集成、调试和审查能力，同时把明确、可验证、低风险的 coding、测试、文档和机械同步工作分流给 Luna max。每个 task 的局部质量门由独立 Terra xhigh 对抗式验收；Sol medium 只承担最终整体对抗式验收，除非进入明确的第三次修复例外。

## 2. 固定角色

- **Terra xhigh / 常驻 OS**：负责复杂或高风险施工、跨文件集成、调试、任务分解、Luna 任务信封、修复升级和每个 task 的独立对抗式验收。施工 Terra 与验收 Terra 必须是不同 actor identity。
- **Luna max / 中初级 coding 执行器**：默认优先承接具有固定读写范围、可判定输出和可运行测试的低/中复杂度任务；包括机械多文件修改、测试补充、fixture、byte-parity、文档、格式/配置迁移及已定位的局部 bug。它必须在 task 信封内 TDD/验证，不拥有架构、开放式 debug、跨域协议、权限/安全承重变更、普通任务验收或最终验收。
- **Sol medium / 最终整体验收者**：不参与普通 task 计划、施工或局部验收；只在所有 task 通过 Terra 对抗式验收后，做跨 task 的最终整体对抗式验收。例外：第二次 Terra 验收仍失败时，Sol medium 执行第三次修复，另一独立 Sol medium 验收该例外修复。
- **Sol xhigh / 大型项目规划师与终局 fixer**：为明确大型/跨域项目编写总体方案和全局规划书。若 Sol-medium 例外修复仍失败，Sol xhigh 直接做一次终局修复，不再有 task-level 验收；最终整体 Sol medium 验收仍在所有任务结束后执行。
- **Terra medium、Sol high**：没有默认调度角色，控制器必须拒绝自动选择。

## 3. 默认分流

Luna 不再只是只读工具。Terra xhigh 的任务分解为每个子任务明确 `owner_role`、允许路径、L0/L1/L2 证据、可复现验证和升级条件。

| 子任务特征 | 默认施工 owner | 局部验收 |
|---|---|---|
| 固定范围、确定性代码/测试/文档/fixture/parity/config 修改 | Luna max | 独立 Terra xhigh 对抗式验收 |
| 单模块、已定位 bug，完整负例和测试可描述 | Luna max | 独立 Terra xhigh 对抗式验收 |
| 跨文件协议、运行时身份、授权、并发/持久化、安全、开放式 debug、架构/集成 | Terra xhigh | 独立 Terra xhigh 对抗式验收 |
| 大型项目总体方案 | Sol xhigh planner | 不施工；由 Terra xhigh 分解执行 |
| 最终跨 task 系统验收 | Sol medium | 对抗式整体验收 |

Luna 的资格必须 fail closed：没有明确 task 信封、范围、确定性 done-when、至少一个负例/变异和可运行验证命令时，任务改派 Terra xhigh。控制器不得因 Luna 空闲自动插入，也不得把 Luna 用作 reviewer。

## 4. 每个 task 的对抗式验收与升级

1. 原施工 owner（Luna 或 Terra xhigh）提交候选、命令证据和限定 diff。
2. **第一次 task 验收**由一位与施工 owner 不同的 Terra xhigh 执行对抗式审查。它必须尝试证伪，而不是复述测试结果：重跑相关验证、检查授权范围和旧契约、构造至少一个现实负例/变异、审计实际 diff 与运行时路径，并登记新发现。
3. 若失败，原施工 owner 在登记 finding 范围内修复并再次提交。
4. **第二次 task 验收**由另一位独立 Terra xhigh 对抗式审查；该 reviewer 不得与施工 actor 或第一次 reviewer 相同。
5. 若第二次验收仍失败，Sol medium 只修复已登记且未关闭的 finding；另一位独立 Sol medium 对抗式验收该修复。
6. 若该 Sol peer 仍失败，Sol xhigh 执行一次终局修复。此终局修复不再触发 task-level 验收、不会自动扩展范围，也不会产生另一轮修复。
7. 全部 task 结束后，独立 Sol medium 执行一次最终整体对抗式验收：检查 task 间契约、Plugin/source parity、状态机、运行时身份、报告、测试门和剩余风险。它不重开已关闭 task 的无限返工；若发现承重问题，报告为整体 `BLOCKED` 并列出需要所有者裁决的最小范围。

每个 acceptance 事件都记录 fixer/reviewer identities、candidate/base commit、finding IDs、allowed paths、负例/变异命令、实际输出和 verdict。验收结论只有 `ACCEPT`、`REWORK` 或 `BLOCKED`；不得由施工 actor 自验。

## 5. 安全与兼容

- 不改变 `ai-task-1`、`ai-result-1` 与 `ai-route-decision-1` 的字段或版本；角色、repair 和 review 细节记录在运行时事件和兼容元数据。
- `legacy` 保留历史角色链；`shadow` 只记录新候选；`terra_os` 执行新分流。Luna task ownership 来自经验证的本地计划步骤，而不是 route schema 猜测。
- 所有写入仍受 worktree、allowed paths、candidate、HEAD/diff、运行时身份、敏感信息和 Git 守卫约束。
- `automatic_sol_high=false`、`automatic_merge=false`、`automatic_push=false` 保持；Sol xhigh 的规划与终局修复只能由上述显式状态机触发。

## 6. 验收标准

1. Luna eligible task 能实际施工并交付 L0/L1/L2 证据；没有资格证明时 fail closed 到 Terra xhigh。
2. 每个 task 都由不同 Terra xhigh 做两次以内的对抗式局部验收；普通 Sol medium 不在该链路。
3. 第二次 Terra 验收失败后，只有 Sol medium 可进行第三次限定修复，且由不同 Sol medium 验收；再失败只允许一次 Sol xhigh 终局修复。
4. Terra medium、Sol high、Luna reviewer、施工 actor 自验、未登记修复、自动 merge/push 均被测试拒绝。
5. 最终 Sol medium 整体验收覆盖跨 task、Plugin parity 和全量验证；最终用户结论不能依赖单 task 的绿色测试。
