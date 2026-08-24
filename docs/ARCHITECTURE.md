# Codex Team 架构说明

Codex Team 是一个本地、可恢复、可审计的半自动编排层。它把“谁可以做什么”固化成配置、Schema、运行时证据和人工闸门，而不是让模型自由解释路由规则。

## 1. 执行面

| 执行面 | 默认用途 | 身份约束 | 权限边界 |
|---|---|---|---|
| `NATIVE_SUBAGENT` | Luna Max 默认路径 | `role=luna`、`gpt-5.6-luna/max`、`agent_type=null`、native agent/thread UUID | 由冻结 envelope 决定；通常只读或有界写 |
| `CODEX_EXEC_ROLE_CONTRACT` | 明确授权的独立 `codex exec` 会话 | 独立记录，不能冒充原生子代理 | 由任务信封和 assignment capability 决定 |

原生身份必须由控制器签发并由运行时证据闭环证明。模型、推理档、执行面、权限、沙箱、cwd 或 UUID 缺失/冲突时，流程 fail-closed；自定义 Agent 名称不能补齐证据。

## 2. 默认角色

| 角色 | 负责什么 | 明确不负责什么 |
|---|---|---|
| Luna Max | 冻结 envelope 内的机械 coding、确定性检查、证据抽取、分发同步 | planning、review、语义仲裁、final acceptance |
| Terra xhigh | 复杂施工、调试、集成、开放式问题拆解 | merge、push、自我验收 |
| Sol medium | 所有工程小节完成后的集中、只读、对抗式 final acceptance | 普通 construction、常驻 planning |
| Sol xhigh | owner-authorized planning；Sol-medium 梯级失败后的 terminal repair | 普通施工、绕过 final acceptance |

Terra medium 和 Sol high 没有默认角色。角色名称、模型和推理档是独立字段，不能用“同名路径”或调用者自报替代运行时身份。

## 3. 生命周期

### 正常路径

```text
用户目标
  → task envelope / Schema 校验
  → Luna 事实盘点或有界施工（如获授权）
  → Terra 复杂施工（需要时）
  → 各工程小节完成自检
  → 固定 candidate commit
  → Sol medium 集中 final acceptance
  → 人工 owner decision
```

中间工程小节不再单独派发对抗式审查，但施工 owner 仍必须执行冻结信封内的目标测试、负向检查、范围核对和运行时证据门。这里的自检不能被写成“独立验收”。

### Final acceptance 返工路径

```text
Sol medium REWORK
  → 人工批准冻结 findings / paths / commands
  → 不同身份的 Sol-medium fixer 有界返工
  → 另一不同身份的 Sol-medium read-only recheck
  → 再次 REWORK 才可 owner-authorize Sol-xhigh terminal repair
```

Sol-xhigh terminal repair 是一次性的例外，不自动启动，也不增加普通 Sol-xhigh construction 权限。

## 4. Codex Team 入口

工具名称是 `codex team`，消息 grammar 是：

```text
team call <objective>
team call: <objective>
team call：<objective>
```

入口只识别消息开头的固定指令，并把目标分成四种 disposition：

- `DIRECT_L0`：控制器执行固定 allowlist argv，不调用模型；
- `DIRECT_L1`：Luna 只读抽取一个安全的仓库相对文件；
- `PLAN_REQUIRED`：回到需要人工 owner gate 的完整规划流程；
- `BLOCKED`：输入、锁、权限或执行证据不满足要求。

Team Call 不授予 Luna review、approval、construction 或 final acceptance 权限，也不自动 merge、push。

## 5. 证据与状态

所有任务都围绕固定 `base_commit`、`candidate_commit` 和授权文件集合运行。关键证据包括：

- task / route / plan / result 的严格 Schema；
- runtime evidence（模型、推理档、执行面、sandbox、permission、cwd、native UUID）；
- cost evidence（实测、投影和 unavailable 明确区分）；
- append-only events、human decisions 和 assignment capability；
- 真实 diff、工作树、Git 控制面和测试输出。

状态机遇到 HEAD 漂移、只读角色写入、范围越界、重复 attempt、证据缺失或非法跳转时停止并记录 `BLOCKED`，不依赖模型解释来“继续”。

## 6. 安全边界

- 默认不自动 merge、push、删除 worktree 或修改全局配置；
- 写入必须在具名、隔离的 worktree 和冻结路径内进行；
- 不把项目密钥传给子进程，日志不记录环境变量和完整原始数据；
- 原生 Luna 不依赖仓库或 Plugin 中的自定义 Agent 模板；
- 历史 Agent 清理工具只做 cleanup-only 迁移，不创建缺失模板；
- 清理竞态无法安全证明最终 inode 时保留私有 deferred quarantine，不删除 replacement。

## 7. 代码地图

```text
config/                         # 任务、路由、计划、结果、运行时与成本 Schema
scripts/ai_workflow.py          # 主 CLI、任务状态机、Team Call 生产入口
scripts/ai_workflow_runtime.py  # 原生/exec 运行时身份与证据
scripts/ai_workflow_artifacts.py# 严格 artifact 校验和数据类
scripts/ai_workflow_routing.py  # Terra OS 闭集路由
scripts/ai_workflow_planning.py # 计划和施工信封
scripts/ai_workflow_repairs.py  # acceptance repair ledger v2
scripts/ai_workflow_team_call.py# Codex Team grammar、分类和收据
plugins/ai-workflow/             # 对外 Plugin；runtime/config 必须与根目录一致
tests/                           # 默认假 runner、负向注入和发布一致性测试
```

## 8. 当前限制

- 项目是 public preview，重点是自用和实验，不提供生产 SLA；
- 旧格式 native rollout 缺少 native UUID 时会安全拒绝；
- cleanup-only 的私有 quarantine 不自动 GC，需要后续设计身份安全的恢复/清理协议；
- 真实计费数据、模型服务可用性和 live rollout 仍必须由实际环境单独验证；
- Windows 原生生命周期不在当前验证范围内。
