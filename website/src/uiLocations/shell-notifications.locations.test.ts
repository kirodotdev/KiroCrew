/**
 * Goldens for the `shell` and `notifications` areas (the rail's bottom rows,
 * the bell, focus mode, and the notification feed + detail panel): the exact
 * parent chain, entry, route, label key and requirements each location carries
 * in the committed index, plus the render site each marker sits on.
 */
import { describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'

interface Req { kind: string; value?: string; location?: string; when?: string; id?: string }
interface Placement { surface_id: string; route: string; parent_ids: string[]; entry_kind: string; requires: Req[] }
interface Loc { id: string; kind: string; label_key: string; alias_keys?: string[]; terms?: Record<string, string[]>; placements: Placement[] }

const INDEX_FILE = path.resolve(__dirname, '../../../src/kiro_crew/docs/ui-index.generated.json')
const index = JSON.parse(fs.readFileSync(INDEX_FILE, 'utf-8')) as { locations: Loc[]; labels: Record<string, Record<string, string>> }
const byId = new Map(index.locations.map(l => [l.id, l]))
const src = (rel: string) => fs.readFileSync(path.resolve(__dirname, rel), 'utf-8')
const APP_SRC = src('../App.tsx')
const FEED_SRC = src('../components/notifications/NotificationFeed.tsx')
const DETAIL_SRC = src('../components/notifications/NotificationDetailPanel.tsx')
const TERMINAL_SRC = src('../components/BottomTerminalPanel.tsx')
// The shell's rail chrome and notification sheet moved out of App.tsx.
const RAIL_SRC = src('../shell/nav/railChrome.tsx')
const SHEET_SRC = src('../shell/notifications/notificationSheet.tsx')

const cond = (id: string): Req => ({ kind: 'condition', id })
const vp = (value: 'desktop' | 'mobile'): Req => ({ kind: 'viewport', value })
const MENU_GATES: Req[] = [vp('mobile'), cond('not_on_sessions_page')]

type P = { surface: string; route: string; parents: string[]; entry: string; requires: Req[] }
const shell = (entry: string, parents: string[], requires: Req[]): P => ({ surface: 'shell', route: '', parents, entry, requires })
const railAndMenu = (gate: string): P[] => [
  shell('rail', [], [vp('desktop'), cond(gate)]),
  shell('menu', ['shell.mobile-menu'], [...MENU_GATES, cond(gate)]),
]
const TERMINAL_OPEN: Req[] = [
  cond('terminal_enabled'), cond('terminal_not_popped_out'),
  { kind: 'shown_by', location: 'shell.terminal', when: 'terminal_panel_closed' },
]
const page = (entry: string, requires: Req[]): P => ({ surface: 'notifications', route: '/notifications', parents: ['page.notifications'], entry, requires })

type Expect = { kind: string; labelKey: string; aliasKeys?: string[]; source: string; en: string; zh: string; placements: P[] }

const EXPECTED: Record<string, Expect> = {
  'shell.developer': { kind: 'button', labelKey: 'app.developer', source: APP_SRC, en: 'Developer', zh: '开发者', placements: railAndMenu('developer_mode') },
  'shell.terminal': { kind: 'toggle', labelKey: 'app.terminal', source: APP_SRC, en: 'Terminal', zh: '终端', placements: railAndMenu('terminal_enabled') },
  // The docked terminal panel's ⋯ menu and its dock-position item (side by side).
  'shell.terminal-more': {
    kind: 'button', labelKey: 'components.bottomTerminalPanel.more_actions', source: TERMINAL_SRC, en: 'More actions', zh: '更多操作',
    placements: [shell('toolbar', [], TERMINAL_OPEN)],
  },
  'shell.terminal-more.position': {
    kind: 'menu-item', labelKey: 'components.bottomTerminalPanel.move_panel_to_right',
    aliasKeys: ['components.bottomTerminalPanel.move_panel_to_bottom'], source: TERMINAL_SRC, en: 'Move panel to right', zh: '将面板移至右侧',
    placements: [shell('menu', ['shell.terminal-more'], TERMINAL_OPEN)],
  },
  'shell.connect-phone': { kind: 'button', labelKey: 'app.connect_your_phone', source: APP_SRC, en: 'Connect your phone', zh: '连接手机', placements: railAndMenu('phone_connect_available') },
  'shell.kiro-account': {
    kind: 'button', labelKey: 'components.kiroAccountModal.kiro_account', source: APP_SRC, en: 'Kiro Account', zh: 'Kiro 账户',
    placements: [shell('menu', ['shell.mobile-menu'], [...MENU_GATES, cond('kiro_account_entry')])],
  },
  'shell.report-problem': {
    kind: 'button', labelKey: 'app.report_issue', aliasKeys: ['app.report_a_problem_with_diagnostics'], source: RAIL_SRC, en: 'Report issue', zh: '反馈问题',
    placements: [
      // Desktop: the rail row folds away while the rail is collapsed.
      shell('rail', [], [vp('desktop'), { kind: 'shown_by', location: 'shell.nav-toggle', when: 'nav_rail_collapsed' }]),
      shell('menu', ['shell.mobile-menu'], MENU_GATES),
    ],
  },
  'shell.nav-toggle': {
    kind: 'toggle', labelKey: 'app.expand_sidebar', aliasKeys: ['app.collapse_sidebar'], source: RAIL_SRC, en: 'Expand sidebar', zh: '展开侧边栏',
    placements: [shell('rail', [], [vp('desktop')])],
  },
  'shell.notifications': {
    kind: 'button', labelKey: 'app.notifications', source: APP_SRC, en: 'Notifications', zh: '通知',
    placements: [shell('header', [], [])],
  },
  'shell.notifications.open-inbox': {
    kind: 'link', labelKey: 'app.open_inbox', aliasKeys: ['app.open_the_full_inbox'], source: SHEET_SRC, en: 'Open inbox', zh: '打开收件箱',
    placements: [shell('menu', ['shell.notifications'], [])],
  },
  'shell.focus-mode': {
    kind: 'toggle', labelKey: 'app.focus_mode', source: APP_SRC, en: 'Focus mode', zh: '专注模式',
    placements: [shell('header', [], [vp('desktop')])],
  },
  'notifications.mark-all-read': {
    kind: 'button', labelKey: 'components.notifications.notificationFeed.mark_all_as_read', source: FEED_SRC, en: 'Mark all as read', zh: '全部标为已读',
    placements: [shell('menu', ['shell.notifications'], [cond('has_unread_notifications')])],
  },
  'notifications.clear-all': {
    kind: 'button', labelKey: 'components.notifications.notificationFeed.clear_all_notifications', source: FEED_SRC, en: 'Clear all notifications?', zh: '清除所有通知？',
    placements: [shell('menu', ['shell.notifications'], [cond('has_notifications')])],
  },
  'notifications.page-mark-all-read': {
    kind: 'button', labelKey: 'components.notifications.notificationFeed.all', aliasKeys: ['components.notifications.notificationFeed.mark_all_as_read'],
    source: FEED_SRC, en: 'All', zh: '全部', placements: [page('toolbar', [cond('has_unread_notifications')])],
  },
  'notifications.page-clear-all': {
    kind: 'button', labelKey: 'components.notifications.notificationFeed.clear', aliasKeys: ['components.notifications.notificationFeed.clear_all_notifications'],
    source: FEED_SRC, en: 'Clear', zh: '清除', placements: [page('toolbar', [cond('has_notifications')])],
  },
  'notifications.detail.mark-unread': {
    kind: 'button', labelKey: 'components.notifications.notificationDetailPanel.mark_unread', source: DETAIL_SRC, en: 'Mark unread', zh: '标为未读',
    placements: [
      shell('content', ['shell.notifications'], [cond('notification_selected'), cond('notification_read')]),
      page('content', [cond('notification_selected'), cond('notification_read')]),
    ],
  },
  'notifications.mute-channel': {
    kind: 'button', labelKey: 'components.notifications.notificationFeed.mute_channel', source: FEED_SRC, en: 'Mute channel', zh: '静音频道',
    placements: [
      shell('content', ['shell.notifications'], [cond('new_channel_prompt')]),
      page('content', [cond('new_channel_prompt')]),
    ],
  },
}

/** The four shell ids that predate this batch; they keep their own goldens elsewhere. */
const PRE_EXISTING_SHELL = ['shell.menu-search', 'shell.mobile-menu', 'shell.search']

describe('shell and notifications area locations', () => {
  it('indexes exactly the expected shell.* and notifications.* ids', () => {
    const ids = index.locations.map(l => l.id)
      .filter(id => (id.startsWith('shell.') || id.startsWith('notifications.')) && !PRE_EXISTING_SHELL.includes(id))
      .sort()
    expect(ids).toEqual(Object.keys(EXPECTED).sort())
  })

  for (const [id, e] of Object.entries(EXPECTED)) {
    it(`${id}: label, aliases, placements and render site`, () => {
      const loc = byId.get(id)
      expect(loc, id).toBeDefined()
      expect(loc!.kind).toBe(e.kind)
      expect(loc!.label_key).toBe(e.labelKey)
      expect(index.labels.en[e.labelKey]).toBe(e.en)
      expect(index.labels['zh-CN'][e.labelKey]).toBe(e.zh)
      expect(loc!.alias_keys ?? []).toEqual(e.aliasKeys ?? [])
      expect(loc!.placements.map(p => ({
        surface: p.surface_id, route: p.route, parents: p.parent_ids, entry: p.entry_kind, requires: p.requires,
      }))).toEqual(e.placements)
      // Exactly one marker, in the render file this batch owns.
      const marker = `uiLocation('${id}'`
      expect(e.source.split(marker).length - 1, id).toBe(1)
      for (const other of [APP_SRC, FEED_SRC, DETAIL_SRC, TERMINAL_SRC, RAIL_SRC, SHEET_SRC].filter(s => s !== e.source)) {
        expect(other.includes(marker), id).toBe(false)
      }
      // Every location carries newcomer terms in English and Chinese.
      expect(loc!.terms?.en?.length, id).toBeGreaterThan(0)
      expect(loc!.terms?.['zh-CN']?.length, id).toBeGreaterThan(0)
    })
  }

  it('marks the bell popover footer, not the crash fallback, as Open inbox', () => {
    const footer = SHEET_SRC.indexOf("{...uiLocation('shell.notifications.open-inbox')}")
    expect(SHEET_SRC.indexOf("{i18nT('app.open_inbox')}", footer) - footer).toBeLessThan(200)
  })

  it('marks the bell feed on the mac variant and the page feed on the panel variant', () => {
    const line = (id: string) => FEED_SRC.split('\n').find(l => l.includes(`uiLocation('${id}'`)) ?? ''
    expect(line('notifications.page-mark-all-read')).toContain('!mac && unread > 0')
    expect(line('notifications.page-clear-all')).toContain('!mac && items.length > 0')
  })
})
