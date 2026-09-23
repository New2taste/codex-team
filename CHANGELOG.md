# Changelog

## Unreleased — 2026-09-23

默认模型执行面升级：Terra OS 施工角色现绑定 `gpt-6-sol / medium`，原 Sol medium 规划监督与集中终验角色现绑定 `gpt-6-sol / xhigh`，原生 Luna 工具角色升级至 `gpt-6-luna / max`。职责、权限、Astra 角色和兼容用的角色 ID 保持不变；历史账本与研究样本继续保留其实际观测到的旧模型身份。

### Changed

- 同步主配置、运行时身份校验、最终验收收据、探针闭集和 Plugin 分发镜像；
- README 中英文首页、架构说明与编排 Skill 改为展示当前模型绑定；
- 不改变任务路由、返工次数、owner gate、Astra 规划/复验/终局修复职责或 Luna max 推理档。

## Unreleased — 2026-09-07

本轮将 Astra medium（`gpt-6-astra / medium`）设为 owner-authorized 总体规划、闭集裁定和终局修复角色；Terra xhigh 继续负责常驻施工，Luna max 继续负责有界低成本工具任务。Sol medium 仍负责集中最终验收和首轮有界修订；新增 Astra low（`astra_low_reviewer`，`gpt-6-astra / low`）对首轮修订执行独立、只读 peer 验收。

### Changed

- 更新默认角色说明、路由示意和编排 Skill，使 Astra medium 成为规划/裁定/终局修复的当前执行角色；
- 保留既有终局授权接口的兼容 ID，旧账本可重放但新运行时身份使用 Astra；
- 记录 Sol-medium 首轮修订 → Astra-low 独立复验 → owner-authorized Astra-medium 终局修复的验收梯级。

## Unreleased — 2026-08-29

本轮更新补强了 Sol medium 集中终验与返工链路的所有权控制面，并保持 Terra xhigh 常驻施工、Luna max 廉价工具进程的既定分工。

### Changed

- FrozenPlan 冻结时自动物化 task-hash-bound ownership registry，缺失或身份漂移时 fail closed；
- 唯一 final-acceptance child 在创建时即生成独立、不可变的所有权登记，明确 Sol medium 返工路径，避免返工启动后才发现登记缺失并留下孤儿 attempt；
- ownership registry 的写入和读取均重新绑定已存储 task 信封哈希，防止调用方伪造任务身份；
- root runtime、Plugin 镜像、配置清单和导入图登记保持同步。
- README 主页面直接展示中英双语项目介绍、角色边界表和默认路由 Mermaid 示意图；`README.en.md` 保留完整英文版。

### Verification snapshot

- `sh scripts/verify_all.sh`：1221 tests passed，8 个既有 repair-ledger-v1 intentional skips；
- root/Plugin parity、Plugin verifier、compileall、shell syntax、导入图和 `git diff --check` 均通过。

## 0.4.0 — 2026-08-26

运行时与常驻路由研究升级，公开项目名称继续统一为 **Codex Team**。

### Added

- live runtime 合同修复：完整 rollout 解析、受限身份归一化和安全诊断输出；
- provider-strict dispatch schema 投影，保持 `ai-result-1` canonical contract 不变；
- 独立、shadow-only 的 Luna/Sol/Terra 常驻路由探针，支持 dry-run、fake、live 双钥匙、配对分析和报告；探针脚本保持根目录研究工具定位，不进入 Plugin runtime 分发；
- Git 控制面只读快照比较，以及 router-probe manifest / cost evidence 契约。

### Changed

- 默认常驻入口分类建议记录为 Luna max；这只是项目说明，不改变确定性生产路由；
- README、架构说明、配置和 Plugin runtime/config 镜像同步到当前升级；
- Plugin 版本升级为 `0.4.0`。

### Verification snapshot

- `sh scripts/verify_all.sh`：585 tests passed，8 个既有 repair-ledger-v1 intentional skips；
- root/Plugin parity、runtime inspector、router probe、scheduler、repair 和分发测试纳入同一验证入口。

### Known limitations

- 常驻路由研究仍为 shadow-only；未完成实测前不宣称真实成本赢家；
- live rollout、模型服务可用性和计费数据需要在实际 Codex 环境中单独验证。

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

- `sh scripts/verify_all.sh`：532 tests passed，8 个既有 repair-ledger-v1 intentional skips；
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
