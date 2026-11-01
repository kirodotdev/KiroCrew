# Kiro BYO ACP 工具标题五语言兼容修复

## 范围

- `src/kiro_byo/tool_display.py`：新建显示层工具标题生成器。
- `src/kiro_byo/acp_server.py`：在 `tool_call` 的 `title` 写入本地化标题。
- `src/kiro_byo/locales/{zh-CN,zh-TW,en,ja,ko}.json`：五份现有翻译资源同步增加 `tool.title.*` 键。
- 不改动 `rawInput`、`toolCallId`、`kind`、`_meta.kiro`、权限检查、日志身份或实际执行。

## 语言策略

1. BYO **明确指定界面语言**时，ACP 工具展示标题跟随该语言；不以最终回答语言或推理语言决定。
2. 选择 `auto` 时，ACP 无法直接读取 Crew 前端实时语言设置，当前按系统语言回退、无法识别时回退英语。
3. 未识别工具保留原名；翻译资源损坏也不能阻止工具派发。
4. Shell 采用泛化而准确的“执行终端命令”等描述，绝不将未经验证的命令内容误说成只读扫描。MCP 的真正身份仍保留在协议字段。

## 限制

此修复只确保 BYO 发往 Crew 的 ACP `tool_call.title` 符合五语言；Crew RC3 若直接依据 `rawInput` 绘制活动历史，仍可能显示 PowerShell 原始命令，需要 Windows 实际测试和前端适配。不能以本修复宣称官方 Crew 活动历史已经完全本地化。

本次没有更改 Mode / Type / Tiga / Provider 路由或 Crew 源码注入补丁。
