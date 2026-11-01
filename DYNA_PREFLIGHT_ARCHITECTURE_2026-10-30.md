# Kiro BYO 0.2.5 — Dyna 启用前审计与代码复杂度治理（2026-10-30）

## 目的与边界

输入基线：`Kiro_BYO_0.2.5_Records_Health_Fix_Source.zip`，构建 `0.2.5+7052e0c6f67015c9`。
保持产品版本 **0.2.5**，Dyna **继续禁用**。没有给 Flash/Strong/Miracle 发明策略，没有改变 Tiga Sky/Multi/Power/Glitter、Crew 模型来源、ACP 权限、MCP、工具/副作用或 Durable Run 行为。

## 源码检查与可复现结论

| 检查项 | 结论 | 证据/处理 |
| --- | --- | --- |
| Dyna 是否可执行 | **当前不可执行（按设计）** | `modes/dyna.py` 与 `policy_packs/dyna.json` 双重 `enabled=False`；Registry/Router 显式 fail-closed；`dyna` ID 在自定义插件安装和发现流程中被保留。新增回归覆盖三 Type 的拒绝调用和不生成假路由 Trace。 |
| Dyna 三 Type 的真实执行策略 | **尚未设计完整，不能直接开放** | Dyna JSON 的 `execution` 只有三个数值 routing 权重；Tiga 的四 Type 还有 reason/intents、adaptive/fit、orchestration、secondary 等约束和差异。仅把 enabled 改成 true 不构成合格的发布。 |
| 非 Tiga Mode 是否能复用基础设施 | **可控模拟通过，不能替代 Dyna 正式策略验收** | 使用数据驱动、开启的 **preflight 自定义 Policy Pack**，定义同名 Flash/Strong/Miracle，分别执行三轮 ACP→Router→Durable Run→Provider stub→TurnTelemetry→RuntimeTrace。每轮真实 `model.route.selected` 与请求记录的 `session_id`、`turn_id`、`run_id` 一致。Dyna 本身未启用。 |
| 具体模型直连 | **通过** | 非 Tiga Mode 下直接指定真实 Provider 模型，完成一轮推理后不产生虚构的 Type 路由记录。 |
| 路由事件维护边界 | **结构性风险，已进行最小拆分** | 原 `turn_runner.py` 内联构造约 70+ 行路由事件；提取到无副作用 `src/kiro_byo/routing_events.py::selected_route_event_data`，TurnRunner 仍只在 Durable Run 成功创建后写真实事件。对提取前后 `data` AST 做结构等价检查，结果一致。 |
| 发布检测与模块拆分的耦合 | **已确认问题并修复** | `scripts/release_check.py` 原仅在 `turn_runner.py` 中搜索 `"task_profile"` 字段；拆分后虽然执行正确却 FAIL。现验证 `routing_events.py` 的真实字段和 TurnRunner 对独立函数的调用。发布检查 PASS。 |
| 请求失败与健康状态、Trace 清除独立性 | **继承前版修复，相关测试通过** | 健康只由运行/集成状态决定，非单次请求结果；路由 Trace 与请求遥测独立；Durable Run 不参与“清除请求”。本次未修改这些存储/状态策略。 |
| Crew 0.11.0-rc.5 Windows 环境 | **未实机验证** | 源包包含的 Crew 版本声明仍属于 rc1 源码审查与 rc2+ 候选契约预检；无法证明 rc.5 宿主 ACP 存活状态或渲染器行为。 |

## 本次最小修改

1. `src/kiro_byo/routing_events.py`：新增无副作用的路由决策事件数据投影，字段、舍入、上限、选择信息保持原样。
2. `src/kiro_byo/turn_runner.py`：将原内联事件结构替换为一次共享函数调用；保留原事件写入时机、真实关联 ID 与 `content={"prompt": text}` 由既有 Trace 策略处理的语义。
3. `scripts/release_check.py`：升级契约检查路径，既要求新投影字段存在，也要求 TurnRunner 实际调用它。
4. `tests/test_dyna_preflight_shared_infrastructure.py`：新增三项回归；Dyna 保留禁用、三 Type 的通用共享链路、具体模型直连不伪造 Type 路由。
5. `src/kiro_byo/modes/dyna.py`：移除过期的 `0.2.3` 模块说明（无行为改动）。
6. 本报告与自动更新的 `BUILD_INFO.json`。

## 完整测试结果

- Python `compileall` PASS。
- `node --check ui/index.mjs` PASS。
- `scripts/release_check.py` PASS。
- 全量 `pytest` 依文件拆成五组，保证完整覆盖：**1,133 passed、1 skipped**，另有 **52 subtests passed**，没有出现失败。拆分只是执行方式，不是筛选测试。
- 抽离前后路由事件的 `data` AST 结构比对完全相同（仅 `session.model` → 显式输入 `requested_model`，`selection_truth` → `selection` 参数更名）。

## 代码行数的真正瓶颈

基线统计（Python/JS/MJS/TS，含测试；统计时未纳入本次新测试和新模块）：约 **71,006** 行。其中 `src/kiro_byo` 41,375 行、`backend` 1,427 行、`ui` 1,431 行、`tests` 24,441 行、`scripts` 1,861 行，另有少量其他路径代码。生产代码的重量主要集中在少数高度耦合入口，而不是测试代码多本身。

最明显热点：

- `turn_runner.py` 原 5,018 行：Provider 循环、工具调度、ACP turn 事务、恢复、安全护栏、遥测混杂。
- `acp_server.py` 3,709 行：JSON-RPC/Session/模型选择/UI 控制面多种职责。
- `integration_manager.py` 1,924 行：源代码修补、事务安装、回滚、宿主兼容。
- `mode_router.py` 1,451 行：信号、可用性、评分、协作、执行计划混在一起。
- `backend/routes.py` 1,427 行、`ui/index.mjs` 1,431 行：管理 API/前端页面交叉规模增长。

基线 Python AST 方法统计：约 1,488 个函数定义，**52 个函数超过 100 行、15 个超过 200 行**。例如 `integration_manager.install_from_app` 542 行、`turn_runner._handle_prompt_request` 498 行。比代码行数更有治理价值的是缩短这些跨职责函数、减少外部状态读写和形成单向依赖。

### 推荐的阶段化治本路径（未在此次修复中进行大重构）

1. **先锁定不变量**：对每次提交坚持真实 Route→Turn→Run 关联、Durable/副作用不回退、失败请求不污染 Runtime 健康、Crew 模型源不跨后端、五语言覆盖等自动化契约。
2. **拆纯函数和数据投影（低风险）**：像这次路由事件的提取一样，把格式化、判定和只读映射从长事务函数迁出。保留 `TurnRunnerMixin`、ACP 对外接口、存储格式，减少横向耦合。
3. **拆 ACP 控制面（中风险）**：将 JSON-RPC 校验、模型选择、Session 生命周期各自提为内部服务，`acp_server.py` 仍作为兼容适配器。优先测配置无效/取消/恢复/并发。
4. **拆执行副作用边界（高风险）**：将 Provider loop、工具批次、Durable checkpoint 分层；每阶段先做行为等价测试，始终让执行权和副作用准入保持在同一事务边界。不能未经验证地拆散 `unknown_side_effect` 保护。
5. **宿主兼容与 UI 模块化**：以语义适配器隔离 rc1/rc5 等 host 差异，并将记录、Mode、Provider UI 组件拆分；保留现有布局和本地化键值。
6. **用指标评估而不是压缩行数**：每次小步提交记录模块最大函数长度、进口依赖、测试失败率、行为等价和发布检查。代码行数可以先不下降，但核心模块应该更短、职责更清晰、修改影响面更小。

### 在正式启用 Dyna 前的“准入门槛”

- 由产品规则明确 Flash/Strong/Miracle 的性能目标、pool/fallback、adaptive、reasoning、worker/admission、工具/安全、未知副作用与取消恢复语义；分别制定测试，不得复制 Tiga 的规则后改名。
- 完整开启正式 Dyna 后做三 Type 的真实 Type→Route→Trace→Records API→UI 回归，不以自定义 preflight Pack 的测试代替。
- 与具体模型直连及 Tiga 四 Type 做双向隔离/切换测试，同时验证 Crew rc.5 原生设置的联动与撤回。
- 拿到可靠的宿主 ACP liveness 信号后补全红灯检测；不要以请求次数推测 Runtime 崩溃。
- 完成 Windows KiroCrew `0.11.0-rc.5` 的运行与 UI 实机验收，尤其是重启/升级、工具安全、副作用保护与五语言。

**审计结论**：Dyna 的禁用保护没有发现需要立即改变执行语义的漏洞；当前最大的待办是先设计三种 Type 的完整独立 Policy 语义以及 rc.5 实机验收。此次只做了低风险结构拆分和针对性回归，不声称 Dyna 已可正式发布，更不将代码行数下降等同于功能质量提高。
