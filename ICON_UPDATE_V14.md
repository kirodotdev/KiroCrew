# V14 — Kiro BYO 应用图标

- 新增顶层 `app.json.iconPath = "assets/icon.png"`，用于 KiroCrew App Store / 已安装应用卡片。旧版只有 `ui.pages[].iconUrl`，它仅对应侧边栏图标，因此应用卡片会使用默认立方体。
- 新增 512×512、不透明 RGB PNG，蓝紫色 BYO 星环助手图案，用星环表示自带 Provider / 模型路由。
- `ui/icon.svg` 更新为同一图形的单色版本，支持浅色及深色主题。
- 发布脚本现包含 `assets/`，并把资产纳入运行时指纹；新增回归测试确保图标尺寸、PNG 格式及配置入口正确。
- 不修改插件 id、路由 / Mode、Provider、ACP 或用户配置。
- 重新通过原安装流程更新 App 后，可能需要刷新 KiroCrew 应用目录缓存或重新启动宿主以看到更新。
