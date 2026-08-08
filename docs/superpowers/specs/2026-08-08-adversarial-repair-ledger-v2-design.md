# 对抗式验收账本 v2 设计

> 状态：`APPROVED_FOR_IMPLEMENTATION`
> 日期：2026-08-08
> 决策者：Sol xhigh 总体规划；项目所有者已授权直接采纳。

## 1. 问题与边界

历史 `repair-ledger-1` 将 Terra 修复和 Sol 验收写成固定的、调用者自报身份的轮次；验收成功后账本还会释放 task 给通用 runner。这既不符合当前的独立 Terra 对抗式验收梯子，也无法证明 actor 身份、候选提交或实际修改范围。

本设计新增 `adversarial-acceptance-1` 账本。v1 保持只读回放兼容，不能被混入或升级为 v2。不得修改 `ai-task-1`、`ai-result-1`、`ai-route-decision-1` 字段/版本、`legacy|shadow|enforced` wire、全局 Sol 写权限、自动 merge/push 或 Plugin 镜像。

## 2. 状态机

```text
REVIEW_1 --ACCEPT--> TASK_TERMINAL(PENDING_WHOLE_PROJECT_ACCEPTANCE)
    | REWORK
    v
OWNER_REPAIR --completed--> REVIEW_2
    |                         | ACCEPT --> TASK_TERMINAL
    |                         | REWORK
    v                         v
              SOL_MEDIUM_REPAIR --completed--> SOL_MEDIUM_PEER_REVIEW
                                                    | ACCEPT --> TASK_TERMINAL
                                                    | REWORK
                                                    v
                                       SOL_XHIGH_TERMINAL_REPAIR --completed--> TASK_TERMINAL
```

- `REVIEW_1` 和 `REVIEW_2` 都必须由 `terra_xhigh` 只读对抗式验收。review #1 的 runtime instance 不能等于 owner；review #2 不能等于 owner 或 review #1。
- `OWNER_REPAIR` 必须由原 owner runtime instance 完成，允许 `luna` 或 `terra_xhigh`。
- `SOL_MEDIUM_REPAIR` 仅在 review #2 的 `REWORK` 后出现，且只能处理该 review 的精确 canonical findings；其 peer runtime instance 不得相同。
- `SOL_XHIGH_TERMINAL_REPAIR` 仅在 Sol peer `REWORK` 后出现。完成即 task terminal，绝无 task-level review 或下一次修复。
- `BLOCKED` 与每个成功 terminal 都永久占有 task；仍要求独立 Sol medium 的最终整体验收。通用 runner 不得继续该 task。

## 3. 可验证身份与 capability

`ActorIdentity` 仅表示预期角色，不能作为完成或验收的证明。`VerifiedActorReceipt` 必须包含：

- `assignment_id`、`attempt_id`、execution surface、稳定 `runtime_instance_id`；
- requested role、observed model/reasoning effort/sandbox/permission/cwd；
- 运行时证据哈希，以及不可由模型输出提供的 native agent UUID 或 Codex thread identity。

身份相等由 `execution_surface + runtime_instance_id` 判断；display name、role 和 attempt id 均不能伪造“独立 reviewer”。

每个运行建立一次 `AssignmentCapability`，其不可变 ID 哈希：task/task SHA、assignment/phase、attempt、期望 receipt、base/input candidate、finding IDs、允许路径、有限写权限、禁止 merge/push 和 issuing event。专用 adapter 只接受当前 capability 和 `run_assignment(...)`，不得回退 `runner.run(...)`。Sol 的临时写能力只来自当前 assignment capability；通用 Sol config 继续只读。

## 4. 追加式账本

账本版本为 `adversarial-acceptance-1`。每一事件含严格闭集的共同字段：

```text
ledger_version, event_type, event_index, event_id, previous_event_id,
timestamp_utc, task_id, task_sha256, base_commit, candidate_commit
```

`event_id` 是去除自身后的 canonical JSON SHA-256，`previous_event_id` 构成链。允许事件：

- `ACCEPTANCE_OPENED`
- `ASSIGNMENT_ISSUED`
- `ASSIGNMENT_ATTEMPT_STARTED`
- `ASSIGNMENT_ATTEMPT_FAILED`
- `REPAIR_COMPLETED`
- `REVIEW_COMPLETED`

REWORK 必须有非空、规范排序的 findings；ACCEPT 必须没有 findings。新 findings 只能由 `REVIEW_COMPLETED(REWORK)` 创建，下一 assignment 必须逐项复制其 ID、路径和顺序。每个 repair completion 将 input candidate 推进到一个非 merge descendant output candidate；实际 diff/snapshot 是范围校验的权威，结果 JSON 的 `changed_files` 只作辅助信息。

launch 前先追加 `ASSIGNMENT_ATTEMPT_STARTED`。一个 attempt 仅可有一个终局结果。异常、超时、身份/范围/结果错误走同一 guard，保证一次 `FAILED`；重启发现孤儿 STARTED 时仅追加一次 `INTERRUPTED` failed record。技术 retry 必须新 attempt ID，且不能制造新的语义修复机会。

## 5. Controller 集成

在 `run_until_gate()` 任何通用 state transition 前调用账本 ownership 查询。只要该 task 存在 v2 账本（open、failed、accepted 或 terminal），通用路径都返回 `REPAIR_ADAPTER_REQUIRED`，零模型调用、零状态污染。专用 adapter 负责 issued assignment 的 fix/review，并验证 capability、receipt、diff、result 和 ledger replay。

完成后 re-export 必要的新 v2 API；历史 v1 公共 import 保持但创建新 v1 history 报 `REPAIR_PROTOCOL_V1_DISABLED`。

## 6. 必测反例

1. owner 自验、review #1 被 #2 复用、同 actor 换 attempt ID、Luna/Terra-medium/Sol-high reviewer 均失败。
2. owner 被其他 Terra 接管修复、Sol fixer 兼任 peer、跳过 review #2、Sol peer REWORK 后多次 Sol xhigh 修复均失败。
3. 空/新增/改名/乱序 findings、跨 task 或 stale capability/candidate、伪造 changed_files 与实际越界 diff、glob/traversal/symlink/prefix 路径均失败。
4. 缺/多/换序事件字段、断 previous hash chain、重放已终结 attempt、terminal 后通用 runner/merge/push/reopen 均失败。
5. 成功路径必须覆盖 Luna owner 与 Terra owner，且所有终态均显式标记 `whole_project_acceptance_required=PENDING`。
