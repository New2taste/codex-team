# Changelog

## 0.2.0 — 2026-08-24

当前公开预览基线。

### Added

- 原生 Luna Max 默认执行面：严格验证 `gpt-5.6-luna / max`、`NATIVE_SUBAGENT` 和 native agent/thread UUID；
- Codex Team 受限入口及 `team call` grammar，包含 L0/L1/plan fallback、单活跃收据锁和 append-only ledger；
- Terra OS 闭集路由、Sol medium final acceptance 和失败后的有界 Sol-medium rework ladder；
- 运行时身份、成本证据、计划、路由、结果和 acceptance repair ledger 的严格 Schema 与测试；
- 根目录到 Plugin 的 runtime/config/schema 字节一致性验证和篡改负向检查。

### Changed

- 移除仓库和 Plugin 中的 active `luna-max.toml` / `luna-worker.toml`；
- 历史 Agent install/uninstall 改为 cleanup-only，不再创建缺失模板；
- 中间工程小节采用 section self-check，不再逐小节派发独立对抗式审查；
- final acceptance 失败时，返工优先由不同身份的 Sol medium 处理，再由另一 Sol medium 只读复核。

### Verification snapshot

- Full unittest：357 tests passed，8 个既有 repair-ledger-v1 intentional skips；
- Distribution suite：29 tests passed；
- Plugin verifier、Skill validator、compileall、shell syntax、runtime/config parity 和 `git diff --check` 均通过。

### Known limitations

- public preview，不提供生产 SLA；
- 旧 native rollout 缺少 UUID 时会 fail-closed；
- cleanup-only 的私有 deferred quarantine 暂不自动 GC；
- 真实 live rollout 和计费数据需要在实际 Codex 环境中单独验证。
