# Kiro BYO 0.2.3 · Stage 9 工程报告（模式完整性 + 工具回执）

基线：Stage 8 完整交付包中的 `Kiro_BYO_0.2.3-source.zip`。只修改独立源码副本；未接触本机 Crew 安装和数据。

## 一、Mode / Type 实际审计

- **Tiga 内置 Mode**：已注册并启用，Sky / Multi / Power / Glitter 四个 Type 和默认 Multi 全部通过实际 Registry/Policy 解析测试；固定 Type Selection ID 保持兼容。`tiga.json` 的 SHA-256 仍为 `0c09ad73ea883b38ae177afb4b0bc88a8915cf4e95b85f1b88836ed0bb743969`，与 S4-03 的冻结基线一致。
- **Dyna 内置 Mode**：已注册，Flash / Strong / Miracle 为预览 Type，整个 Mode `enabled=false`。运行时选择 Dyna 被拒绝，不是可用的新模式。`dyna.json` SHA-256 `a57c7ff9f61c22f073cb8435fc593ffe64aa6d3896225ec0c592d8a1986636aa`，未改动。
- **插件化程度**：共享 Harness + Mode Registry + 内置可信声明 + 数据型 Policy Pack。四形态是 Tiga JSON 内的四项独立策略，**不是四个可独立安装或卸载的执行代码插件**。
- **热加载/热插拔**：用户自定义 JSON Mode 能被下次 Registry 读取看到，支持受控版本升级、启停与单步回滚；内置 Tiga/Dyna 不能通过自定义 Mode 管理器升级或卸载。没有 DSH/Cordis 的执行代码热替换或插件 Fiber 生命周期。当前会话下一次是否采用新策略取决于实际会话选择/路由，非立刻中断正在运行的工具回合。
- **“升级成功”的证据等级**：S4-01/02/03 的静态源码/Registry/Mode Type 单元测试通过；策略文件维持原始哈希，无法据此证明三个 OS 的真实 KiroCrew 页面和持续会话运行已经完成端到端验收。

## 二、Stage 9 实际修复

1. **自定义 Mode 假阳性回滚**：`mode_lifecycle_status` 原先只验证回滚记录中的 `after_sha256`，即使 previous.json 丢失/损坏仍报告 `rollback_available=true`。新增先失败的测试，现会检查 journal 版本、前态存在性、前态 SHA 和可解析性；丢失或污染时显示冲突，不冒充可回滚。
2. **明确插件能力**：只读 Mode 目录增加 `hot_reload_scope=custom-json-policy-only`、`executable_hot_swap=false` 和每个 Mode 的 `policy_hot_reload` / `managed_upgrades` / `code_hot_swap`。保留旧 `hot_reload` 字段兼容已有 UI。
3. **新增模式审计命令**：`python scripts/audit_mode_plugins.py`，只读验证内置声明、可用性、Type ID、默认 Type、策略文件 SHA，并检测历史用户 Tiga 同名覆盖。此命令不导入用户 Python，也不改变 Crew 配置。
4. **后工具回执失败不再继续**：在工具批次执行之后的 `tool_batch_completed` 持久化回执改为关键边界。如果保存失败，立即停止本轮继续推理，明确报错，并保留外部操作副作用未知/需人工核验，不自动重放。
5. **跨平台 CI 专项入口**：增设 Windows/macOS/Linux + Python 3.11/3.12/3.13 的 Mode Integrity、回滚、工具回执测试组合；未在远程 CI 实际运行。

## 三、回归测试

- 新增 Mode 测试先复现三个失败场景，修复后通过。
- Linux 源码完整测试按文件分两批：A **552 passed, 1 skipped, 22 subtests passed**（退出 0），B **351 passed, 30 subtests passed**（退出 0）。合计 **903 passed, 1 skipped, 52 subtests passed**。
- 单次全量测试超过执行时限，不能宣称单次全量干净通过。分批执行的全套测试均通过。
- `python -m compileall -q src backend scripts tests`、`node --check ui/index.mjs`、发布检查通过。

## 四、架构及安全边界

- 仍没有 DSH 式可执行 Python 插件的动态安装/热卸载；未来要引入，必须设计可信签名、版本依赖、安全权限、隔离进程和会话引用生命周期。
- Dyna 不因这次审计而启用。Tiga 四 Type 路由 JSON 完全未修改，Auto/Jev 选择器、Crew 队友/日程、Stage 3 联动未主动改动。
- HTTP/MCP 远端操作若已产生副作用，BYO 的本地回执不提供外部 exactly-once 保证。安全恢复应优先查询外部状态，未知时人工核实。
- Crew v0.10.0-insider.5 上游真实版本契约，以及 Windows/macOS/Linux 的真实桌面安装/升级/卸载、会话重启/热更新 E2E 都未验收。

## 五、Stage 8 -> Stage 9 源文件差异

新增：
- scripts/audit_mode_plugins.py
- tests/test_stage9_mode_integrity.py
- tests/test_stage9_tool_receipt_failure.py

修改：
- .github/workflows/ci.yml
- scripts/build_release.py
- src/kiro_byo/mode_lifecycle.py
- src/kiro_byo/policy_packs.py
- src/kiro_byo/turn_runner.py

删除：无。
