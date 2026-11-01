# S4-03 — Tiga 内置 Mode 插件迁移与行为兼容报告

- 日期：2026-10-24
- 基线：S4-02 Build `0.2.3+8b85f8ef3eb52e7e`
- 产品版本：`0.2.3`（保持原有 App ID、ACP 名称、Type Selection ID）
- KiroCrew 基线：`v0.10.0-insider.4`（`e9bfada`）
- 实机验证：**未完成**；本轮仅源码/模拟环境验证

## 本轮改动

1. 新增 `src/kiro_byo/modes/` 内置 Mode 插件声明：`contracts.py`、`tiga.py`、`dyna.py` 和显式可信注册表 `__init__.py`。每个 Mode 作为整体定义自己的身份、Type 列表、默认 Type、执行准入与策略资源路径。
2. `policy_packs._discover_policy_packs` 不再靠泛扫内置 Policy Pack 目录决定哪些内置模式有效，而是加载已注册的内置 Mode 策略资源并检验声明。声明与 JSON 中的 Mode ID / Type 列表 / 默认 Type / enabled 不一致时拒绝激活并提供诊断错误。
3. `mode_registry.ModePlugin` 保留旧的 `.pack` 接口，新增可选 `.declaration` 指向可信内置 Mode 声明。自定义 JSON Mode 不会注册可执行 Python 插件。
4. Tiga 四形态的 `tiga.json` **字节级不变**，Dyna 预置的 `dyna.json` **字节级不变**；模型评分、Agent Loop、Provider、工具/权限、Crew 集成补丁和 UI 均未修改。
5. 历史用户的 `policy_packs/tiga.json` 手工同名策略覆盖仍保留，避免破坏旧配置。S4-02 的受控管理 API 依然拒绝更新内置 Tiga/Dyna；历史手工覆盖与受控管理是不同路径。Dyna 同名用户文件持续被拒绝，以维持禁执行。

## 架构约定

```text
KiroCrew  -- ACP --> BYO Core（会话/Provider/工具/权限/执行）
                              |
                       Mode Registry
                              |
              +---------------+---------------+
              |                               |
        Tiga 内置插件                     Dyna 内置插件
       Sky/Multi/Power/Glitter          Flash/Strong/Miracle
         纯策略数据 + 模式声明             预览，禁止执行
```

**这是内置 Mode 的独立注册与数据插件化，不是第三方任意 Python 代码执行系统。** 所有 Mode 仍共享 BYO 的路由器与 Agent Loop；未引入 Jev。自定义 JSON Policy Pack 继续使用之前的热加载与生命周期接口。

## 等价性与安全边界

- 冻结 Tiga Policy SHA-256：`0c09ad73ea883b38ae177afb4b0bc88a8915cf4e95b85f1b88836ed0bb743969`
- 冻结 Dyna Policy SHA-256：`a57c7ff9f61c22f073cb8435fc593ffe64aa6d3896225ec0c592d8a1986636aa`
- 固定 Tiga Type ID：`sky`, `multi`, `power`, `glitter`；默认 `multi`。
- 固定 Dyna Type ID：`flash`, `strong`, `miracle`；`enabled=false`，任何 Type 无法执行。
- Crew 既有 `kiro-byo-type:tiga:<type-id>` 保持字面不变，策略目录、旧 API 返回结构保持兼容。
- 不扩展任何 MCP、浏览器、文件、终端工具权限；不复制 Crew 的队友、日程、任务数据。

## 已通过测试

- `pytest -q --disable-warnings`：**798 passed / 1 skipped / 52 subtests passed**（单次完整 Python 回归）。
- 新增 `tests/test_s4_03_tiga_plugin.py`：涵盖内置插件注册、两份策略文件的字节指纹、四形态执行、默认/禁用状态、Type Selection ID、热加载、不导入用户 Python、手工历史覆盖兼容与不匹配声明 fail-closed。
- 本轮确认代码层面的兼容性；真实 Crew 模型/队友/日程、Windows 热切换和策略运行时间性能尚未作 E2E 证明。

## 后续

- S5：Dyna 在同一模式区域切换到三形态只读预览，仍保持不可执行，不让页面因额外预览出现纵向滚动。
- 按既定计划集中验证 Windows 原生 Harness Parity（浏览器/Steering/队友/日程/后台任务/权限）。
- 若未来希望开放“完全独立安装的第三方代码插件”，必须另立隔离、签名和安全审计方案；不应在本阶段自动允许任意插件代码。
