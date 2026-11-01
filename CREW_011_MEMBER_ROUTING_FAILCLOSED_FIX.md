# Kiro BYO 0.2.5 — Crew 0.11 RC1 成员执行路由保护补丁

## 修复背景

原有 Crew 0.11 RC1 适配器只让**未显式设置**的 `agent.member_acp_backend` 跟随 BYO 主代理。若已有配置固定为 KAS/Kiro CLI，Crew 的独立队友后端优先级仍可将新队友会话发送到官方后端。模型选择与实际执行后端是不同的东西。

## 本次更改

- `config/loader_builders.py`：在主代理是 `kiro-byo` 时，为**新创建**的队友会话设置有效后端 `kiro-byo`，即使旧成员设置保存了 `kas` 等后端；不修改用户保存的设置。切回官方时恢复 Crew 原有后端选择。
- `members_parts/member_sessions.py`：为 BYO 主代理下的新队友会话增加最靠前的路由保护：即使旧的成员后端字段仍指向官方，也通过 BYO 路由；若会话另有显式官方后端覆盖，则立即失败并报告冲突；若 BYO 未注册也拒绝官方 fallback。
- `dashboard/handlers/agents.py` 的原 BYO 扩展成员模型后端解析，改为读取 BYO 模式下的**有效**成员后端，以免模型列表在切换时误判官方能力。
- 兼容升级：识别之前 BYO 写入的两个旧补丁标记，只对已知 BYO 所有的插入块执行原位升级。重复应用结果一致，未知外来源码差异继续拒绝。

## 明确不做

- 不更改 Crew 队友模型的原生选择界面，也不强制把某个模型替换成 BYO 模型。
- 不清空数据、不改写 `agent.member_acp_backend` 的原始持久化配置，不将已经打开的官方会话热迁移为 BYO。
- 未对真实 Windows Crew 0.11 RC1、每个 Mate 专属 Conductor/Worker 的所有运行路径完成实机 E2E；如果存在绕过共享配置/选择器的独立路径，本补丁不能声称绝对阻止这类路径的官方积分消耗。

## 验证建议

更新 BYO 安装包后，重新安装/更新 Crew 集成并重启 KiroCrew。**不要使用已开启的旧官方队友会话进行测试。** 先在新队友会话中检查实际后端 ID 和 BYO 请求计数，必要时对照官方积分记录。若出现 `explicit official backend override while BYO is active`，则是安全拦截，需要排查 Crew 的单会话独立官方覆盖项，而不是关闭保护。

## 工程验证

Python 源码语法、补丁幂等、旧安装升级、官方模式恢复、显式覆盖拒绝、缺失 BYO 注册时拒绝及 Crew 相关专项测试；真实 Windows 桌面验证未完成。
