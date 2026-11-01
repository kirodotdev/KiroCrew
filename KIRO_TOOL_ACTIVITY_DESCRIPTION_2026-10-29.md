# Kiro BYO — Kiro 风格工具活动描述修复报告

## 基线与范围

基线：`Kiro_BYO_0.2.5_五语言工具标题_完整源码覆盖.zip`；产品版本保持 0.2.5。仅修改原生工具 Schema、NativeToolRuntime 的展示 Schema 生成、ACP 工具标题和五语言资源，并添加测试。没有修改 Mode/Type/Tiga 策略、Provider 路由、Crew patch 注入和 Cron 存储。

## 行为

1. Model 调用原生工具时可以**额外**填写 `description`（<=160 字符），在工具 Schema 中按 BYO 界面语言明确提示：写真实操作的简短自然语言描述；不用把 Shell 命令塞进描述。
2. ACP 工具活动 `title` 优先取安全的具体描述；描述不合法、长度过大、包含控制字符、Shell 形态或显著凭据时，回退原有五语言通用标题。
3. `description` 仅用于展示，不参与权限检查、执行参数选择、MCP 工具身份。保留原始 `rawInput` 和 `toolCallId`、`kind`、`_meta.kiro`；MCP 工具继续使用原有标题规则。
4. 不强制升级旧工具调用；没有 `description` 的请求完全沿用原实现。
5. 工具 Schema 每次调用都复制，避免中/日/英等不同会话的描述语言互相覆盖。

## 安全与限制

模型填写的自然语言说明**不一定准确**，不能当作实际执行动作或权限的证明；必须保留真实命令、日志和授权审查。BYO 不会用模型说明替换原始 `command`。

KiroCrew RC3 有可能根据 `rawInput.description` 或其他参数自行生成可见标题，因此此改动还需要 Windows 实机验证。未获得实时 Crew 前端语言时，BYO `auto` 继续按系统语言回退。本修复不新增 Crew 注入点，也不保证所有 MCP 第三方工具提供类似描述。

## 验证

见本轮测试记录、Python 编译与发布检查；Windows E2E 仍为 `not_verified`。

## 本轮验收结果

- 五语言、工具 Schema、ACP、权限、Mode/Type、Mate/Worker 等扩展定向测试：279 passed。
- 全项目 124 个测试文件分四组：343 + 259 + 268 + 246 = **1116 passed**；**1 skipped**；另有 **52 subtests passed**。
- Python 编译、JS 语法、版本发布检查、ZIP/wheel 完整性通过。
- Windows KiroCrew RC3 实机展示未验证，仍需检查 Crew 是否采用 title 或 rawInput.description。

## 前后差异文件清单

- Kiro BYO/BUILD_INFO.json
- Kiro BYO/CHANGELOG.md
- Kiro BYO/KIRO_TOOL_ACTIVITY_DESCRIPTION_2026-10-29.md
- Kiro BYO/src/kiro_byo/acp_server.py
- Kiro BYO/src/kiro_byo/locales/en.json
- Kiro BYO/src/kiro_byo/locales/ja.json
- Kiro BYO/src/kiro_byo/locales/ko.json
- Kiro BYO/src/kiro_byo/locales/zh-CN.json
- Kiro BYO/src/kiro_byo/locales/zh-TW.json
- Kiro BYO/src/kiro_byo/native_specs.py
- Kiro BYO/src/kiro_byo/native_tools.py
- Kiro BYO/src/kiro_byo/tool_display.py
- Kiro BYO/src/kiro_byo/turn_runner.py
- Kiro BYO/tests/test_tool_activity_descriptions.py
