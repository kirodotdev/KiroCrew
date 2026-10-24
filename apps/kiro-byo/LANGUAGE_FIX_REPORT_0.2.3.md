# Kiro BYO 0.2.3 — 语言一致性修复报告

## 修复范围

以原 `Kiro_BYO_v0.2.3_模式单页布局修复_源码.zip` 为基线，仅优化 BYO 语言处理与语言设置帮助文本。版本号继续保持 `0.2.3`；保留 Tiga 单页布局、Dyna 禁用预置、Provider、Agent、Crew 0.10 集成和其他执行功能。

1. **简繁识别**：区分具有明确字形差异的简体/繁体汉字，排除 `先`、`路`、`案`、`示` 等共享字造成的误报。
2. **短句上下文**：当前轮为无明确简繁字形的中文短句时，继承最近用户输入中已确认的书写体系；纯 `API`、`ACP` 等技术简写优先沿用最近会话语言。
3. **界面语言兜底**：只有用户消息与历史都不足以确定语言时，才参考 BYO 已明确配置的 UI 语言；不会强行将所有聊天改成 UI 的语言。
4. **推理与最终回复**：自动推理语言可跟随用户明确指定的回复语言，手动设置的推理语言仍独立有效。对上游可见 thinking/reasoning summary 与最终自然语言回复明确提出简繁体与语言要求，并延续到子 Agent 和结果复核。
5. **技术文本安全**：不修改隐藏推理、工具参数、JSON、代码、文件路径、原始引文与日志，不在流式阶段机械翻译文本。
6. **设置说明**：更新五种 UI 语言中的“界面语言”“最终回答语言”帮助文案，说明自动模式的优先级与兜底行为。

## 已执行测试

- `pytest -q tests/test_reasoning_language.py tests/test_i18n.py tests/test_acp_control_plane.py tests/test_acp_v3_semantics.py tests/test_turn_runner_regressions.py tests/test_glitter_relay.py`：**121 passed，5 subtests passed**。
- `pytest -q tests/test_management_ui.py tests/test_provider_wire_roundtrips.py tests/test_provider_boundary.py tests/test_session_events.py tests/test_process_session_lifecycle.py tests/test_turn_loop_refactor_contract.py`：**51 passed**。
- `pytest -q tests/test_acp_lifecycle_failures.py::test_stalled_stdout_has_bounded_nonblocking_queue`：单独重跑 **1 passed**；此前在繁忙全量测试运行中曾因 2 秒耗时阈值失败。
- Python compileall：通过。
- **全量测试未完成**：浏览器布局测试在当前执行环境超时，部分计时敏感测试受运行负载影响。不能把原有 701 项测试算作本次全部通过。

## 运行边界

此修复主要是语言检测与 Provider 提示策略增强。某些 Provider 的可见 reasoning/thinking 内容由上游模型自行决定，不保证严格执行语言要求；隐藏的私有推理不能控制或翻译。若希望简体中文最稳定，可在 **Kiro BYO → 设置 → 语言 / 推理** 中同时将“最终回答语言”“可见推理语言”明确设为“简体中文”，界面语言可按偏好独立设置。

由于核心 Runtime 有更新，替换安装包后请完全关闭并重启 KiroCrew，并在新消息中验证。此包**没有执行过用户 Windows 桌面中的真实 KiroCrew 端到端测试**。
