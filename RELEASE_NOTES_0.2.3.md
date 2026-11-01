## S1-04 工具权限与执行闭环补充（2026-10-24，同版本构建）

- Crew 批准后执行前，重新检查任务取消/Steering 与 MCP 实际权限/服务器身份；如果授权发生变化，不派发工具。
- MCP 自报的只读/幂等注解不再被视作执行安全证明；返回错误或执行中取消时记录不确定副作用，提醒核实后再重试。
- 阻止单批次重复工具调用 ID 造成重复派发。
- 保留 Tiga、Dyna、Crew 补丁与用户配置；测试结果 736 passed / 1 skipped / 52 subtests。
- 本轮不保证远程 MCP 调用可撤销，也不保证跨重启 exactly-once；Windows 真实环境验收尚未完成。

> 2026-10-21 UI follow-up: The Modes page has been restored to a single-screen Tiga layout. The Dyna preview cards are no longer appended underneath Tiga; Dyna remains reserved and disabled in the Mode menu. A future UI update can enable read-only Dyna previews in the same panel without enabling execution. This follow-up does not change the backend Dyna locks.

# Kiro BYO 0.2.3 — KiroCrew 原生兼容修复与 Dyna 预览

**兼容性基线**：重点按 KiroCrew `v0.10.0-insider.4` 的 `v0.10-adaptive` 补丁验证；已停止支持 Crew 0.9，当前包只包含 `v0.10-adaptive`。BYO 与官方 ACP 是平行执行后端，Crew 继续负责队友、日程、工作区、界面、权限和任务治理。

## 本次完成

1. **保留后端显式选择优先权。** 队友会话默认继承 BYO 的现有设计不变，但在 Crew 明确传入 `backend_override` 时不得抢占；空字符串代表官方 Kiro 后端也作为有效的显式选择。兼容升级已安装的 0.2.2 队友补丁。
2. **避免切换 Harness 误写旧会话。** `v0.10` 聊天 Slot 先读取正在运行会话的实际 ACP Provider 后端；活跃旧会话继续保留原后端及模型选择。只有无活动 Provider 的冷 Slot 才根据当前后端整理 Auto/Jev 与 BYO Type 状态。兼容升级已注入的旧版辅助函数。
3. **模型目录故障不再被伪装成健康的空列表。** Provider 配置读取失败，或有 Provider 但没有任何有效模型且也无 ACP 缓存可用时，Crew 模型接口返回 HTTP 503 并留下诊断日志；真正没有配置 Provider 的初始安装仍可返回空列表。已有明确配置的具体模型和有效 ACP 广告模型作为回退保留。
4. **Dyna 模式预置、但严格锁定。** 模式页面可查看 Flash（闪亮型）、Strong（强壮型）、Miracle（奇迹型）三种形态的开发中预览。Dyna 不可选、不进 Type 模型目录、不允许配置/路由/ACP 执行。禁止用自定义 JSON 覆盖 `dyna` 内置保留 ID 来解锁；Tiga 和其他正常自定义 Policy Pack 不受影响。
5. **管理界面小范围修正。** Dyna 预览置于 Tiga 详情后面；Modes 页面允许滚动，并保留窄屏可读性。未重写 Crew 全局界面，也没有修改用户本地的队友或日程数据。
6. **一致的版本标识。** App、Python 包、ACP Agent/worker 的版本号更新为 0.2.3，不更改稳定的 `kiro-byo` App/Backend ID。

## 0.2.2 → 0.2.3 升级步骤

1. 在 KiroCrew 中导入新 ZIP（或替换安装的 Kiro BYO App），保留既有用户 Provider 配置。
2. 打开 **Kiro BYO → Settings → KiroCrew → Install / Update integration**，执行集成升级。只替换 BYO 自有集成补丁；按正常流程生成/校验备份。
3. 关闭并**完全重启 KiroCrew**。确认集成状态和 App 版本为 0.2.3。
4. 检查 `Settings → Agent Harness → Kiro BYO`；在新会话中验证模型目录、模型选择、队友、停止/恢复和日程执行。切回官方 ACP 时再核对原生模型和队友配置。

不需要清除 Crew 原生队友、日程、工作区或会话数据库；发生异常先保留日志并使用原有的集成回滚流程，不要手工删文件。

## 测试及限制

- 本地测试：`699 passed, 1 skipped, 47 subtests passed`；包含新增加的后端覆盖优先级、旧补丁升级、活跃 Slot 归属、目录故障和 Dyna 锁定测试。JavaScript 通过 `node --check`。
- **尚未完成**真实 Windows KiroCrew 桌面全功能端到端验收，也没有完成官方 Saved Agent 全能力契约，更不能承诺 Crew 所有功能已 100% 对等。
- 保留了 KiroCrew 的全局模型展示兼容层；尚未从根本上移除对前端 DOM 的版本依赖。后续需在真实 0.10 桌面版进一步验证 Chat、队友、自定义 Agent 和定时任务的模型选择界面。
- **Dyna 仅为预览**，任何形态均不能用于生成或调度；JSON 中的路由参数只是未来策略结构的占位，并非已经实现的执行逻辑。


## 0.2.3 同版本 UI 排版修复（2026-10-21）

- 「系统总览 → 开始使用」三张引导卡使用紧凑内容高度，按钮不再被撑到页面底端。
- 随内嵌可用宽度切换三列 / 两列 / 一列，系统状态卡不再跟随左侧空白区域强行拉伸。
- 未修改 Provider、ACP、队友、任务、权限、Tiga/Dyna 执行逻辑，也未修改 Crew 集成补丁。
- **版本仍为 0.2.3**（App Manifest、Python 包、Agent 版本一致），这是同版本界面热修包。
- 使用同版本重新导入时，客户端可能缓存旧界面；若修改未显示，请退出 BYO 页面、完全重启 KiroCrew，必要时在备份配置后按 App 管理流程重新加载同版本包。**不需要因 UI 修复重新注入 Crew 集成**。
- 已通过模拟 Crew 双侧栏的 Chromium 布局测试；真实 Windows `v0.10.0-insider.4` WebView 尚待本机验证。

### S1-02 Steering reliability update (same 0.2.3 version)

- Duplicate `_session/steer` messages with the same `messageId` are de-duplicated; mismatched reuse is rejected.
- `steering_queued` and `steering_injected` carry correlated `messageId` metadata. Unconsumed messages are explicitly cleared if a turn terminates.
- Late steering after a final response candidate is not acknowledged as deliverable. No change to Tiga, Dyna or the Browser S1-01 integration.
- Requires Windows E2E checks for real model consumption, permission waits, cancellation and Crew queued fallback.

## S2-03 Addendum (2026-10-24)

Saved Agent tools omitted by Crew now fail closed; native execution re-checks Saved Agent visibility; session restore does not revive withdrawn Agent when current Crew metadata exists. No changes to Tiga/Dyna. See `S2_03_TOOL_SCOPE_REPORT.md`.
