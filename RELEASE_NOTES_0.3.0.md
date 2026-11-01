# Kiro BYO 0.3.0 — Dyna 三形态

- Dyna Mode 已从仅预览升级为可执行候选；顺序 Miracle → Flash → Strong，默认仍为 Flash。
- Tiga / Dyna 同一 Mode 页面按活动模式切换，历史使用量按 Mode+Type 独立过滤。
- 三种 Dyna Type 共用 BYO 执行、工具权限、Provider、ACP 和 Durable Run；不新增第二个 Harness。
- 内置 Tiga 的策略文件未修改。自定义策略不能冒充 Dyna ID。
- 新版升级需要重新加载 Crew 模型列表；未完成 Windows 0.11.0-rc.5 实机认证。

## 0.3.0 修复构建说明（2026-10-31）

- Dyna 模式切换后，冷会话旧 Tiga Type 将迁移为 Dyna 默认 Flash；Tiga 默认仍为 Multi，手动当前 Mode Type 不变。活跃会话继续使用其原有执行策略。
- 增加 BYO App → KiroCrew 模型目录的跨界面刷新通知及宿主冷会话协调。Windows 前端 React Query 端到端行为仍未实机确认，如出现旧菜单需保存记录并复核，不应视为完成稳定版验收。
- 0.11.0 正式版仅获源码契约候选准入，不代表已验收，后续 0.12 继续默认拒绝。
- 架构门禁恢复通过，参考 `FIX_REPORT_0.3.0_2026-10-31.md` 的回归记录、部署建议与限制。

## 0.3.0 双向 Mode 同步修复候选（2026-10-31）

- 修复已经产生回答、仍保留 ACP Provider 的空闲会话在 Dyna → Tiga → Dyna 切换时不更新的问题。会话真实重置与选择切换须一致，不能仅修改 UI 展示。
- 模式切换仍保持 Tiga 默认 Multi、Dyna 默认 Flash、Dyna 展示顺序 Miracle → Flash → Strong；内部类型 ID 与路由配置不变。
- 运行中的会话及其子代理受保护，不自动切断。切换时若处于执行中，请等待结束后重新进入该会话/打开模型菜单以触发协调。
- 当前仍为 Windows 0.11.0-rc.5 / 0.11.0 端到端待验证的修复候选。详见 `FIX_REPORT_0.3.0_MODE_REVERSE_2026-10-31.md`。

## 0.3.0 V8 candidate: cross-platform 0.11 turn clock correction

- Corrected a source compatibility gap: 0.11 Crew's actual time owner modules are `turn_dispatch_gates.py` and `turn_stats.py`, not the legacy monolithic `chat_runner.py` timing seam.
- BYO's 0.11 server Turn elapsed footer uses an early monotonic start and end-of-completion clock, not a shorter Provider self-reported duration. Official backends keep their own timing policy.
- Two new source-contract-validated Crew owner files are installed transactionally; prior 21 exact source pins stay intact. Strict source preflight remains required.
- This is **not yet** exact click-to-final-browser-paint timing: a client-side render instrumentation contract is required for that separate metric.
- Windows, macOS and Linux are supported *candidate targets*, not tested/verified OS combinations. See `FIX_REPORT_0.3.0_CROSS_PLATFORM_TURN_TIMING_2026-10-31.md`.


### Records Center Phase 1 candidate (2026-10-31)

The Records page now has a unified overview timeline and independent retained counts for Turns, route decisions, other runtime events and protected Runs. Only bounded, whitelisted metadata is returned from the new read-only endpoint. Five existing UI languages are supported. No existing retention, permission, adaptive-routing or recovery-writing behavior is changed. Desktop end-to-end validation is pending; see `RECORDS_CENTER_PHASE1_REPORT_2026-10-31.md`.
