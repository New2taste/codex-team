# Native Luna Max Default Design

> 状态：`APPROVED_FOR_IMPLEMENTATION`
> 日期：2026-08-17

## 目标

将 Workflow 的默认 Luna 调用从仓库维护的自定义 `luna_max` Agent，切换为主控直接
派发的原生子代理，并用运行时模型与推理档验证它确实是 `gpt-5.6-luna / max`。

## 方案

- `luna` 继续作为 Workflow 角色名；`execution_surface=NATIVE_SUBAGENT` 表示主控
  派发的原生子代理。
- 原生身份不再要求 `agent_type=luna_max`。身份由受控调度参数、原生线程/代理 UUID、
  `model=gpt-5.6-luna`、`reasoning_effort=max`、sandbox、permission 和 cwd 共同证明。
- 任一模型、档位、权限或线程证据缺失/冲突，运行立即 `BLOCKED`；不得用自定义名字补齐。
- `CODEX_EXEC_ROLE_CONTRACT` 仍可用于自动化独立会话，但必须单独记录，不能冒充原生子代理。

## 清理范围

- 删除仓库中的默认自定义模板 `.codex/agents/luna-max.toml` 和 Plugin 镜像。
- 从默认 preflight、Skill、README、Plugin verifier 和 Agent metadata 中移除安装/调用自定义
  Agent 的要求。
- 保留一次性 migration/uninstall 工具，允许已有用户清理历史 `luna_worker`/`luna-max`
  安装；它不再拥有当前模板，也不参与默认路由。用户目录经盘点为空，不执行用户目录删除。

## 验证

- 先新增失败测试：原生证据允许 `observed_agent_type=null`，但缺 model/effort 或错误
  model/effort 必须失败；自定义模板和默认安装器入口必须不存在。
- 更新 root/Plugin runtime、schema、docs、verifier 和 fixtures 后，运行 focused runtime、
  distribution 与完整 unittest、compileall、Plugin verifier、Skill validator 和 diff 检查。
