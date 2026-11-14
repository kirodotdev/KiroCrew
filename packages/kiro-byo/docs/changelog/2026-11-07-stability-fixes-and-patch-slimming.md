# Kiro BYO 开发记录 — 2026-11-07 稳定性修复与补丁收敛（迈向 0.4）

> 基线：0.3.8（本机工作区，未提交 git，按用户要求以本机最新状态为准）
> 本次主题：**不加新功能，专注"瘦身 + 稳定 + 说得清"**，为 0.4 规范化、上 GitHub 做准备。
> 所有改动均在真实源码上进行，并以真实测试验证（非模拟冒充）。

---

## 一、本次完成的改动（全部已验证）

### 1. 修复：原生工具沙箱越界漏洞（安全）

- **文件**：`src/kiro_byo/native_specs.py`
- **问题**：`_load_native_policy()` 无条件把整棵 KiroCrew 数据主目录（`crew_data_home()`）加成原生工具的"允许根"。结果：任意一个会话的原生工具能读写**整个 CrewData 树**——包括别的会话、别的工作区的数据、配置、记忆。违反 ACP「额外目录必须由客户端每次显式声明」的契约。
- **修法**：删除该隐式注入。原生工具的根只保留：当前会话 cwd + 用户在 `config/native_tools.json` 的 `allowed_roots`（或 `KIRO_BYO_ALLOWED_ROOTS`）显式授权 + 客户端每次 session/new、session/load 声明的 `additionalDirectories`。敏感路径 deny 仍对每个根生效。
- **验证**：`test_acp_control_plane.py` 的 3 个相关测试（out_of_scope / session_roots）从红转绿；最小复现脚本确认越界读被正确拦截。
- **补充**：这些测试在真实 KiroCrew 机器上会被本机 `crew_data_home` 污染，需隔离环境变量——已记录。

### 2. 补丁收敛：对官方 Crew 的改写从 20 个文件 → 17 个

目标：减少"每次 Crew 升级就要重新适配"的耦合。先派只读子代理测绘 20 个被覆盖文件，分三类（必要-单点 / 必要-深改 / 可疑冗余），再按"最安全先动"逐刀退役。

**第一刀 — 退役 turn 计时补丁（#19 + #20）**
- 文件：`turn_dispatch_gates.py`、`turn_stats.py` 两处补丁
- 性质：纯 BYO turn 耗时"显示口径"微调，与接入/路由/权限完全无关
- 退役后 BYO 耗时显示回退到官方口径，功能无损
- 改：`crew_current_patch.py`（删 2 常量 + 2 函数 + `_ensure_runtime_capability_import` + TARGET_FILES + 2 dispatch 分支）、`manifest.json`、`compatibility.json`、删 `test_crew11_turn_wall_clock.py`
- 验证：契约/集成测试 70 passed、0 失败

**第二刀 — 退役 member_sessions.py（#14）**
- 现役注入仅剩一行注释，真实成员路由逻辑已迁至 `loader_builders.py`
- 官方 `select_provider_backend` 优先级（显式 override > 队友 harness pin > 成员自动 > 配置默认）本就正确，BYO 不该再碰
- 走 BYO 自带的 `retired_generated_files` 正规退役轨道：升级时用官方原文件 pristine 恢复，自动清掉旧版遗留的强制守卫（正好根治"队友被硬拉进 BYO"）
- 改：`crew_retirement.py` 白名单 + member_sessions、`manifest.json`/`compatibility.json`、`crew_current_patch.py`、`test_crew11_no_official_member_fallback.py`
- 验证：成员路由 + 集成测试 67 passed、0 失败

**第三刀 — 退役 Crew 0.9 时代远古迁移死代码**
- `_kiro_byo_crew_pin_scope_reason`（识别并升级 Crew 0.9 时代老 BYO split-backend 补丁的迁移码）
- 基线只认 Crew 0.12+，一个能升到 0.3.8 的用户其 Crew 必然已是 0.12+，不可能还带 0.9 时代老补丁 → 这条迁移路径实际已走不到 = 死代码
- 改：`crew_patch_common.py`、删 `test_crew09_agents_old_split_backend_patch_is_upgraded`（保留 "not in patched" 正向断言）
- 验证：57 passed、0 失败
- 注：未动 `_previous_owned_*` 族（较近 R72 facade 升级路径，来源版本不同，风险有别）——精准砍，不误伤

### 3. 修复：MCP 子进程泄漏竞态（稳定性，主因）

- **文件**：`src/kiro_byo/mcp_transports.py`
- **问题**：会话正在启动 MCP 子进程（`subprocess.Popen`）时，若会话被关闭（close），刚诞生的子进程无人 terminate，成为僵尸，拖住整个进程不退出。这是实机"队友卡工作中 / Gateway 一直显示工作中"的同源病根。
- **修法**：
  - 新增持久关闭标志 `_shutdown`（close 一次性 latch，永不被后续 start 重置）
  - `_launch_process`：Popen 赋值后立即检查 `_shutdown`，若 close 已发生则 `_terminate_orphan` 杀整个子进程树（Windows 用 `taskkill /T /F`）并抛错退出
  - `start()`：握手前检查 `_shutdown`，已关则拒绝启动
  - close 区分两种语义：`permanent=True`（会话真关，latch）vs `permanent=False`（握手失败/重连清理，不 latch，可重连）
  - terminate 逻辑抽成 `_terminate_orphan` 两处复用；`HttpMcpServer.close` 同步加 `permanent` kwarg
- **验证**：最小竞态复现 6/6 零僵尸；`test_acp_control_plane.py` 从**卡死 110 秒跑不完 → 24.9 秒跑完**；MCP 生命周期测试 54 passed

### 4. 修复：取消 / 插话（cancel/steer）卡死 + 语义对齐官方

- **文件**：`turn_runner.py`、`provider_loop_terminal.py`、`acp_session_state.py`
- **问题（两层）**：
  1. 取消信号在"检查 → 消费 steer"之间的极小窗口到达时，被误判成 interrupted（stopReason=end_turn）而非 cancelled，终结响应发不出 → 卡死
  2. 一条刚插入、尚未被模型处理的 steer，被 provider worker 抢先标成"已注入"；即便紧接着 turn 被取消，也不回滚 → 违背官方语义
- **官方依据**（查 Crew 0.12 `client_class.py` 的 `steer_needs_loss_recovery`）：**取消/失败的 turn，steer 交回 Crew 重新排队，只有 turn 干净结束才算"已消费"**；Kiro CLI 同理。
- **修法**：
  - `_handle_provider_interruption`：消费 steer 返回 False 后**再查一次 cancel_event**，捕捉第一关之后才到的 cancel
  - 新增 `turn_injected_steer_ids`（本 turn 临时注入的 steer id）；turn 以 cancelled 收场时 `_rollback_turn_injected_steers` 把 receipt 从 injected 回滚成 cleared 并发 `steering_cleared` 事件
  - 关键补漏：`_finish_provider_loop_cancelled_before_invoke`（第二轮重跑时在 invoke 前检测到 cancel、绕过中断路径的终结点）也加回滚
- **验证**：探针确认 receipt injected→cleared、steering_cleared 发出；`test_cancel_...` 1 passed（原卡死 30s → 1.03s）；**完整 `test_acp_control_plane.py` 40 passed、21.23s、进程干净退出**；中断/取消回归 36 passed

### 5. 前端数据端点（仅后端，未动界面）

- **文件**：`src/kiro_byo/web_ui.py`
- 为三个"做好但未接 UI"的仪表盘模块各加只读数据端点（**刻意未改任何现有界面 HTML / 风格**，用户对现界面满意）：
  - `/api/records` → `record_center.records_snapshot`（统一运行记录总览）
  - `/api/overview` → `overview_state.build_overview_snapshot`（版本/provider/能力/健康，复用 /api/status 同款数据源）
  - `/api/routing-dashboard` → `routing_dashboard.build_routing_dashboard`（Tiga 路由可视化）
- 修正一处真实错误：误用 `KiroByoConfig.load()` → 正确的 `load_config()`
- **验证**：三端点冒烟全通；三模块测试 12 passed
- **待办**：前端页面接法（融进现有卡片页 / 独立标签页）待用户定，将严格复用现有配色字体卡片样式

---

## 二、关键认知（写给 0.4 和未来维护）

### 原生 ACP 接入的真相
- 官方 KiroCrew 0.12.0 源码里**没有任何 BYO 痕迹**，`ACP_BACKENDS_KNOWN` 是锁死的冻结集合，官方故意用它 + 路由校验挡住所有仓库外 harness（防绕过安全）。
- BYO 靠语义补丁改写官方 17 个源文件接入。代码自己的注释承认："Apps 目前没有公开的任意 backend 接入 API"。
- **这是商业决定（官方不收编），不是技术不通**。所以 BYO 永远要自维护接入层。
- **"准治本"目标**：把要改的点收敛到最少、最稳，让 Crew 升级（0.13/0.14）时只需改一两处而非 20 处。彻底零改需官方开放接入口——不在我方掌控。

### 深改 7 处的判定（收敛的边界）
经评估，以下属"必要-深改"，**不可收敛、必须保留**，原因正当：
- `#7 backends_routing` 的 `ACP_PERMISSION_GATE`：故意返回 `Verdict.INDETERMINATE`（不是官方的 ROUTED），诚实标注"BYO 工具权限未经 Crew 完整验证"。**收敛它 = 伪造安全认证，违反红线。**
- `#13 mirrors/registry` 的 `KiroByoMirror.session_projection`：实现官方没有的"多会话 host 托管身份绑定服务器"语义（让队友能回报频道），与官方单二进制 harness 相反，耦合宽是形态决定的。
- `#18 chat_title`：官方标题模型是硬编码常量、无统一入口，只能改调用点。
- 结论：**能砍的是历史废料，留下的是 BYO 作为"多会话管家型 harness"的真本事。**

### 6 个"孤儿"模块的真相（不是垃圾）
`routing_dashboard`、`record_center`、`overview_state`、`mode_lifecycle`、`crew_linkage`、`crew_ledger_bridge` 共约 1284 行，被标"零引用"，但**全有测试、功能完整，只是做好了没接 UI**。属"半成品好功能"而非死代码——**决定全部保留**。其中三个可视化/总览模块接进 UI 正好能治 BYO"说不清"的痛（本次已接后端数据端点）。

### 瘦身的真实距离
- 现状约 41,955 行，目标约 35,000 行，需砍约 7,000 行。
- 退役"死/半死"模块最多回收约 2,500–3,500 行（仅覆盖目标 40–50%）。
- 剩余缺口必须从**巨型活模块内部去重/拆耦合**出：`turn_runner`(2641)、`acp_server`(2261)、`integration_manager`(1667)、`mode_router`(1490)、`native_tools`(1304)，以及 trace 三件套（work_trace/runtime_trace/telemetry 共 762 行）。这是细活、风险高，需逐模块读实现。

---

## 三、0.4 规范化路线（上 GitHub 前）

1. **瘦身**：啃巨型模块内部冗余（非删功能），逼近约 35,000 行。
2. **立规矩防复发**：每加一个东西先问"能不能不改官方源码"；改不了的明确标成"自维护层"。这是治"越滚越乱"的根。
3. **接仪表盘 UI**：把 routing_dashboard / overview_state / record_center 接进界面，让 BYO 第一次"看得懂"（严格复用现有风格）。
4. **仓库规范化建议**：
   - 版本历史 → 根目录 `CHANGELOG.md`（通用标准）
   - 开发文档 → `docs/` 文件夹（当前"开发与升级说明"文件夹上 GitHub 前建议改名 `docs/`）
   - 补丁源码清楚标注"自维护 Crew 扩展层，非官方原生"
5. **实机验收**：真实 Crew Gateway 端到端（本次只跑了单元/集成测试，实机那关未过）。
6. **交付包**：Install.zip + Source.zip + 开发说明。

---

## 四、尚未解决 / 未做（诚实清单）

- ❌ 真实 Crew Gateway 实机端到端验收（只跑了单元/集成测试）
- ❌ 第四阶段：模型混合池、多模态、专长模型路由测试（完全未碰）
- ❌ 简洁交付包未打包
- ❌ 巨型模块内部瘦身未开始
- ⬜ 仪表盘前端页面未接（仅后端数据端点就绪）
- ⬜ `_previous_owned_*` 族 facade 升级码可否收敛，待核实来源版本

---

*本记录对应 2026-11-07 的开发会话。所有"已验证"均为本机真实 pytest 运行结果，非模拟。git 未提交，按用户要求以本机工作区最新状态为准。*

---

# 续：2026-11-07 晚 — 0.4 正式收口会话

> 接上文「迈向 0.4」。本节把 0.3.8 正式收口成 **0.4.0**：全量测试本机全绿、架构棘轮转绿、五处版本号统一。
> 方向严格按用户已定的 **A+B**（A 只砍确证死代码、已到头；差额用 B 如实抬棘轮基线），无一处造假、无一处为凑数硬跳过。

## 五、本会话完成的收口（全部已验证）

### 1. 架构棘轮诚实收口（0.4 唯一代码阻塞项）

- **现象**：`architecture_guard.py --check` 报 `core_python_lines 46455 > budget 46437`，守卫红，`test_architecture_guard.py` 随之红。
- **归因**：守卫只统计 `src/kiro_byo/` 行数。真实 46455 比上个会话写定的预算 46437 多 18 行——这 18 行是上个会话继续做安全修复/恢复误删的 re-export 时的真实净增（发生在预算写定之后），不是臃肿。逐文件核对确认无单文件破预算，破的只是总量。
- **修法（B）**：如实把基线从 46355 抬到真实的 **46455**（净增约 100 行），三处同步：
  - `config/architecture_budget.json` 的 `core_python_lines` → 46455，并更新收口注释写清这 100 行对应取消回滚、MCP 竞态、沙箱越界三组必要安全修复 + 恢复 `advertised_native_tool_names` 再导出。
  - `tests/test_architecture_guard.py` 的 `assert budget["core_python_lines"] <= 46455`（原 46437）。
- **性质**：棘轮防的是无意义膨胀，不是必要安全修复。真正的大模块瘦身（约 42,000→35,000）留给专门重构轮次。
- **验证**：守卫 `architecture budget: PASS`；`test_architecture_guard.py` 9 passed。

### 2. 修复：出队背压测试误报（真修根因，非跳过）

- **文件**：`tests/test_acp_lifecycle_failures.py::test_stalled_stdout_has_bounded_nonblocking_queue`
- **现象**：期望队列满抛 `queue exceeded`，实际抛 `ACP stdout has failed`，稳定失败，耗时 ≥3.5s。
- **根因**：`AcpServer.send` 每条都走 `runtime_trace.record` 落盘 I/O。测试连发 2055 条填满 2048 上限队列，但在 Windows 慢文件系统上这 2055 次带落盘的 send 耗时超过了测试给写出线程设的 3 秒停滞窗口 → 写出线程先因 `write` 超时置 `_outbound_failed`，"has failed" 抢在 "queue exceeded" 前触发。**产品背压逻辑本身正确**（队列满会正确抛 queue exceeded）。
- **修法**：测试加 `monkeypatch.setenv("KIRO_BYO_RUNTIME_TRACE", "off")` 关掉这条测试本不关心的 trace 旁路 I/O（`runtime_trace.record` 在 `off` 模式直接返回、零落盘）。不改产品逻辑、不放宽断言。
- **验证**：三次重跑全 passed，耗时从 3.5s 降到 0.3s（反证就是 trace I/O 拖累）。

### 3. 修复：两个真实 ACP 子进程端到端冒烟的 WinError 2（真修脚手架，非跳过）

- **文件**：`scripts/acp_conformance.py`（测试辅助脚手架 `Process` 类）
- **涉及测试**：`tests/test_r68_mode_type_wire.py`（Mode/Type/Model 线协议）、`tests/test_r32_work_link_protocol_smoke.py`（work-link 往返）
- **现象**：两个端到端冒烟报 `FileNotFoundError: [WinError 2] 系统找不到指定的文件`。
- **根因**：`Process.__init__` 用 `shlex.split(command)` 拆 `"C:\...\python.exe -m kiro_byo acp"`。POSIX 模式的 `shlex.split` 把 Windows 路径里的反斜杠当转义符吃掉，`C:\Python\python.exe` 被拆成 `C:Pythonpython.exe` → 找不到文件。这是测试脚手架的 POSIX 假设 bug，不是产品 bug（真实 Crew 调 BYO 不走这条 shlex 拼接路径）。
- **修法**：`shlex.split(command, posix=(os.name != "nt"))`，Windows 下非 POSIX 拆分保住反斜杠；补 `import os`。
- **关键联动**：上个（出问题的）会话那次没生效的 `import os` 改动，正是为这个修复做的半步——它只加了 import、没改 `shlex.split` 那行，且改动本身没落盘，才显得像废 import。本会话一并做对。
- **选型理由**：这是能真修的脚手架 bug，修好后两个测试在 Windows 本机**真跑真绿**，优于 `skipif` 跳过。契合「BYO 要和 Codex/Cursor 同等出色、在 Windows 要真能用」的目标。
- **验证**：两个冒烟脚本直跑 `ok: true`；两个测试 2 passed。

### 4. 版本号收口 0.3.8 → 0.4.0（五处统一）

真相源是 `VERSION`，有版本一致性测试（`test_release_current_contract` / `test_app_package`）断言全部等于它。五处全升：

- `VERSION`、`pyproject.toml` 的 `version`、`src/kiro_byo/__init__.py` 的 `__version__`、`src/kiro_byo/protocol.py` 的 `AGENT_VERSION`、`app.json` 的 `version`。
- 升版后首次全量暴露 4 条版本一致性红（`protocol.py`/`app.json` 还留着 0.3.8），补齐后全绿——这正是一致性测试的用途。
- `BUILD_INFO.json`（`build_id` 带 commit 哈希）是打包产物，由打包流程生成，**未手改**。
- `app.json` 里的 `license: "MIT"` 是既有字段，用户决定暂不加 LICENSE，本会话未动，待用户定夺。

### 5. CHANGELOG 定版

- `CHANGELOG.md` 的 `[Unreleased] — 迈向 0.4` 段定版为 `[0.4.0] — 2026-11-07`。
- 补入本会话两条修复（shlex 跨平台修复、背压测试稳定化）到稳定性修复段；补架构棘轮诚实收口到架构收敛段。
- 「已知未完成」改写为「0.4 之后（下一轮）」。

## 六、0.4 最终状态（本机真实 pytest）

| 指标 | 收口前 | 收口后 |
|---|---|---|
| 全量测试 | 3 failed / 1885 passed / 77 skipped | **1888 passed / 0 failed / 77 skipped** |
| 架构棘轮 | 红（46455 > 46437） | 绿（PASS） |
| 版本号 | 0.3.8 | 0.4.0（五处一致） |

**此刻名正言顺是 0.4.0。** 全量 174s 跑完，0 失败。

## 七、0.4 之后（下一轮，本会话未做，按用户要求）

六个"孤儿"模块经本会话再次确认定性：全有测试、功能完整，是"做好没接 UI"的半成品特色功能，**用户已决定全部保留**，勿删：

| 模块 | 行 | 是什么 | 离能用多远 | 判断 |
|---|---|---|---|---|
| routing_dashboard | 333 | Modes 页路由仪表盘：Tiga 为何选某模型、各模型表现 | 功能完整，差 UI 页面显示 | 留（好东西） |
| record_center | 132 | 分散运行记录汇成只读总览 | 功能完整，差 UI 接线 | 留（轻、有用） |
| overview_state | 167 | 概览页/侧边栏健康状态（多少 provider/模型/运行如何） | 功能完整，差 UI 接线 | 留（很基础） |
| mode_lifecycle | 269 | 自定义 Mode 的安全增删改+一键回滚 | 功能完整，差 UI 接线 | 留（BYO 扩展性底子） |
| crew_linkage | 189 | 只读查"某队友/角色该用哪后端和模型" | 纯查询函数，随时可调 | 留或并（很小、无害） |
| crew_ledger_bridge | 194 | 把 Crew Work Ledger 任务与 BYO 运行关联（带安全校验） | 需 Crew 控制面配合才用得上 | 留（安全相关，别删） |

其余下一轮事项（均未做）：
- 真实 Crew Gateway 实机端到端验收
- 巨型模块系统性瘦身（turn_runner/acp_server/integration_manager/mode_router/native_tools + trace 三件套去重），逼近约 35,000 行
- 接仪表盘前端页面（三个只读端点 `/api/records`、`/api/overview`、`/api/routing-dashboard` 已就绪，严格复用现有界面风格）
- 小米 MiMo 深化：评估接入其 Token Plan 订阅套餐（`providers/xiaomi_mimo.py` 已有基础；MiMo 开放平台支持 OpenAI Chat Completions/Responses + Anthropic Messages 三协议）
- 立「不再改官方源码则标自维护层」的防复发规矩

---

*本节对应 2026-11-07 晚的 0.4 收口会话。所有"已验证"均为本机真实 pytest 结果，非模拟。git 未提交，按用户要求以本机工作区最新状态为准。*
