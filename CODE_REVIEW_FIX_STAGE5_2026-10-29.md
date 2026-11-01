# Kiro BYO 0.2.5 — 第五阶段修复与结构整理

日期：2026-10-29  
上一基线：`0.2.5+e22e7f5980fba773`  
目标上游：KiroCrew `v0.11.0-rc.1`

## 改动

1. **渐进式执行器拆分**：将 `TurnRunnerMixin._execute_mcp_tool` 的既有 MCP 分发逻辑，原顺序抽取到 `src/kiro_byo/mcp_tool_dispatch.py` 中的 `McpToolDispatchMixin`。`TurnRunnerMixin` 继承它，既有调用入口、方法名、参数和返回值不变；权限检查、实发前绑定/可见性复核、审批后的取消/转向检查以及不确定副作用判断仍按原顺序执行。没有移动 Mode/Type、Provider、ACP 协议或 Crew 适配器代码。
2. **MCP 传输异常保守处理**：在实际 `session.mcp.call()` 后遇到 `OSError` 时，不再让异常绕过 MCP 工具状态和不确定副作用事件；返回不含敏感原始异常信息的错误，标记为执行效果不确定，并提示核对外部状态后再考虑手动重试。任何自动重试均未增加。
3. **增补 3 项回归**：入口继承兼容性、工具传输失败不重试/不泄露异常内容、Crew 审批后工具绑定发生变化必须拒绝执行。

## 不变的产品边界

- Tiga `Sky/Multi/Power/Glitter`、默认 `Multi`、Mode/Type 命名空间与具体模型直连行为不变。
- `mode_router.py`、`protocol.py`、`acp_server.py`、`crew11_patch.py`、`crew_shared_patch.py`、`policy_packs/tiga.json`、官方 Crew RC1 overlay harness、`ui/index.mjs` 等 11 个受保护文件与第四阶段**逐字节一致**。
- Crew 官方后端的模型列表、配置和历史偏好不受此轮影响。

## 测试与验证

- 新增 3 项专项测试通过，MCP/成员路由与 Mode/Type 相关专项测试通过。
- 原有 118 个 `test_*.py` 文件沿用上一阶段已验证的分组边界，结果 266 + 363 + 187 + 209 = **1025 passed、1 skipped、52 subtests passed**；再加新测试 3 passed，合计 **1028 passed、1 skipped、52 subtests passed**。
- 尝试把新增文件加入连续分组后的单次总调度，在重新划分的第三组出现超时；随后使用原分组边界逐组运行成功。因此这里只报告**分组覆盖通过**，不声称一次性全量无超时。
- Python 编译、前端 JS 语法、版本一致性及发布 ZIP 完整性均需以本次构建记录为准。

## 仍未解决

- **Windows 真实 Mate/Worker E2E**：不能在本环境启动用户的 KiroCrew 桌面，因此当前只有源码形态与集成/权限/失败关闭回归，没有声称真实官方积分或 Worker 全链路得到验证。
- **Crew 原生 Cron 与 BYO 页面跨界面原子 CAS**：RC1 公开 Cron 接口不足以在两端执行统一原子 compare-and-swap。沿用 BYO 内部串行、写后复核与冲突提示，不侵入官方存储实现。
- `turn_runner.py` 的其他大函数与 `acp_server.py` 仍需要独立、小批次的结构整理。本次只完成 MCP 分发责任拆分。

## 本地更新

将源码 ZIP 中 `Kiro BYO/` 的内容合并覆盖本地项目，保留 `.git`、用户配置及数据。App 安装 ZIP 用于 App 更新；升级后应重启 Crew，再验证 Mate/Worker 与模型选择界面。
