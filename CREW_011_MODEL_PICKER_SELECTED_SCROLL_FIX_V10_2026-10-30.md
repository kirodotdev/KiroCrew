# Kiro BYO 0.2.5 · Crew 会话模型下拉菜单选中项滚动修复

## 用户报告

Crew 聊天输入框右侧当前实际选中 `mimo-v2.6-pro`（列表靠后），再次通过鼠标打开模型菜单时，滚动视口仍显示较靠前的 DeepSeek / mimo-flash 行，当前模型不可见。

## 源码定位及根因边界

修复范围为 `src/kiro_byo/crew11_model_labels.py`，该文件向 Crew 0.11 前端注入仅作用于会话模型菜单的展示桥接脚本。

需要纠正一个容易误判的点：旧代码**已经**通过设置页共用的 `pointerdown` 处理器调用 `composerTrigger`，并非缺少鼠标监听。现有代码只在打开后的早期布局帧尝试对齐一次，随后立即终止；当 Crew 复用 Portal、异步插入模型行或晚于本次对齐恢复先前 `scrollTop` 时，就可能导致选中具体模型最终不在可视区域。

本地 Chromium DOM 复现确证：

1. 复用已挂载菜单，打开后 110ms 恢复 `scrollTop=0`，旧版定位结果丢失；
2. 复用菜单但在打开后 110ms 才插入末尾具体模型行，旧版没有再次尝试定位。

上述模拟验证说明设计缺口确实存在；但不能仅凭模拟断言用户机器上的具体时序完全一致。

## 修改内容

- 维护打开事件的代次标识，对菜单开启后 60、180、320ms 做有限次数的可见性复核。仍由原有第二帧负责初次定位。
- 当具体模型晚挂载或 Crew 在定位后覆盖滚动位置时，以真实选中 chip 的 ID 或文本寻找菜单行，仅调整该菜单的滚动容器 `scrollTop`。
- 选中 Type 时仍恢复形态分组顶部；不修改具体模型、模式、路由、Picker 点击事件或 Crew 编译 JS。
- 用户在菜单内 `pointerdown`、`wheel`、`touchstart` 或键盘操作后，立即取消后续自动定位，以免抢夺手动滚动。
- 仅在打开窗口内复核，320ms 后关闭自动定位窗口；不启动常驻循环。
- 保留已有 Settings、Mate、官方后端隔离及五语言资源。

## 回归测试

- `tests/test_crew11_picker_scroll_browser.py` 新增：晚期滚动位置恢复、晚期具体模型行挂载、`mimo-v2.6-pro` 末尾模型重新打开定位。
- 自动化测试：全部 136 个测试文件完成；新增测试后共 **1,178 passed, 1 skipped, 58 subtests passed**（第六组执行停滞后拆开分别完成 28 与 48 项）；未发现断言失败。
- 前端测试使用本环境 Chromium 和 Playwright，涉及真实 DOM 几何与滚动；未对 Windows 上的 KiroCrew `0.11.0-rc.5` 执行端到端认证。

## 源码变更边界

仅更改 `src/kiro_byo/crew11_model_labels.py`、`tests/test_crew11_picker_scroll_browser.py`，新增本报告。`BUILD_INFO.json` 将由现有构建脚本生成新的构建指纹，版本保留 `0.2.5`。

## 实机验收建议

在 BYO 后端新会话选中最后一项 `mimo-v2.6-pro`，关闭并多次重新打开下拉列表，确认每次均滚动至该模型且用户能够随后任意手动滚动；切回 `Tiga · Multi Type` 再确认形态分组顶部可见。分别验证鼠标与键盘操作，以及 BYO / 官方后端切换。不需要删除任何 Durable Run 记录。
