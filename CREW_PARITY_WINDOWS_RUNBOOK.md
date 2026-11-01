# Kiro BYO 0.2.3 — Windows Crew Harness Parity 验收手册

**固定对照**：KiroCrew `v0.10.0-insider.4` (`e9bfada`)。保持 BYO 与 Kiro 官方执行后端的 Crew、权限、工作区与工具配置尽量相同。

本文件是一份**待执行**手册。它不会从聊天之外自动读取或修改用户的 Windows 安装、配置、密钥、Cookie 或聊天记录。请先备份现有安装与配置，再升级 BYO。**不要为了测试开启危险的免审批模式**。

## 记录每次测试的最小信息

```text
Case: E01 / E02 / E03 / E04 / E06 / E15
Crew: 0.10.0-insider.4 e9bfada
BYO: 读取安装包 BUILD_INFO.json 的 build_id
系统: Windows 版本（可选）
模式/模型: （不包含 API key）
设置: 内置浏览器 On/Off、会话工具授权、使用的 Harness
步骤:
实际 MCP 工具名称（例如 mcp__kirocrew-core__browser）:
ACP steering 事件（种类和顺序，不复制用户敏感文本）:
期望/实际结果:
PASS / FAIL / BLOCKED:
已脱敏证据:
```

## E01 — 内置浏览器开启

1. 在 Crew 设置中开启内置浏览器，使用桌面内置浏览器面板；确保当前 Agent 有使用 `kirocrew-core/browser` 的权限。
2. 新建 BYO 会话，要求“用内置浏览器打开公开网页，再点击页面上的一个链接并描述结果”。
3. 观察工具调用是否为来自 `kirocrew-core` 的 `browser`，是否出现浏览器面板联动，并记录调用成功/失败的信息。
4. 用官方 Harness 在相同设置重复。

**通过要求**：若 Crew 授予了浏览器工具，BYO 应直接向模型展示该工具，而不要求模型先使用 `tool_search`；实际是否运行内置面板应由 Crew 的 `browser` MCP 处理。无面板/无权限必须明确返回，不允许 BYO 自己绕过 Crew 政策启动隐藏浏览器。当前仅有单元测试证据，真实桌面尚未验收。

## E02 — 关闭内置浏览器与拒绝工具

1. 在 Crew 关闭内置浏览器，重复 E01。检查 Crew 既有 `browser` MCP 是否返回回退指导；不要将“工具仍然可见”误认作开关失效。
2. 将 Crew 的 Saved Agent 工具列表限制为**不允许** `@kirocrew-core/browser`，再运行 E01。
3. 检查 BYO 是否不暴露该工具，是否遵守拒绝决定，是否出现跨 shell/其他工具绕过。

**通过要求**：无权调用时不能执行；在允许调用但内置面板关闭时，回退仍由 Crew 决定而非 BYO 猜测。

## E03/E04 — Steering 与 Queue

1. 用官方 Harness 启动一个有多步工具执行的任务，中途新增“停止查找 A，改为查找 B”；保存 ACP/UI 事件序列。
2. BYO 重复同样步骤，分别在“模型输出中”、“工具运行中”、“等待权限批准时”追加要求。
3. 确认追加消息最终出现在正在执行的上下文中。取消与断线时查看是否误报已消费，是否重复工具副作用。

**暂不宣称通过**：BYO 的基础 Steering 机制已存在，但本轮未修改 Steering 状态机或验证 Windows 实际消费回执。此项是下一轮独立 S1 工单。

## E06/E15 — Harness 切换与回滚

1. 建立官方会话与 BYO 会话，记录各自固定模型、队友和历史，不清空 Crew 数据。
2. 切换默认 Harness，分别继续各自会话，确认旧会话 Provider 归属和模型 Pin 不被全局切换改写。
3. 如出错，先完全退出 Crew，使用 BYO 设置页的集成回滚/卸载入口并检查哈希备份，不要直接删除 Crew 目录或数据库。
4. 卸载后再试官方原生浏览器和队友功能。

## 本轮自动化测试

- `PYTHONPATH=src python -m pytest -q tests/test_crew_browser_parity.py`
- `PYTHONPATH=src python -m pytest -q`
- `python scripts/release_check.py`
- `python scripts/build_release.py --output-dir dist --skip-wheel`

**隐私**：证据中只保留必要的工具名称、时间、返回状态和脱敏错误；不要上传原始聊天、网页 Cookie、授权头、API Key 或个人文件路径。

## S1-02: Steering receipt acceptance (Windows verification pending)

1. Start a BYO session with a deliberately long answer. While it is generating, add a correction via Crew's normal message entry. Verify that the current turn changes direction, and record both `steering_queued` and `steering_injected` with matching `messageId` when the host surfaces those events.
2. Repeat while an authorized *read-only* tool is active. Verify no duplicate tool side effect, the new text is in the following model round, and the UI does not mark the correction complete before it actually affects the result.
3. Repeat just as a turn ends. If BYO responds `queued: false`, verify Crew retains or sends the new text as the next prompt instead of dropping it.
4. Cancel after admission but before injection. Verify `steering_cleared` appears and the pending correction is not silently claimed to have been executed.
5. Re-send a captured `messageId` with identical text (debug environment only): verify `duplicate` and no additional model-facing user message. Reuse that ID with different text: expect JSON-RPC `-32602`.
6. Test after shutdown/restart: the old in-process steering receipt ledger is intentionally ephemeral; Crew's durable message queue remains authoritative. Do not claim exactly-once *end-to-end* delivery until verified.

Record timestamps, session/run IDs, client ACP method/response, Kiro session-info metadata, actual executed tool IDs, the model's next-turn result and the Crew version. Redact secrets, paths and user content.
