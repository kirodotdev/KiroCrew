# Kiro BYO 0.2.3 — S1-02 Steering / Queue 可靠性修复

日期：2026-10-23。目标 KiroCrew：`v0.10.0-insider.4`（仅静态目标，未做 Windows E2E）。

## 范围

本轮仅修改 `src/kiro_byo/acp_server.py` 和 `turn_runner.py` 的 Steering 收据与终态边界，新增 `tests/test_steering_receipts.py` 六项回归；原有 Tiga 四形态、Dyna 禁用、Provider、Crew 浏览器优先发现和 UI 不变。

## 修复的具体竞态

之前 `_session/steer` 在 `active_turn` 为真时可能返回 `queued: true`，但在最终回答候选已完成后，缺少原子关闭接收口，回合 `finally` 直接清空队列。这样存在刚到达的追加消息没有进入下个模型回合的窗口。

现在 `state_lock` 同时保护接收与最终回答候选的 `steer_accepting` 边界：一旦没有待处理消息，立即关闭接收；过晚的请求返回 `queued: false`，由 Crew 的正常 Queue/下一回合逻辑处理。其他提前终止路径如果仍有待处理消息，退出时显式发出 `steering_cleared` 而非静默丢弃。

## 消息收据与幂等

- 按会话保存最近 256 个 `messageId` 状态：`queued / injected / cleared`。每回合最多缓存 64 条纠正消息；超额请求明确返回未入队。该收据仅在进程内存在，不是分布式事务或长期持久化队列。
- 重试相同 `messageId` + 相同正文：不再重复注入；新正文复用同 ID：报 JSON-RPC `-32602`。
- `steering_queued` / `steering_injected` 增加 `messageId` 元数据，保留旧 `kind` 与 `content` 兼容字段。
- 注入操作在会话锁内更新 Provider 面向的消息历史，随后发布通知；取消状态下不注入。
- `steering_injected` 仅说明已进入 Provider 面向的上下文，**不能证明模型已经正确理解或完成该任务**；本地持久化也沿用既有 best-effort 策略，不能保证进程崩溃后端到端 exactly-once。

## 验证

- 定向测试（控制平面、V3、Turn Runner、新增收据）：83 passed。
- 全量测试：**721 passed, 1 skipped, 52 subtests passed**。第一次运行遇到上轮测试遗留会话文件导致发布树防泄漏测试报错；清理测试工作目录的这四个测试产物后重新运行，所有测试通过。
- Windows KiroCrew 真机上的 Steering、队友队列/会话中断，以及工具副作用尚未验收。

## 后续 / 风险

1. 在用户桌面环境验证中途提示、工具期间提示、接近结束时的排队回退、取消与重试。
2. 有权限或执行副作用的工具必须逐一验证停止机制；本轮没有放开任何授权或改变工具治理。
3. 真正“模型消费确认”仍需 Crew 与 BYO 共同提供更强的协议级证明；不能把 `queued` 与 `injected` 冒充最终完成。
4. 接下来应推进 S1 的 Crew 设置/能力快照与权限闭环；不要先重构 Tiga 或插件系统。

## 回滚

保留旧 S0-S1 的源码/安装 ZIP，出现真实回归时切回旧包并重启 Crew。无需删除队友、日程、会话数据库；没有 Crew 源码集成模板改动，也无须为本轮重新注入 Crew 补丁。
