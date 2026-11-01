# Kiro BYO 0.2.4 · KiroCrew 0.11.0-rc.1 模型显示修复

## 问题

Crew 0.11 将 `kiro-byo-type:tiga:sky` 等原始 ID 用在了模型下拉、会话 Chip、底部状态栏。BYO 0.2.4 的上一候选版为了避免侵入未经审计的前端，暂时跳过了 0.11 的显示补丁。模式和路由本身没有因此被证明出错。

## 本次修复范围

- 模型下拉：`Sky Type` / `Multi Type` / `Power Type` / `Glitter Type`。
- 对话 Chip 与底部：`Tiga · Sky Type` 等。
- Auto、Auto(Jev)、固定模型以及 `data-model-id` 原值：不修改。
- 新增 `crew11_model_labels.py`。安装器在已编译前端存在并证明保有 `data-model-id` + `composer-model-chip` 两处 DOM 合同时，仅对 `static/dist/index.html` 注入受控展示脚本，**不重写任何 hash JS 资源**。
- 和既有 Crew 源码补丁同一事务管理，备份原始 HTML，重复安装幂等，卸载恢复原内容。
- 源码包不携带用户原有数据库、Provider 凭据和完整 Crew 源码。

## 兼容和验收边界

使用 KiroCrew 0.11.0-rc.1 上传源码作 UI 结构核对，并在 Linux 使用合成的已编译前端 + Node 执行的 DOM 语义测试。上传的 Crew 压缩包没有实际 Windows 构建后的 `static/dist` JS，因此尚不能声称你本机 Windows 桌面的可见效果已经通过实测。如果 DOM 字符串不匹配，安装器会拒绝改写前端。普通 ACP、Tiga 策略、Mate 2.0 等运行能力仍按旧报告的限制执行，不因标签修复增加能力。

## 验证

- Crew 精确源码只读审计：37 份源文件、20 处改写点，0 不匹配。
- 专项模型 UI：菜单/Chip/底部显示、用户可见与内部 ID 分离、脚本语法、幂等、未知前端拒绝及 HTML 回滚。
- 全部 Python 测试按 3 个文件组运行；测试结果见总交付包的 `TEST_RESULTS.txt`。
