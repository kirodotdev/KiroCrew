# Kiro BYO 0.3.0 — V6 草稿误判最小修复候选

## 事实与修复边界

用户在 Windows KiroCrew 0.11.0-rc.5 现场已证明 `/api/models` 返回 Dyna 三个 Type、手动 Ctrl+R 后 Crew 菜单显示正确。V5 的主界面提示进一步指向“检测到未发送的草稿”；然而截图中当前 composer 是空的。

V5 `src/kiro_byo/crew11_model_labels.py` 的 `safeToReloadCrew()` 遍历整个 `document.querySelectorAll('textarea,[contenteditable="true"]')`，对隐藏的旧会话、关闭的弹窗或不可见编辑器的文本也认为是当前草稿，因而拒绝刷新。**这能解释用户看到的提示，但现场尚未提供是哪一个 DOM 节点触发的证据。**

此次 V6 仅在现有检查中跳过 `hidden`、隐藏/不活动祖先，以及 `checkVisibility` 和 `getClientRects` 确认不可见的编辑器。可见的未发送文字仍然阻止自动重载。原有 `/api/chat/slots` 的安全门禁、重试上限、模型 ID、官方/BYO 后端转换和 ACP 会话管理均保持原状。

> 这修复的是一种误判路径，仍然依赖安全检查通过后执行整个 Crew 页面重载；不是原生 React Query 层的无刷新热更新，Windows 端到端尚未验证。

## 测试

- 新增 6 项 Node VM 注入脚本测试：隐藏旧编辑器、不含草稿的可见输入、真实可见草稿、可见弹窗草稿、运行中的会话阻止重载、获得焦点但视觉透明的真实草稿保护。
- 已有 V5 同步、后端转换、Tiga/Dyna 双向切换与安装相关 104 项定向测试通过。
- `scripts/release_check.py` 通过；`scripts/architecture_guard.py --check` 通过（54/54）；实际注入 JS 通过 `node --check`。
- **未运行完整 pytest 全量测试**；Windows 实际机验证仍然未完成，不能宣布故障已完全修复。

## 用户验收

1. 备份当前配置和会话状态，采用 BYO 正式集成更新流程安装此候选包，并彻底退出后重启 KiroCrew。
2. 在聊天输入框空白、无任务执行时，从 Tiga 切换 Dyna，不按 Ctrl+R；应在短暂安全检查后菜单提供 Miracle → Flash → Strong，默认 Flash。
3. 再切回 Tiga；应出现 Sky → Multi → Power → Glitter，默认 Multi；官方 ↔ BYO 也需要测试。
4. 在输入框填写临时内容（**不要发送**）后切换 Mode，应提示草稿保护，且不应自动重载丢失内容；运行中任务不得因切换中断。
5. 如果在空白 composer 仍报告草稿，下一步需要在 DevTools 中定位触发的 DOM 编辑器，只读记录元素属性和可见性，避免继续猜测。

## 长期推荐

有条件改动 KiroCrew 宿主源码时，应由 BYO Mode 切换确认事件驱动 Crew React Query 使 `/api/models` 相关缓存失效并 refetch，在空闲边界协调模型选择；不要长期依赖注入 `index.html` 的三秒轮询和整页 reload。不同后端与旧历史执行标签需要分开维护。
