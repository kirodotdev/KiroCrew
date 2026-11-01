# Kiro BYO 0.2.5 · Crew 模型显示名称按界面统一

## 用户确认的显示规则

| Crew 界面 | Type 显示示例 | 不变项 |
|---|---|---|
| 聊天模型菜单的置顶动作 | `将 Multi Type 置顶到 kirocrew 代理（覆盖全局默认）` | `kiro-byo-type:tiga:multi`、按钮动作、会话状态栏完整名 |
| 队友模型选择（含已选值与下拉候选） | `Multi Type` | 继承语义、具体模型名、原始 value |
| 设置 → 聊天 → 后台模型（含下拉候选） | `Tiga · Multi Type` | 原始 value、Auto 选项、设置写入 |
| 开发者 → 遥测中出现的模型标识 | `Tiga · Multi Type` | 原始统计数据、请求日志、历史模型 ID |

以上规则同样适用于 Sky / Power / Glitter；Dyna 保持禁用，没有改变 Type 路由。

## 代码修复

- `src/kiro_byo/crew11_model_labels.py`：使用现成的 `short` / `full` 字典，不新增模型映射或修改配置。
- 聊天 pin action 只在聊天模型菜单含 BYO Type 行且选中菜单开启时，严格限制在与 Type 行相同的弹层内，把置顶操作文案中的完整 Type 名称替换为短名；不修改页面其他位置。
- Mate 2.0 `agents.model` 已选值与其门户 listbox 选项统一为短名；保留继承与具体模型。
- 设置的单值 `SettingsSelect` 除默认模型外支持后台模型。优先依赖 `data-setting-label`；部分 Crew RC 没有该属性时，只识别“后台模型”明确标签且包含唯一 combobox 的局部区域，继续保留 Auto。
- 开发者遥测页面必须存在明确“遥测 / Telemetry”等标题，才将**完整匹配**的 Type wire-id 文本叶替换为全名。源码、日志、消息、代码和可编辑输入排除在外，不猜测截断 ID，不改写底层遥测。
- MutationObserver 识别后挂载的完整名节点，避免重复挂载弹层后又变回长名。既有模型选择器滚动定位逻辑保持原样。

## 回归验证

- 全部 137 个测试文件均被执行：原始 137 文件版本统计 1,184 passed、1 skipped、58 subtests passed；第 4 组为防止长组停滞，拆为 47 + 53 个通过项。
- 最后追加简繁中、英、日、韩文案参数化场景，新增浏览器场景总计 18 个；最后针对性复测 `test_crew11_name_scope_browser.py` 和 `test_crew11_model_labels.py` 共 33 passed（新增场景均通过）。按文件去重的完整测试项合计 1,196 passed、1 skipped、58 subtests passed。
- Chromium/Playwright 回归覆盖：聊天菜单 pin 反复更新、同页无关置顶按钮不变化、选中 chip 不变化、Mate 选项、后台设置有/无 data-setting-label、遥测栏/表、原始代码隔离、具体模型与 `unknown` / `auto` 原样保留。
- `scripts/release_check.py` 通过；`node --check` 在现有测试中验证注入 JS 语法；最终构建重新进行发布检查。

## 不作保证的部分

- Windows KiroCrew 0.11.0-rc.5 实机 DOM 尚未接入本环境。模拟覆盖有助于验证逻辑，但仍需针对 rc.5 的真实页面检查后台模型控件及遥测的实际挂载结构。
- 仅修复 UI 文本展示，不修复 Crew 遥测 `unknown` / `auto` 的原始数据来源，也不把逻辑 Type 误判为真实 Provider 模型。
- 不修改 Tiga Policy Pack、Dyna enable 状态、Crew 模型写入 API、ACP 或 Durable Run。
