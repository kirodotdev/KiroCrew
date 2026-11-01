# Kiro BYO 0.2.3 · 统一后端联动管理 — 第一阶段候选

日期：2026-10-25。基线：用户提供的 `Global Audit Fix 03`。候选版本保留 SemVer `0.2.3`，由新的 BUILD_INFO runtime fingerprint 区分构建；未覆盖原始包。

## 已实现

1. **任务 UI 新增统一状态中心**：显示 Crew 主默认 ACP、统一队友后端、Cron（依每任务/会话解析）、后台 AI 模型和 Subagent 模型；补充 Heartbeat、TaskRunner/工作流以及运行中会话的归属边界。后端未取得精确信息时显示 Crew 决定/未知，不凭全局主配置捏造实际执行状态。
2. **明确的队友同步操作**：用户在 UI 确认后，可把 `agent.member_acp_backend` 与当前主默认后端对齐。仅更改 Crew 原生这一项设置；不写队友数据库，不重建队友，不停止旧会话，不开启后台自动同步。会验证 Crew 当前后端、队友旧值、`ACP_BACKENDS_MEMBER_DISPATCH` 注册情况，防止过时界面覆盖最新选择；对已知 `config.local.json` 更高优先级覆盖写前拒绝。
3. **后台模型 Auto 语义修复**：同时返回保存的 `role_models.background` 原值与 `resolve_model("background")` 后的模型。合法 Auto 继承不再因字符串不同错误报错；写入后返回是否受其他未知高优先级配置覆盖的信息。
4. **跨后端模型写入保护**：在非 BYO 主 Harness 下拒绝新增 BYO 专属的背景/Cron 模型固定值；允许清空旧的固定选择。原有非 BYO 模型 ID 仍显示并保留，不会被悄悄替换。
5. **Script / Command Cron 分类**：这两类零模型任务不再展示无意义的 ACP 模型/上下文选择；新增模型固定请求被拒绝，已有 Agent Cron 不受影响。
6. **UI 多语言与 CI**：新增 zh-CN、zh-TW、en、ja、ko 翻译；新增 Mac + Linux + Windows 的联动专项测试矩阵，并把已有进程生命周期矩阵覆盖到 macOS。

## 特别注意：没有宣称完成的功能

- **尚未实现“主后端变化后自动同步全部任务”开关**。本阶段队友同步是用户点击、确认后的单次写入；官网 Crew 的双配置选择规则仍归宿主管理。没有新增 Crew 启动时 `.pth` 改写或旁路轮询，也没有强制拦截 `select_provider_backend()`。
- **Cron / TaskRunner / Heartbeat / Subagent 不能仅靠普通会话后端确定实际 Harness**。本阶段 UI 标明边界，并未写入伪造的 backend override 或重写任务记录；BYO 模型固定目前只能确定主默认后端选中 BYO，不能证明每个已有任务会话的真实后端。要实现完全自动联动必须先得到宿主的可验证 runtime/backend provenance API。
- **没有声明 BYO 完整 `ACP_BACKENDS_MEMBER_CAPABILITIES`**；Saved Agent 完整规格和队友独立 model pin 仍需宿主能力契约验收。
- **没有获取 insider.5 完整上游源码进行集成安装验证**；BUILD_INFO 保持该来源合同 `pending_ci_checkout`。不提供 macOS/Windows/Linux 真实 Crew E2E 成功保证。

## 回归与平台矩阵

- 单测通过：新增 `tests/test_crew_linkage.py` 覆盖默认状态、成员注册能力、显式同步、过期配置、已知 local override、Auto 继承、非 BYO 模型拒绝、Script/Command Cron。
- 本次最后按两批完整运行 `tests/test_*.py`，结果 **849 passed、1 skipped、52 subtests passed**（519 + 330）；`node --check ui/index.mjs` 和 `scripts/release_check.py` 通过。该结果来自当前 Linux 容器环境，不等于三平台真实 KiroCrew E2E。
- 新增 GitHub Actions `linkage-cross-platform`：Python 3.11/3.12/3.13 × macOS/Linux/Windows；`process-model-cross-platform` 现在覆盖 macOS。**配置 CI 不代表这些系统的任务已经实际在 CI 跑完**。

## 后续第二阶段

1. 获取目标 KiroCrew tag 的完整源码并执行 `scripts/verify_crew10_integration.py`，对于不支持的源码形状继续 fail-closed。
2. 引入 Crew 官方可验证的每任务/会话 **configured backend、runtime backend、model pin、explicit override、session status** 只读来源。
3. 在宿主可验证 API 基础上实现“跟随/固定”策略预览、冲突检测、明确确认和回滚。严格保持 explicit override > member/task config > main default，已有会话不热切。
4. Windows、macOS、Linux 分别验收队友分派、Agent Cron/Script Cron、TaskRunner、Heartbeat、MCP 权限、会话恢复、回切官方后端、卸载回滚。

## 安装提示

此候选是 **App + 源码**，不是新的 KiroCrew 正式发行包。安装前备份 Crew 配置和数据，并检查安装集成页面的 host compatibility 状态。更新后可能需要按 Crew 提示重启；不需要删除 Crew 的队友、日程和记忆。当前没有设备级端到端验收，因此建议先在隔离的测试配置中验证，不要直接覆盖唯一生产安装。
