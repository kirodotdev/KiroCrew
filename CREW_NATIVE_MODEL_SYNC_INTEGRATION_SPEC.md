# KiroCrew 0.11 宿主侧模型目录热更新接口设计（尚未落地）

本设计不能由 BYO iframe 的 QueryClient 代替实现。目标为宿主前端、真实模型查询缓存、会话模型选择之间的状态一致性。

## 建议接入方式

在宿主的 React Query Provider 下层挂载专用 `useHostModelCatalogueSync` hook。查询 key 必须从 KiroCrew 0.11.0/rc.5 的真实模型 useQuery 定义获取，**不可臆测为固定的 `['models']`**。当 `/api/models` 的 Type 命名空间从 `tiga` 变为 `dyna`、或从 BYO 变为官方时，对该 key 的活动查询执行失效与重取；使用原本的 UI 状态更新逻辑让菜单重绘。

伪代码（接线指导，**不是可直接打包的补丁**）：

```ts
// Inside KiroCrew's own React Query provider and model state module.
const queryClient = useQueryClient();
const key = ACTUAL_MODEL_QUERY_KEY_FROM_CREW_SOURCE;
const onAuthoritativeCatalogueChanged = async () => {
  await queryClient.invalidateQueries({ queryKey: key });
  await queryClient.refetchQueries({ queryKey: key, type: 'active' });
  // Existing Crew session-model reconciliation should be invoked here
  // only at a safe turn boundary, not during a running agent task.
};
```

Mode 变更的通知可使用 BYO 已发布的 `kiro-byo:catalog-changed` CustomEvent / `kiro-byo:mode-catalog` BroadcastChannel；因跨 WebView 隔离，宿主的 `/api/models` 轮询仍需作为事实校验。事件只是唤醒信号，不可直接信任传入的 Type IDs。

## 验收必须覆盖

- Tiga → Dyna 的三种 Type 顺序与默认 Flash。
- Dyna → Tiga 的四种 Type 顺序与默认 Multi。
- 官方后端 ↔ BYO 后端。
- 未发送草稿保持原样、无黑屏、无整页 reload。
- 运行中不热切模型，完成后再同步；历史消息标签保持原值。
- 服务端 `/api/models` 已改变但 UI 缓存旧，触发查询失效后自动收敛。
- 403/500、模型目录混合、同步失败都不会伪装成成功。
- 不依赖非公开 React Fiber 属性或对 minified/hashed JS 按变量名打补丁。
