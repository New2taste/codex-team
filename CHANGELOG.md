# Changelog

## 0.3.0 — 2026-08-26

调度与效率升级，公开项目名称统一为 **Codex Team**。

### Added

- 零模型 scheduler control plane：批次调度、结果收据、集中终验 child、resume 和 abort；
- optimization shadow route advice、成本门禁和双钥匙 compact prompt 投影，默认不改变 effective route；
- 固定 root-to-Plugin 同步器与 `scripts/verify_all.sh` 零模型完整验证入口；
- scheduler、resume/abort、optimization、全工程终验和分发同步测试。

### Changed

- README、架构说明和 Skill 入口收束为当前运行时契约；
- Plugin 版本升级为 `0.3.0`，GitHub 仓库地址统一为 `New2taste/codex-team`；
- 中间工程小节继续只做施工自检，集中终验仍由 Sol medium 执行。

### Verification snapshot

- `sh scripts/verify_all.sh`：529 tests passed，8 个既有 repair-ledger-v1 intentional skips；
- root/Plugin parity、Plugin verifier、Skill validator、compileall、shell syntax 和 `git diff --check` 均通过。

### Known limitations

- public preview，不提供生产 SLA；
- 真实 live rollout、模型服务可用性和计费数据需要在实际 Codex 环境中单独验证。

## 0.2.0 — 2026-08-24

当前公开预览基线。

### Added

- 原生 Luna Max 默认执行面：严格验证 `gpt-5.6-luna / max`、`NATIVE_SUBAGENT` 和 native agent/thread UUID；
- Codex Team 受限入口及 `team call` grammar，包含 L0/L1/plan fallback、单活跃收据锁和 append-only ledger；
- Terra OS 闭集路由、Sol medium final acceptance 和失败后的有界 Sol-medium rework ladder；
- 运行时身份、成本证据、计划、路由、结果和 acceptance repair ledger 的严格 Schema 与测试；
- 根目录到 Plugin 的 runtime/config/schema 字节一致性验证和篡改负向检查。

### Changed

- 中间工程小节采用 section self-check，不再逐小节派发独立对抗式审查；
- final acceptance 失败时，返工优先由不同身份的 Sol medium 处理，再由另一 Sol medium 只读复核。

### Verification snapshot

- Full unittest：359 tests passed，8 个既有 repair-ledger-v1 intentional skips；
- Distribution suite：30 tests passed；
- Plugin verifier、Skill validator、compileall、shell syntax、runtime/config parity 和 `git diff --check` 均通过。

### Known limitations

- public preview，不提供生产 SLA；
- 真实 live rollout 和计费数据需要在实际 Codex 环境中单独验证。
