# 开发与验证指南

Codex Team 当前以个人自用和公开预览为主。贡献应保持契约优先、证据优先，并避免把本地运行状态或用户目录内容提交到仓库。

## 环境

- Python 3.11 或更高版本；
- Git；
- POSIX shell；
- Plugin verifier 需要 `jq`；
- 不增加第三方 Python 依赖。

## 修改规则

1. 先读相关 Schema、README、`plugins/ai-workflow/skills/orchestration/SKILL.md` 和现有测试。
2. 代码变更必须有对应测试；安全边界或身份契约变更必须包含负向测试。
3. 根目录 `scripts/`、`config/` 是权威实现；Plugin 的 `runtime/`、`config/` 必须字节一致。
4. 不提交 `data/state/ai-workflow/`、Team Call 用户 state、`__pycache__`、真实凭据或 rollout 原始敏感数据。
5. 不在普通施工任务中自动 merge、push、删除 worktree 或修改全局 Agent 配置。
6. 改动超出冻结任务 envelope 时先停下并回交，不把相邻发现夹带进提交。

## 本地验证

```sh
python3.11 -m unittest discover -s tests
python3.11 -m compileall -q config scripts tests plugins/ai-workflow/runtime plugins/ai-workflow/scripts
sh plugins/ai-workflow/scripts/verify.sh
for script in plugins/ai-workflow/scripts/*.sh; do sh -n "$script"; done
git diff --check
```

如果本机安装了 Codex skill-creator，再运行：

```sh
python3 "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" \
  plugins/ai-workflow/skills/orchestration
```

发布前还要确认每个根目录/Plugin runtime 与 schema copy 都通过 `cmp -s`，并在临时副本中篡改一个镜像文件，验证 Plugin verifier 会返回非零。

## 文档与提交

- README 面向第一次使用者，架构和边界细节放在 `docs/ARCHITECTURE.md`；
- 新的行为契约要同步 README、Skill、manifest（如适用）和测试；
- 提交信息使用 Conventional Commits，例如 `feat(workflow): ...` 或 `fix(runtime): ...`；
- 推送前重新运行完整测试，不依赖其他 worktree 或之前的测试输出；
- 发布、合并和推送是 owner 决策，不由 Codex Team 自动执行。
