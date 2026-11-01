# Kiro BYO 0.2.3 — S4-01 Mode Registry / 模式插件化基础

- 固定目标：KiroCrew `v0.10.0-insider.4`；产品显示版本保持 `0.2.3`。
- 上游基线：S3-03 Build `0.2.3+e3d7a1ba4f335ae7`。
- 边界：这是 **data-only Mode 插件注册**，并不是加载第三方 Python 执行代码的扩展系统。共享 Harness 不做二次实现。

## 本次改动

1. 新增 `src/kiro_byo/mode_registry.py`，包含只读 `ModePlugin`、`ModeRegistry` 和 `load_mode_registry()`。
2. `policy_packs.py` 的旧 API（`load_policy_packs`, `get_policy_pack`, `resolve_policy`, `default_pack`）保持签名和返回行为，内部委托注册表；磁盘扫描保留为私有发现入口 `_discover_policy_packs`。
3. 原有 Tiga 模型路由、评分、Sticky Routing、执行策略与 Type 标识不变；`ModeRegistry.resolve()` 返回既有 `PolicyDefinition`，不改变上层配置加载/路由消费。
4. Dyna 可发现而不可执行，注册表与旧 API 均显式拒绝其三种 Type。`dyna` 用户同名覆盖仍被拒绝。
5. 自定义 Policy Pack 继续支持数据热加载；不引入新的进程内全局缓存，旧配置、日志、`kiro-byo-type:<mode>:<type>` ID 兼容。
6. `public_policy_packs()` 增加向后兼容的注册表元信息 `mode_registry_api_version` 和 `mode_plugin_kind`（额外字段，旧 UI 可以忽略）。

## 不变项目

- Crew ACP、工具、会话、Saved Agent、队友/后台与权限逻辑：未修改。
- Tiga 四形态及既定策略、无 Jev 路由、Dyna 禁用、UI 一屏布局：未修改。
- 未引入可执行插件安装器、动态代码加载、Hook、独立进程或新配置数据库。

## 验收范围

- [x] 静态入口兼容；Tiga 4 Type、Dyna 3 Type 标识稳定。
- [x] Dyna 所有执行入口 fail-closed；自定义同名覆盖被拒绝。
- [x] 自定义 Mode 增删改后热重读，已获得的快照不被后续改变。
- [ ] 真实 Windows KiroCrew 的会话/选模/Mode 页面全流程。
- [ ] S4-02：真正的 Mode 生命周期、插件包隔离和安全迁移（另行设计）。

## 风险与回退

若 KiroCrew App 内调用出现模式加载问题，停止部署 S4-01，并以 S3-03 的源码和安装包作为回滚点；不要删除 Crew 数据或用户配置。自定义 Mode 的可信来源、签名、Code Plugin 沙箱不属于 S4-01。

## 本地测试结果（2026-10-24）

- Mode Registry + 旧 Policy Pack + Tiga/Dyna 接口定向：**47 passed**。
- 全项目以 S3-03 相同分组法执行：**A 组 474 passed / 1 skipped / 22 subtests；B 组 313 passed / 30 subtests**。
- 合计 **787 passed / 1 skipped / 52 subtests**，两组均无失败。
- Python `compileall` 和版本元数据一致性检查通过；尚未进行 Windows KiroCrew 真实端到端验收。
- 先前 S3-03 曾出现未拆分串行运行超时，本轮为两组测试通过，不冒充一次完整串行执行成功。
