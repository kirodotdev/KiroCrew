# Kiro BYO 0.2.5 / Crew 0.11.0-rc.1 模型目录隔离修复

## 本次修复（可由源码和测试核实）

1. `ui/index.mjs`：TaskConsole 的日程、后台模型候选严格取自 BYO `/tasks` 返回的模型目录。旧官方/不认识的 Crew 持久化 pin 只作警告，不再放入 BYO 下拉框。读写仍经 Crew 原生 API，更新时带 `expected_model` 防旧页面覆盖。
2. `ui/index.mjs`：主后端不是 BYO 时，日程/后台显示 Crew 模型只读文字，不提供 BYO 模型下拉菜单。
3. `src/kiro_byo/crew_patch_common.py`：`/api/models` 的 BYO 分支不再因为模型列表为空而插入宿主 `configured_default`；已成功读取无 Provider 配置时，不回退到可能过期的 ACP 广告列表。
4. `src/kiro_byo/crew11_patch.py`：升级检查可严格识别前一构建的 BYO-owned 模型目录 helper，允许安全覆盖为新版本，未知修改不被接受。
5. `tests/test_byo_catalog_isolation_025.py`：6 项专门回归验证。

## 刻意未修改

- 不在任务页添加聊天、队友/Worker 选择器。聊天、队友和 Worker 仍在 Crew 原生界面管理。
- 不写入或迁移 Crew 现有用户模型 pin；BYO 任务页将不兼容的旧值显示为提醒，由用户自行选择新的 BYO 模型或继承/Auto。
- 不强行把运行中的 Crew 会话迁移至 BYO，也不改变 KiroCrew 0.11.0-rc.1 的官方逻辑。
- 不自动恢复先前每个官方模式下的模型 pin；实现这种跨模式备份需要另外明确持久化策略。

## 安装/覆盖注意

源码 ZIP 从项目根目录解压到你的本地 `Kiro BYO` 仓库，备份后合并覆盖现有同名文件。ZIP **不包含 `.git`、本地密钥、Provider 配置、临时缓存**，不会替换已有仓库历史。不要直接把源码 ZIP 作为 Crew 应用导入；如需 App 安装使用对应 `release` ZIP，按 `WINDOWS_QUICKSTART.md` 操作。更新 BYO 代码后重启 Crew，待集成管理器检查/修复 Crew 注入，确认运行进程已重新加载。

未进行真实 Windows KiroCrew 0.11.0-rc.1 的 Conductor/Worker/Cron 全链路验证。新加的 UI 和服务端测试不应替代实机验收。实际运行是否调用官方额度需要对照官方用量核对。
