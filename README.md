# Codex Team

一个面向 Codex 的、可审计的半自动多模型协作工作流。它用任务信封、确定性路由、运行时身份和人工闸门，把规划、施工、证据收集、最终验收与返工连接起来。

| 项目状态 | 当前值 |
|---|---|
| Plugin 版本 | `0.2.0` |
| 发布形态 | Public preview；自用优先，不承诺生产 SLA |
| 默认 Luna 执行面 | 原生 `NATIVE_SUBAGENT`：`gpt-5.6-luna / max` |
| 默认施工 OS | Terra xhigh |
| 最终验收 | Sol medium 集中、只读、对抗式验收 |
| 最近更新 | 2026-08-26 |
| 许可证 | 尚未声明；公开可见不等于授予再分发许可 |

## 这是什么

Codex Team 不是常驻服务，也不是自动替用户做最终决定的项目经理。它是一组标准库实现、Schema、Plugin 镜像、CLI 和测试，用来把多模型协作规则固定成可检查的工程契约。

核心分工：

| 角色 | 主要职责 |
|---|---|
| Luna Max | 冻结 envelope 内的机械 coding、确定性检查、证据抽取和分发同步 |
| Terra xhigh | 复杂施工、调试、集成和开放式问题拆解 |
| Sol medium | 全部工程小节完成后的最终整体验收；验收失败时执行一次有界返工梯级 |
| Sol xhigh | owner-authorized 规划，或返工梯级失败后的终局升级 |

Luna 不承担 planning、review、approval 或 final acceptance；Terra 不合并、不推送、不自验；Sol xhigh 不自动启动。未明确列入配置的模型和档位不会被隐式注入流程。

## 快速开始

要求：Python 3.11+、Git、POSIX shell；Plugin verifier 需要 `jq`。项目不增加第三方 Python 依赖。

```sh
git clone https://github.com/New2taste/codex-team.git codex-team
cd codex-team

python3.11 -m unittest discover -s tests
sh plugins/ai-workflow/scripts/verify.sh
```

可选的 Skill 检查（需要本机已安装 Codex skill-creator）：

```sh
python3 "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" \
  plugins/ai-workflow/skills/orchestration
```

## Codex Team 调用

工具名称是 `codex team`，最简单的消息指令是 `team call`。只解析消息开头的三种形式：

```text
team call <objective>
team call: <objective>
team call：<objective>
```

示例：

```sh
codex team "team call 检查当前工作区状态"
codex team "team call 核对文件 README.md"
```

仓库内的等价测试入口：

```sh
python3 scripts/ai_workflow.py team-call \
  "team call 检查当前工作区状态" \
  --repository-root "$PWD"
```

Codex Team 只有四种受限 disposition：

- `DIRECT_L0`：控制器执行固定 allowlist argv，不调用模型；
- `DIRECT_L1`：Luna 只读抽取一个安全的仓库相对文件；
- `PLAN_REQUIRED`：回到需要人工 owner gate 的规划流程；
- `BLOCKED`：输入、锁、权限或执行证据不满足要求。

它不自动修改、合并、推送或替代最终整体验收。失败收据以退出码 `2` 返回，并保留 append-only 账本。

## 工作流

### 1. 规划

```text
目标 → 任务信封校验 → Luna 事实盘点 → 证据校验
→ Terra xhigh 只读成案 → owner decision
```

### 2. 施工与验收

```text
冻结 envelope → 有界施工 → 目标测试/负向检查/范围核对
→ 全部工程小节完成 → 固定 candidate commit
→ Sol medium final acceptance → owner decision
```

中间工程小节采用 `section_self_check_only`：施工 owner 必须完成信封内的测试、负向检查、范围核对和运行时证据门，但不再逐小节派发独立对抗式审查。自检不等于验收。

### 3. 验收后返工

```text
Sol medium REWORK
→ 人工批准冻结 findings / paths / commands
→ different Sol-medium fixer 有界返工
→ different Sol-medium read-only recheck
→ 仍 REWORK 才可 owner-authorized Sol-xhigh terminal repair
```

返工不能扩大 candidate、允许路径或验证命令；Sol xhigh 的 terminal repair 是一次性例外，不产生普通常驻施工权限。

## 身份与证据

默认 Luna 使用 `NATIVE_SUBAGENT`，运行时必须同时证明：

- workflow role：`luna`；
- model / reasoning effort：`gpt-5.6-luna / max`；
- `agent_type=null`；
- native agent UUID、thread UUID、sandbox、permission 和 cwd；
- 受控调度参数与运行时 rollout 证据一致。

`CODEX_EXEC_ROLE_CONTRACT` 是另一种独立执行面，不能冒充原生 Luna。任一身份、权限、线程、模型或档位证据缺失/冲突时，流程 fail-closed。

每项任务围绕固定 `base_commit`、`candidate_commit`、授权文件集合和验证命令运行。状态、结果、成本、运行时证据、人工决策和返工事件均使用严格 Schema 或 append-only ledger 保存。

## 安全边界

- 写入必须在具名隔离 worktree 和冻结路径内进行；
- 只读角色产生文件变化、HEAD 漂移、范围越界或证据缺失时立即 `BLOCKED`；
- 不把项目密钥传给子进程，日志不记录环境变量和完整原始数据；
- 默认不自动 merge、push、删除 worktree 或修改全局配置；
- 任务范围、运行时身份和证据不一致时立即停止，不依赖模型自行解释。

## 目录结构

```text
config/                         # 任务、路由、计划、结果、运行时、成本 Schema
scripts/ai_workflow.py          # 主 CLI、状态机和 Codex Team 生产入口
scripts/ai_workflow_runtime.py  # native/exec 身份与 runtime evidence
scripts/ai_workflow_artifacts.py# 严格 artifact 校验和数据类
scripts/ai_workflow_routing.py  # Terra OS 闭集路由
scripts/ai_workflow_planning.py # 计划和施工 envelope
scripts/ai_workflow_repairs.py  # acceptance repair ledger v2
scripts/ai_workflow_team_call.py# Codex Team grammar、分类和收据
plugins/ai-workflow/             # 对外 Plugin；runtime/config 与根目录同步
tests/                           # fake runner、负向注入和发布一致性测试
```

CLI 命令：`new`、`validate`、`team-call`、`run`、`route`、`status`、`decide`、`report`。

## 验证与开发

```sh
python3.11 -m unittest discover -s tests
python3.11 -m compileall -q config scripts tests plugins/ai-workflow/runtime plugins/ai-workflow/scripts
sh plugins/ai-workflow/scripts/verify.sh
for script in plugins/ai-workflow/scripts/*.sh; do sh -n "$script"; done
git diff --check
```

根目录和 Plugin 的 runtime/config/schema 必须通过字节一致性检查。发布前还要在临时副本中篡改一个镜像文件，确认 verifier 返回非零。

## 当前限制

- public preview，不提供生产 SLA；
- 真实 live rollout、模型服务可用性和计费数据需要在实际 Codex 环境中单独验证；
- Windows 原生生命周期不在当前验证范围内。

## 文档

- [架构说明](docs/ARCHITECTURE.md)
- [开发与验证指南](CONTRIBUTING.md)
- [变更记录](CHANGELOG.md)
