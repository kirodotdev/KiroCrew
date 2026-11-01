# Kiro BYO 0.3.0 — Crew 聊天模型目录同步 V5 修复候选

日期：2026-10-31  |  Windows 宿主端到端：**未验证**

## 实机已确认的事实

用户在 KiroCrew 0.11.0-rc.5 里选择 Dyna 时，浏览器 Network 里 `GET /api/models` 返回 HTTP 200，包含 `kiro-byo-type:dyna:miracle`、`:flash`、`:strong`，顺序正确，而聊天模型菜单仍然显示旧 Tiga。手动 `Ctrl+R` 后模型菜单立即正确。由此确认服务端模型目录正确，问题发生在 Crew 前端的缓存/同步路径，不能再归因于 Dyna Router。

`GET /api/instances` 的 403 在截图中出现，但**未证实**与 Mode 同步有关；不能以此假定 `GET /api/chat/slots` 也返回 403。

## V4 根因与此次处理

V4 的宿主内联观察脚本需要先通过 `/api/chat/slots` 确认全局空闲，才可能调用 `window.location.reload()`；当该读返回 403/404、网络失败、格式未知、存在草稿或执行中任务时，仅静默 `false`，没有任何用户可见的错误。旧的 2 次重载上限到达后也缺少提示。**本轮没有证据证明该门禁确实在用户机器上返回失败**，因此 V5 是可观察性和安全收敛修复候选，不是已经通过 Windows 端到端验收的最终修复。

- `src/kiro_byo/crew11_model_labels.py`：宿主脚本中，保留 `/api/models` 每 3 秒只读检测（沿用 V4），仅在渲染的模型 Type 与服务端不同且 `GET /api/chat/slots` 确认空闲、没有草稿时自动安全刷新；在模式设置页面没有聊天 composer 时避免强制刷新。
- 新增 BYO 自有状态提示：因执行中、草稿、`/api/chat/slots` HTTP/网络/格式异常而无法安全自动重载时，直接在聊天界面显示原因。已知存在执行中任务或未发送草稿时**不展示手动刷新按钮**；未知状态时只允许用户明确确认无任务后手动刷新。
- 两次安全重载仍不收敛时停止循环并显示警告，而非静默保留旧菜单。
- 为确保旧 V4 的防重载 sessionStorage 记录不会污染 V5，隔离为 `kiro-byo:mode-refresh-attempt:v3`。
- `backend/routes.py` 的只读 `/mode-sync` 诊断更新至 v3 脚本标记，便于判断新的注入脚本是否确实被安装到 Crew `index.html`。
- 不修改任何模型 ID、Type 顺序、Tiga/Dyna 路由权重、ACP、Provider、持久化账本或用户配置。

## 测试与边界

- 新增 `tests/test_030_mode_sync_v5_live_browser.py`：Node VM 故障注入，覆盖 `/api/chat/slots` 403→恢复、busy→idle、草稿保护、自动重载上限、显式手动操作。5/5 通过。
- 核心宿主同步与安装回归 108 项通过；JavaScript `node --check`、`architecture_guard.py --check`（54/54）和 `release_check.py` 均通过。
- 分为 4 组执行的非浏览器几何回归：301 + 393 + 313 + 264 = **1,271 项通过，1 项跳过、58 subtest 通过**。该统计是增加第 5 个专项参数之前的分组统计，新专项 5/5 随后单独通过；避免累加重复测试。
- 单进程完整 pytest 仍可能在已有测试组处卡住；Playwright/Chromium 真实几何测试本轮未完成，Windows KiroCrew 端到端也未执行。

## 安装与验收

1. 备份当前 BYO 配置，用原有“更新集成”流程替换宿主补丁，**彻底退出包括 Gateway 的 KiroCrew** 并重启。仅把新 ZIP 解压在文件夹中不会更新原先的宿主 `static/dist/index.html`。
2. 在 BYO 选择 Dyna → 进入同一个已完成回答的聊天，不按 `Ctrl+R`，等待最长约 3 秒。菜单应显示 `Miracle / Flash / Strong`；切回 Tiga 后应显示 `Sky / Multi / Power / Glitter`。运行中任务不能强制中断。
3. 如果仍不同步，观察聊天区域顶部右侧是否出现 **“Kiro BYO 已切换模型目录…”** 的阻塞说明。若有，截图文字以明确原因；若无，查看同源 `GET /api/apps/kiro-byo/mode-sync` 的 `installed_host_patches.mode_watch_script`：若为 `false`，说明宿主未安装 V5 注入脚本。
4. 若安装标记为 true、`/api/models` 已是 Dyna、却既不自动刷新也不显示警告，怀疑脚本加载或前端执行异常，仍需从 DevTools 的 Network/Console 进一步查证。不得将这一情况宣称为解决。

## 长期工程建议

最终设计应在 Crew React 模型查询实际拥有者处，收到 BYO 后端/Mode 更新事件时调用 QueryClient 失效/重新获取，并在 idle boundary 协调会话；不应长期依赖在 `index.html` 注入定时检测与整页重载。当前是对 KiroCrew 0.11 宿主兼容性的渐进性、失败关闭的补丁。
