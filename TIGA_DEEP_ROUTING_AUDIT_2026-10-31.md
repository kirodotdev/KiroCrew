# Kiro BYO 0.2.5 — Tiga 四形态路由深度审计与反馈学习修复

> 基线：`0.2.5+225fa7737dec4cac`（ACP V3 / MCP Hardening Source）；保留产品 SemVer `0.2.5`。
> 本次范围：Tiga 反馈归因、缓存有效期、验证工具和回归测试。**没有修改四形态权重、模型池、协作语义、Dyna、Crew UI 或 Provider 接口。**

## 1. 架构与边界

`KiroCrew → ACP Server → Mode Registry → Tiga Policy Pack → Mode Router → Agent Runtime → Provider/Tools/Durable Run → TurnTelemetry/RuntimeTrace → Adaptive Routing`。

Tiga 是内置的数据策略插件，`modes/tiga.py` 声明四 Type，`policy_packs/tiga.json` 保存策略，公共 `mode_router.py` 和 `agent_runtime.py` 负责路由/编排。**不是可以任意装卸 Python 代码的热插拔**：自定义 JSON 策略支持数据热加载；内置 Tiga 的正式更新仍通过 BYO 构建，进行中的 Run 保持执行快照。`audit_mode_plugins.py` 静态检查无错误。

## 2. 四 Type 现状（策略文件原样保留）

| Type | 质量/速度/经济权重 | 主模型准入 | Worker | Reviewer | 主要风险/待验证 |
|---|---|---|---|---|---|
| Sky | 12/62/26 | fast；仅能力不足允许越档 | 不启用 | 极端条件下触发 | 首字速度和最终完成耗时如何权衡；能力兜底是否频繁 |
| Multi | 50/25/25 | any；动态复杂度调整 | adaptive | 高风险/验证收益触发 | 任务特征偏差、复杂任务是否误选弱模型 |
| Power | 76/14/10 | strong；严格拒绝弱档 | adaptive | 更积极的质量复核 | 高质量模型的实测完成率、复核是否有效 |
| Glitter | 94/4/2 | strong Leader；严格拒绝弱档 | always（受任务形状/预算约束） | always | 简单任务的固定协作/复核成本；多 Worker 证据冗余 |

**不更改策略的理由：** 当前没有覆盖真实模型输出质量、Token 成本、跨任务完成率的可比较数据。用少量样例直接改权重会制造不可靠的“提升”结论。上述权重来自原始 Policy Pack，并不是优化后重设的参数。

## 3. 已复现缺陷与修复

### A. 取消/中断污染可靠性和样本置信度

旧 `adaptive_routing.snapshot_from_rows()` 用 `1 if outcome == 'success' else 0` 将 `cancelled` / `interrupted` / 不属于模型的错误全部计为失败；还把这些样本计入 confidence 和 task-specific 样本数。原始请求记录并未出错，是**路由学习归因**错误。

新版本只从 `success` 和可归因的 `error`/`failed`（`provider`、`transport`）构造模型反馈；保留无类别旧版明确错误以避免历史语义突变。`internal`、`durable_checkpoint`、`control`、取消和中断从模型评分里排除，**不删除原始日志，不改变请求失败率统计**。非模型失败不会增加 runtime 速度/可靠性置信度。

### B. Reviewer/Worker 的 ProviderInterrupted 被再次算为模型失败

失败 Worker 的 Trace 可能携带 `error_category=control`；旧版 Reviewer Trace 只有异常类名 `ProviderInterrupted`，被 `runtime_failure_rows()` 误写为模型失败样本。新版本兼容旧 Trace 的异常类名判断，并让新的 Reviewer 失败事件明确写出 `error_category`。Provider/transport fallback 和真实 Reviewer 失败继续是有效负面样本。

### C. 时间衰减缓存不会自然过期

`current_snapshot()` 原来只依赖文件 mtime/size。没有新事件时，3 天半衰期配置不会重新计算：同一快照可无限复用。现在读缓存最多复用 60 秒，文件变化依然立即失效；**60 秒只是缓存刷新频率，不是 Type 的路由权重**。

### D. 历史/受损遥测的非数值延迟和时间戳

旧实现用 `float()` 直接转换，会因 `"n/a"` 或 `"infinity"` 影响路由计算。新版本仅接受有限数值，非法时间戳使用保守时间基线，非法延迟不进入速度证据。原始错误行不被删除。

## 4. 可复现的四形态离线合同检查

新增 `scripts/tiga_policy_preflight.py`，在隔离临时 HOME 和**合成模型**中检查 4 类任务 × 4 Type 共 16 次路线及执行计划；无账号、无网络、无真实模型推理。

| 任务 | Sky | Multi | Power | Glitter |
|---|---|---|---|---|
| 简单问候 | fast / single | fast / single | strong / single | strong / leader-worker-review |
| 代码修复 | fast / single | balanced / single | strong / lead-review | strong / leader-worker-review |
| 跨模块审查 | fast / single | balanced / single | strong / lead-review | strong / leader-worker-review |
| 图像任务（fast 无视觉） | balanced / single（能力兜底） | balanced / single | strong / lead-review | strong / leader-worker-review |

这些结果只能证明**选模硬约束和计划生成在合成候选集中的行为**，不能证明回答正确率、工具效果、性能或真实 API 兼容性。Glitter 简单问候仍有协作计划，这属于现行策略，不是这次引入的行为。

可执行：`python scripts/tiga_policy_preflight.py`；输出 JSON 可与后续策略候选版本比较。运行时不会读取或覆盖用户 Provider 密钥。

## 5. 安全与不变性

- 保持 `Sky/Multi/Power/Glitter` IDs、`Multi` 默认、具体模型直连、Tiga 权重/候选池和 Worker/Reviewer 语义不变。
- 不影响 Crew 双向模型联动、五语言资源、ACP Tool、MCP 安全规则或 `DurableRunStore`。
- 未改变任何已保存遥测或 Runtime Trace 文件，也未清除现有 Run；改变的是后续**读数据用于学习**的判定。
- 保留失败日志和原始统计；评分投影不再错误惩罚用户取消。
- 旧 Trace 如果既没有类别，也没有可辨识异常类名，仍按旧版失败语义处理；无法凭缺失信息倒推出真实责任来源。

## 6. 验证

- 139 个测试文件已分组覆盖：**1,220 passed、1 skipped、58 subtests passed**；Crew 浏览器测试所在分组拆开执行并全部通过。
- 新增归因与时间衰减回归，检查 4 Type 取消后选模不变、模型错误仍可降权、Trace 控制中断不降权、合成离线 16 场景。
- Python 编译、UI JS 语法、`release_check.py`、`architecture_guard.py`、Mode 插件静态审计均通过。
- 无 Windows KiroCrew 0.11.0-rc.5 E2E；无真实 Provider 质量/延迟评测，不能声称实机全链路或模型性能已经验证。

## 7. 未解决/后续按优先级处理

1. **P1 真实任务基准集**：为四 Type 建立同一任务、同一候选池的正确率、执行完整性、工具安全、Tokens/费用/时延指标。离线合成路线不替代真实结果。
2. **P1 速度样本公平性**：目前成功请求的延迟仍会跨不同 prompt 长度/任务类型比较；后续需要分层任务和足够样本才考虑调整评分。
3. **P1 Glitter 协作净收益**：衡量 Worker/Reviewer 对质量的实际增益，避免仅凭 Agent 数量评优；不在本次擅自修改 `always` 语义。
4. **P2 真实失败归因补全**：历史无类别错误无法无损辨识，未来可增加明确的失败分类与来源字段，但不能伪造历史数据。
5. **P0 Windows 宿主实测**：至少覆盖 Tiga 4 Type、具体模型直连、取消/恢复、工具失败与权限、Crew 0.11.0-rc.5 集成。

## 8. 文件清单

生产代码：`src/kiro_byo/adaptive_routing.py`、`src/kiro_byo/turn_runner.py`；发布门禁：`scripts/release_check.py`；新增：`scripts/tiga_policy_preflight.py`、`tests/test_tiga_feedback_attribution.py` 及本报告。

**特别强调：Dyna 仍保持禁用；暂未进入 Dyna 三形态实现。**
