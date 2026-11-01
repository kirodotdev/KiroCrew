# Crew 0.11 模型选择器分组标题修复（2026-10-29）

## 用户现象

KiroCrew RC3 的会话模型下拉框仍正确选中 Multi Type，但下拉框顶部没有以前的「形态 · 默认复合型，可手动切换」分组说明，导致界面分组和上下间距看起来异常。

## 定位与修复

`src/kiro_byo/crew11_model_labels.py` 的 MutationObserver 原来只在新增节点的文本包含 `kiro-byo-type:` 时重新执行分组装饰。新版 Crew 可以从一开始就显示 `Sky Type` / `Multi Type` 等友好名称，而模型 wire id 只位于子元素的 `data-model-id` 属性。这种情况下，模型菜单后挂载不会触发原来的装饰过程。

现在额外识别新增节点自身或子元素的 Type `data-model-id`，再运行原有的分组标题、自动模型隔离和模型分组选项装饰。没有改变点击目标、`data-model-id`、默认 Multi、Mode/Type、Provider 或 ACP 执行逻辑，也没有修改 Crew 的编译 JS 文件。

## 验证

- 新增 DOM 回归：菜单延迟挂载、显示文字已为友好名称、wire id 仅保留在 DOM 属性中。
- 断言恢复「形态 · 默认复合型，可手动切换」，并保留 Auto 隔离、Settings 作用域和模型 wire 身份。
- 不涉及 Cron 并发事务、Mate/Worker 执行路线及 KiroCrew RC3 本机 E2E。

## 本机验证提醒

安装更新版后重启 Crew，确认会话下拉框顶部标题重新出现；若仍未出现，需要采集 RC3 下拉框 DOM 结构和运行日志，不能把此合成 DOM 测试当作真实桌面通过。
