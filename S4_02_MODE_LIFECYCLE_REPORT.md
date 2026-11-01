# S4-02 — Mode 生命周期与策略版本管理报告

- 日期：2026-10-24
- 基线：S4-01 Build `0.2.3+525ff890f33a8fe1`
- 产品版本：继续 `0.2.3`；Crew 目标为 `v0.10.0-insider.4`
- 范围：数据型 Mode 插件的安装、升级、启停、版本准入和单步回滚；保留旧 Policy Pack API。

## 修改

1. `policy_packs.py` 添加可选 `plugin_version`、`registry_api` 元数据；旧策略无须迁移，不兼容协议 fail-closed。
2. `mode_registry.py` 新增只读 Mode 版本属性；界面查询数据添加插件版本与协议字段。
3. 新增 `mode_lifecycle.py`：仅操作 BYO 用户自定义 JSON Mode，提供安装/更新、显式启停、状态查询与最近一次变更回滚。使用文件锁、原子替换、内容指纹及回滚快照。
4. 管理器拒绝修改内置 Tiga/Dyna。Dyna 仍不能执行，自定义 Mode 不获得额外模型、工具或 ACP 权限。
5. 中断更新时，不将尚未提交的新包误认为已安装；人工编辑后阻止自动覆盖。
6. `tests/test_s4_02_mode_lifecycle.py` 覆盖升级、禁用、回滚、哈希冲突、协议不兼容、内置保留与中断写入。

## 不变范围

- 保持 Tiga Sky/Multi/Power/Glitter 的策略、Mode→Type→Model、四形态动态路由、无 Jev 决策模型。
- Dyna Flash/Strong/Miracle 仍预置但不可执行。
- 未改 Crew 集成补丁、用户数据、Saved Agent、会话引擎、权限、MCP、Provider 和管理 UI。

## 尚未交付

- S4-03 仍需完成更彻底的 Tiga 内置策略/插件结构迁移与行为对照。
- 用户图形化管理、外部插件代码隔离、签名市场与多版本历史均不属于 S4-02。
- 真实 Windows Crew 集成、持久化故障注入仍待集中验收；当前测试不等于桌面功能完整对等。

## 验收

- [x] 原有无版本的 Policy Pack 可读取，旧 Mode/Type 选择 ID 不变。
- [x] 自定义 Mode 版本单调升级/启停/一次回滚，编辑冲突 fail-closed。
- [x] 不兼容插件拒绝加载；Tiga/Dyna 不可被管理器更改。
- [x] 注册表读写状态可见；不执行来自 JSON 的任意代码。
- [ ] 真实 Windows KiroCrew End-to-End。

## 本地执行证据

- `pytest -q --disable-warnings`：**793 passed / 1 skipped / 52 subtests passed**（单次全量运行）。
- 新增 S4-02 定向测试：**6 passed**；同时验证 S4-01 原有入口兼容。
- `scripts/build_release.py --skip-wheel`：版本与元数据检查通过；产品版本 `0.2.3`。
- ZIP 完整性检查：通过；安装包 **112** 个文件与源码包 **245** 个文件中对应文件字节完全一致。
- 构建指纹：`0.2.3+8b85f8ef3eb52e7e`。
- Windows KiroCrew `v0.10.0-insider.4` 真实 E2E：**未验证**。
