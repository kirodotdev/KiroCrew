# Kiro BYO 0.3.0 V12 — 客户端等待时间测量（测试候选）

## 范围与目标

本轮基于 V11 `0.3.0+aaf9da18ba53683b`，只新增 Crew 0.11 前端的**客户端观测计时**。原有服务端 Turn 计时（V8）、Provider `upstream_ms`、路由性能反馈和 Durable Run 完全不变。未覆盖旧版 Crew 的前端、未改变模型 ID。

## 实施

- `src/kiro_byo/assets/crew11_ui_timing.js`：捕获用户在当前 `.input-area` 的明确发送动作，使用 `performance.now()` 记录起点。
- 通过已有只读 `/api/chat/slots` 检查运行到空闲的状态转换，并检查 `.chat-container` 里是否出现新的助手回复；两次 `requestAnimationFrame` 后记录本地终点。
- 会话不唯一且无法绑定运行中的 slot、没有实际运行状态转换、没有新回复、请求失败或页面隐藏时，**不报告耗时**，避免伪造数据。
- 客户端结果只作为单独的 `界面实测 ≈ X.X s` 小浮层展示，不覆盖官方 Footer、历史记录或服务器统计。
- 轮询在短任务期间为约 250ms，长任务为约 800ms，因此 UI 观测值有轮询/渲染误差；它不是零误差的物理屏幕绘制计时。
- `crew11_model_labels.py` 通过原有 BYO 专属 `index.html` 安装事务嵌入脚本。`pyproject.toml` 将 JS 资源纳入 wheel。支持简中、繁中、英文、日文、韩文的观测标签。

## 计时边界

**这不是完整的最终解法**：当前宿主未向第三方 BYO 暴露前端 `onSend`、唯一 Turn ID、`onReplyPainted` 的稳定 SDK，因此只在单一可见聊天、观察到合理的 slot 与新回复时进行保守估计。多窗口/多聊天并发、流式恢复、失败/取消、输入快捷键设置差异和跨平台实际浏览器绘制时刻仍需实机核实。要把客户端耗时直接替换主 Footer，必须由 KiroCrew 的 React 聊天组件提供明确 Turn ID 与最终提交后的渲染回调。

## 安全与兼容

- 不拦截/覆盖 `fetch`、WebSocket，也不修改 ACP 会话、Provider、模型、后端或任何运行数据。
- 不读取、不输出用户消息、凭据或工具内容。
- 仅 GET `/api/chat/slots`，无持久化写入。
- 保留原有双后端切换的安全刷新机制；安装兼容校验仍 fail-closed。
- 跨平台共用浏览器接口，但 Windows/macOS/Linux 实机尚未验证。

## 离线验证

- JS 语法检查与 Node DOM/slot 状态模拟覆盖：正常完成、多个 slot 中唯一忙碌者、仅用户消息、多个忙碌者、未实际运行。
- 原有 Crew 0.11 标签、安装事务、服务端计时测试持续通过。
- 不能将浏览器模拟测试表述为 KiroCrew Electron 实机端到端测试。

## 后续严格精准方案

让 Crew 正式公开/接入三个前端事件：`ui.turn.send_accepted(turn_id, performance.now())`、`ui.turn.first_answer_painted(turn_id, …)`、`ui.turn.final_answer_painted(turn_id, …)`，并用同一 `turn_id` 关联服务端模型、工具和恢复遥测。然后将可证明关联的客户端总耗时作为主要显示值，并提供来源/误差说明；保留原有服务端与 Provider 统计不受影响。
