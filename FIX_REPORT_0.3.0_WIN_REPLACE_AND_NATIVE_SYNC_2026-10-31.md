# Kiro BYO 0.3.0 — Windows 配置替换修复与 Crew 原生热同步边界

## 结论与完成状态

**已实现/已验证（离线）：** `providers.json` 在 Windows 上短暂被其它程序占用时，有限重试原子替换；持续拒绝访问时保持原有文件不变；重复选择已启用 Mode 不再写入同一配置。

**尚未实现：** KiroCrew 主界面的无重载 React Query 模型目录热更新。当前 V6 的安全兜底会在适当情况下调用 `window.location.reload()`；本次没有声称消除黑屏，也没有将不受支持的 React 内部状态写入到宿主。用户的实机已证明 `/api/models` 正确返回 Dyna，且 Ctrl+R 后正确显示，问题边界在宿主前端缓存。

## 根因与证据

- 截图错误：`[WinError 5]`：从临时文件 `.providers.json.*.tmp` 到 `providers.json` 的重命名失败。
- 源码：`src/kiro_byo/provider_admin.py::_write_raw` 写入临时文件、fsync 后调用 `os.replace()`，此前无任何 Windows 共享冲突重试。
- Windows 的病毒扫描、索引和暂时以不允许删除/重命名方式打开的其它进程，均可能导致 `os.replace()` 短时返回 Access Denied / sharing violation；不等于能证明某个进程就是元凶。
- `set_mode_selection` 之前即使目标 Mode/Type 和现有配置完全一致，也重复写文件。

## 修改

1. 新增 `_replace_provider_config`：仅 Windows 且仅 WinError 5/32/33（或没有 winerror 的 EACCES/EBUSY）重试，间隔 50/120/250/500ms；最多 5 次 `os.replace`。非 Windows、磁盘写满等错误不重试。
2. 复用原有同目录临时文件、文件刷盘、`os.replace` 原子发布与 finally 清理路径。**不**退化为先删除旧文件再重命名，也**不**更改配置字段和权限。
3. 重复选中相同 Mode/Type 直接返回现有设置，避免无意义的 Windows rename。

## 验证

- `tests/test_030_windows_config_replace_retry.py`：模拟前两次 WinError 5 失败后成功；连续失败保留原文件并清理临时文件；磁盘错误不重试；相同 Mode 不写文件。
- 隔离 Linux 测试环境运行的故障注入，**不是 Windows 实机验证**。当前定向回归与集成测试执行记录见本次交付摘要。
- Python 语法、架构门禁与发布检查通过。

## 原生无刷新同步——需要 Crew 宿主源码变更

目前注入脚本 `src/kiro_byo/crew11_model_labels.py` 可读取服务器 `/api/models`，但它在 Crew React 树之外执行，无法得到宿主所属 QueryClient。官方 App Kit 文档也明确每个 App 的 QueryClient 与 Dashboard 隔离。

正确做法是在 **KiroCrew Dashboard 的 React Query Provider 内**接入：

1. 监听来自 BYO 的模式变更通知，同时定期通过同源 `/api/models` 校验，保证 WebView 隔离时可用。
2. 仅当当前权威模型目录签名变化后，对 Crew **真实的模型查询 key** 调用 `queryClient.invalidateQueries` / `refetchQueries`，而不是单独发一个 fetch 或修改 React DOM。
3. 更新已打开的菜单和当前会话展示；已有任务在执行时不换模型，空闲时使用 Crew 自带的会话模型变更流程完成迁移，并保持历史消息标签不变。
4. 针对官方 ↔ BYO、Tiga ↔ Dyna、闲置/运行中、未发送草稿、接口错误和恢复后的签名收敛做组件/浏览器测试。草稿不应阻止**模型数据查询**，只有可能破坏草稿的整页重载才需要草稿保护。
5. 保留当前的防错回退，**只有宿主热刷新已真实通过实机验收**，才能关闭 BYO 的 `window.location.reload` 兜底。

**未做到：** 无法在本次交付中验证或修改用户电脑上的 KiroCrew 0.11.0-rc.5 React 组件与查询 key，因缺少该版本已安装 Dashboard 源码/构建资产，亦未在 Windows 运行。不能把源码集成方案当成已经安装的宿主修复。

## 实机验收

- 预先备份 `D:\\APP\\KiroCrewData\\apps\\kiro-byo\\data\\config\\providers.json`（无需分享其中的密钥）。
- 断开占用该文件的编辑器或同步程序后进行安装，不更改 ACL。
- 快速往返切换 Dyna ↔ Tiga 及重复点击同一 Type，验证不再发生偶发 WinError 5。如仍发生，请保留 Windows 文件权限/占用调查证据；持续的 ACL 拒绝访问不会被有限重试解决。
- **不要把切换后出现自动刷新视为无刷新切换成功。** 若要消除黑屏，需要上述 Crew 原生查询失效机制。

## 本次实际测试记录（2026-10-31，Linux/Python 3）

- 定向测试（含 Windows 故障注入与 Crew 接口）：`91 passed`。
- 扩大回归：`PYTHONPATH=src:. pytest -q --disable-warnings -k 'not browser'` → `1279 passed, 1 skipped, 44 deselected, 58 subtests passed`（47.44s）。原测试进程在工具调用超时后继续运行并正常结束，保留完整的 pytest 终态日志。
- `python scripts/architecture_guard.py --check`：54/54，通过。
- `python scripts/release_check.py`：通过。
- 尚未运行的 44 项浏览器相关测试与 Windows 宿主 E2E：未验证。

**结论：Windows 文件替换的模拟故障场景修复通过，不等于完整无刷新模式切换实现。**
