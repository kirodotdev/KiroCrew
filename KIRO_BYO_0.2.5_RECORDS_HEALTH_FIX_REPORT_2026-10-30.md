# Kiro BYO 0.2.5：记录系统与全局健康状态修复报告

日期：2026-10-30  
输入：`Kiro_BYO_0.2.5_Multi.zip`，原始 `BUILD_INFO.build_id=0.2.5+951c568915ed0e3d`  
发布：产品版本保持 `0.2.5`；修复构建 ID：`0.2.5+7052e0c6f67015c9`（以 `BUILD_INFO.json` 为准），文件 SHA256 见 `SHA256SUMS.txt`。

## 1. 审查结果与真实根因

| 部位 | 源码证据与结论 | 修复 |
|---|---|---|
| 全局运行健康 | `src/kiro_byo/overview_state.py` 原本依据 `diagnostics.failed` 直接进入 `degraded`，把请求失败错误映射为运行状态故障。**已确认缺陷。** | 删除这个健康判据；`health`（兼容旧 UI）与新增 `runtime_health` 仅按既有集成/配置状态聚合；新增独立 `request_outcome`，请求失败数量和分类保持真实，原诊断统计不变。 |
| 清除请求 | `backend/routes.py::clear_diagnostics()`、独立管理 Web UI `src/kiro_byo/web_ui.py::do_DELETE()` 原本都清空 `TurnTelemetryStore` *及* `RuntimeTraceStore`。**已确认跨存储误清理。** | `DELETE /diagnostics` 只清请求遥测和随请求保留的自适应特征，响应 `removed_trace=0`；保留独立的 `DELETE /runtime-trace` 供显式清除 Trace；从未触碰 Durable Run。 |
| 路由记录显示空 | App UI 原本只拉取最新 100 条任意 Runtime Trace，前端再筛 `model.route.selected`。高频 `acp.outbound`、工具/检查点等可挤出真实路由事件。**已确认数据流缺陷，可控数据复现。** | 为 `RuntimeTraceStore.recent(event=...)` 与 App/独立 HTTP Records API 增加事件类型筛选：在保留的有界全量 Trace 中筛选，再截取最近 N 条。UI 请求 `event=model.route.selected`，不按模型名、默认 Type 伪造事件。 |
| 路由—Run 关联 | `turn_runner.py` 原本在开始 Durable Run、创建活动 Turn ID 之前写 `model.route.selected`；可控 Multi Type 推理证实事件 `run_id=''`，而最终 TurnTelemetry 有实际 Run ID。**测试中新增发现并复现。** | 保留原始路由决策、模型选择和执行计划不变，将**同一真实执行事件**移到成功开始 Durable Run 后写入，使 `session_id/turn_id/run_id` 对齐。Durable Run 创建失败时不伪称执行成功。 |
| 四个标签语义 | “全部记录”实际只展示最近请求，并不合并 Route 与 Durable Run；恢复列表来自 `DurableRunStore`。**界面文案不匹配实际语义。** | 改为“请求概览”并说明仅汇总最近请求；路由和恢复维持独立数据源。简中、繁中、英语、日语、韩语均已更新。 |

## 2. 实际数据流、清除语义

| 页面 | 当前数据源 / API | 用户点击“清除请求记录”后 |
|---|---|---|
| 请求概览（原“全部记录”） | `TurnTelemetryStore().recent` via `GET /diagnostics` | 请求列表和请求汇总清空 |
| 请求 | 同一 `TurnTelemetryStore`；包含 `route_signals/route_diagnostics` 的随请求诊断 | 清空；旧请求路由特征不会继续参与自适应统计 |
| 路由 | `RuntimeTraceStore` 的真实 `model.route.selected`，via `GET /runtime-trace?event=model.route.selected` | **保留**已落盘的历史 Trace；新 Type 推理追加真实事件 |
| 恢复 | `DurableRunStore().list`（只读公开投影），via `GET /diagnostics` | **保留**所有 Run，包括正在执行、待恢复、失败和潜在 `unknown_side_effect` |

`RuntimeTraceStore` 是**有上限的事件流**，无法保证全部历史永久存在；`KIRO_BYO_RUNTIME_TRACE=off` 时不会记录任何新事件，界面会明确提示。历史已经被误清除或超过 retention 的路由记录**不能无中生有地恢复**；旧会话 Trace 中已有的空 `run_id` 也不会被伪造补齐。特定用户本地的两个历史 Run ID 未包含在上传 ZIP 中，因此无法核验其真实内容，但 Run 存储及恢复/副作用策略没有被修改。

## 3. 修改文件

- `src/kiro_byo/overview_state.py`：健康状态与请求结果分离。
- `backend/routes.py`：请求清理仅作用于遥测；Runtime Trace 按事件过滤。
- `src/kiro_byo/runtime_trace.py`：在保留的事件流中筛选，避免最新 100 条事件挤掉真实路由。
- `src/kiro_byo/turn_runner.py`：真实 Type route 事件移动到成功创建活动 Durable Run 后，修正关联 ID。
- `src/kiro_byo/web_ui.py`：独立管理接口的清除及事件筛选语义同步。
- `ui/index.mjs`：路由定向读取、请求概览范围说明、Trace 关闭提示。
- `src/kiro_byo/locales/{zh-CN,zh-TW,en,ja,ko}.json`：五语言文案同步。
- `tests/test_overview_state.py`：更正原有错误健康判定预期，增加真实集成故障优先级检查。
- `tests/test_records_health_regression.py`（新）：请求清除与 Durable Run/未知副作用保护、事件高频挤出、真实可控 Multi Type 推理与路由/Turn/Run ID 对齐。
- 本报告与自动更新的 `BUILD_INFO.json` 属于文档/构建元信息。

## 4. 测试和回归

- Python `compileall`：通过。
- 五组执行全量 `pytest`：**1,130 passed，1 skipped，1131 tests collected**；另有若干 parameterized subtests passed。分组执行是为了避开单次命令执行时间上限，不是删减测试。
- 核心回归：清空旧请求 → 新建 BYO Multi Type Session → stub Provider 完成一轮可控真实执行链路 → 验证请求成功 + 原生 `model.route.selected` 产生 + Session/Turn/Run 关联 ID 一致：**通过**。
- Durable Run 保护：保留 running、存在 side-effect risk 的 Run，清除后原持久化记录逐字典一致：**通过**。
- 事件筛选：一条真实 route 后注入 240 个非 route ACP 事件，新的过滤查询仍返回真实 route：**通过**。
- `scripts/release_check.py`：通过。

对照上传原始 ZIP 的 SHA256：`src/kiro_byo/policy_packs/tiga.json`、`mode_router.py`、`mode_registry.py`、`adaptive_routing.py`、`crew11_patch.py`、`crew_linkage.py`、`durable_runs.py` 完全一致；没有修改 Sky/Multi/Power/Glitter 的决策权重、Worker 计划或 Crew 原生配置联动。

## 5. 未解决、需要实机验证

1. **KiroCrew `0.11.0-rc.5` 的 Windows 主机集成**：上传的 BUILD_INFO 仅证明旧版 rc.1 源码审查和后续候选版本的 provisional contract preflight；当前环境不是用户的 Windows KiroCrew，无法完成真实 Gateway/ACP 生命周期、前端热更新与五语言窗口实测。
2. **ACP 进程真实存活检测**：目前 KiroCrew App 状态接口主要依据集成/配置的只读状态，无由宿主提供、受验证的 ACP 实例 liveness 信号。此次已消除请求失败引起的错误黄灯，但**不能宣称**断开的 ACP 一定会自动变红；需要在 rc.5 真机上获得可靠的宿主存活状态接口后单独补足，避免使用失败请求次数推测 ACP 已崩溃。
3. **原用户历史数据**：压缩包不含本地持久化账本，所以无法确认两个给定历史 Run 是否还在用户机器上、Trace 是否此前已被清除，以及 `KIRO_BYO_RUNTIME_TRACE` 的本机有效值。无需也不得为填补历史而伪造事件。
4. **Trace 的有限保留**：超出 `RuntimeTraceStore.max_records` 的早期事件仍会按设计滚动清理；此次仅修复查询可见性，不承诺无限留存。

本次修复不删除历史失败、不改请求成功率、不做任何模型选择路径重写，不清空 Durable Run Ledger，不包含用户凭据或本地运行数据。
