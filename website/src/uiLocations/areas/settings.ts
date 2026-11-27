/**
 * Registered UI locations inside Settings tabs: controls a tab draws that the
 * settings extraction does not index (buttons, not settings). One area of
 * `UI_LOCATIONS`; see `../descriptors.ts` for the two-step contract. Ids here
 * never start with `settings.`, a prefix the generator reserves for its tabs.
 */
import type { UiLocationArea } from '../types'

export const LOCATIONS = {
  // Settings > Import / Export, "Back up & restore configuration"
  // (PortabilityTab): downloads a zip of settings, memory, skills and schedules.
  'backup.export': {
    kind: 'button',
    label: { from: 'text', key: 'pages.overview.portabilityTab.download_export_zip' },
    terms: {
      en: [
        'back up my settings', 'backup', 'make a backup', 'export my data', 'export settings',
        'export configuration', 'download a backup', 'backup zip', 'move to a new computer',
      ],
      'zh-CN': ['备份设置', '备份', '导出数据', '导出设置', '导出配置', '下载备份', '迁移到新电脑'],
    },
    placements: [{ surface: 'settings', parent: 'settings.tab.imports', entry: 'content' }],
  },
  // The same card's import half: "Choose file" picks an export zip, then Import
  // (Merge or Replace) restores it.
  'backup.import-file': {
    kind: 'button',
    terms: {
      en: [
        'restore a backup', 'restore from backup', 'import a backup', 'import settings',
        'import configuration', 'restore my settings', 'load a backup zip',
      ],
      'zh-CN': ['恢复备份', '从备份恢复', '导入备份', '导入设置', '导入配置', '还原设置'],
    },
    placements: [{ surface: 'settings', parent: 'settings.tab.imports', entry: 'content' }],
  },
  'memory.more-actions': {
    kind: 'button',
    label: { from: 'attr', attr: 'aria-label', key: 'memoryPage.more_actions' },
    placements: [{ surface: 'settings', parent: 'settings.tab.memory', entry: 'header', requires: [{ kind: 'viewport', value: 'desktop' }] }],
  },
  // Settings > Memory, Test recall in the header's ⋯ menu: what a bot gets
  // at session start and for a typed message.
  'memory.test-recall': {
    kind: 'menu-item',
    label: { from: 'text', key: 'memoryPage.test_recall' },
    terms: {
      en: [
        'memory inspector', 'what does the bot remember', 'preview injected memory',
        'what memory does the bot get', 'context preview', 'what gets injected into the prompt',
      ],
      'zh-CN': ['测试召回', '记忆检查', '机器人记得什么', '预览注入的记忆', '会话开始时的记忆'],
    },
    placements: [{ surface: 'settings', parent: 'memory.more-actions', entry: 'menu' }],
  },
} as const satisfies UiLocationArea
