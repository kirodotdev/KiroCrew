# Kiro BYO 0.2.3 · Crew 0.9 停止支持 / 0.10-only 清理记录

**日期**：2026-10-24  
**基线**：S4-03 Tiga 插件化源码包，Build `0.2.3+63bb081ad7d2749e`  
**新支持范围**：KiroCrew **0.10.x**，官方审计目标 `v0.10.0-insider.4`（`e9bfada`）。未来 0.10 构建仍须通过逐接口语义检测。**0.9.x 不支持；0.11+ 尚未适配。**

## 代码改动

1. 删除 `integration/kiro_crew/v0.9-adaptive/` 目录、`scripts/verify_crew09_integration.py` 和旧版上游 CI 矩阵。
2. `app.json:minKiroCrewVersion` 更新至 `0.10.0`；`integration_manager.resolve_target` 在探查文件前显式拒绝低于 0.10 和高于/等于 0.11 的 Crew 版本；Transformer 只接收 `adaptive10`。
3. `bootstrap_patch.repair()` 要求 `v0.10-adaptive/adaptive10` 的已安装快照，同时核对记录中的 Crew 版本和可读取的当前 Crew 版本，禁止旧版继续自修复。
4. 0.10 仍使用的一组 ACP/权限/聊天控制语义函数由旧文件重命名为 `crew_shared_patch.py`；`crew10_patch.py` 保留引用并继续兼容过去由 BYO 所有的 0.10 补丁。**移除旧版适配入口不等于删除仍被 0.10 依赖的安全补丁。**
5. Windows 安装指引、README、CHANGELOG、发布检查和测试全部切换为 0.10-only 声明。新增 0.9、未知版本与 0.11 的拒绝路径测试；保留共有变换测试与 0.10 适配契约测试。

## 自动化验证

- `PYTHONPATH=src python -m pytest -q --disable-warnings`：**805 passed, 1 skipped, 52 subtests passed**。
- `python scripts/release_check.py --metadata-only`：所有产品版本字段为 `0.2.3`，通过。
- `python -m compileall -q src backend scripts`：通过。
- 发布包应仅含 `integration/kiro_crew/v0.10-adaptive/`，不含 `v0.9-adaptive` / 旧验证脚本 / `crew09_patch.py`。
- 0.10 官方源码真实语义验证和 Windows KiroCrew 实机端到端尚未在本轮重新执行；不能仅凭 Python 单元测试声称所有安装场景对等。

## 对现有安装的影响

- **仍使用 Crew 0.9 的机器**：不要直接使用新版安装包替换旧包来执行卸载/修复。先使用之前支持 0.9 的 BYO 版本按原卸载流程恢复 Crew 文件，再升级 Crew 0.10 并安装本版 BYO。请先备份配置和工作区。
- **已使用 Crew 0.10 的机器**：0.10 现有可识别的 BYO 补丁继续按语义兼容检查升级。更换 App 后完全重启 Crew；现有活跃会话仍遵循其原始 Harness 所有权。
- **未来 Crew 0.10 补丁版本**：版本字符串不会单独授予兼容性，所有源码接口必须通过源形状与权限约束检查；未知接口拒绝更改。

## 明确未做

没有改变 Tiga 四形态的动态模型评分、Sticky Pin、路由 ID、Provider 配置；没有开放 Dyna 的 Flash/Strong/Miracle 执行；没有移除历史技术审计报告（历史事实仍可追溯）；没有在机器上实际卸载任何旧 Crew/旧 BYO 数据。
