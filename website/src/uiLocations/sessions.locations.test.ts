/**
 * Goldens for the `sessions` area (Sessions sidebar menus and rows, drawn by
 * ChatSidebar): the exact parent chain, entry, label key and requirements each
 * location carries in the committed index.
 */
import { describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'

interface Req { kind: string; value?: string; location?: string; when?: string; id?: string }
interface Placement { surface_id: string; route: string; parent_ids: string[]; entry_kind: string; requires: Req[] }
interface Loc { id: string; kind: string; label_key: string; alias_keys?: string[]; placements: Placement[] }

const INDEX_FILE = path.resolve(__dirname, '../../../src/kiro_crew/docs/ui-index.generated.json')
const index = JSON.parse(fs.readFileSync(INDEX_FILE, 'utf-8')) as { locations: Loc[] }
const byId = new Map(index.locations.map(l => [l.id, l]))
const SIDEBAR_SRC = fs.readFileSync(path.resolve(__dirname, '../pages/ChatSidebar.tsx'), 'utf-8')
/** The row menu's items are drawn by the shared SessionActionsMenu. */
const ROW_MENU_SRC = fs.readFileSync(path.resolve(__dirname, '../components/SessionActionsMenu.tsx'), 'utf-8')

const DESKTOP: Req[] = [
  { kind: 'viewport', value: 'desktop' },
  { kind: 'shown_by', location: 'chat.sessions-sidebar-toggle', when: 'sessions_sidebar_collapsed' },
]
const MOBILE: Req[] = [
  { kind: 'viewport', value: 'mobile' },
  { kind: 'shown_by', location: 'chat.mobile-sessions-toggle', when: 'sessions_drawer_closed' },
]
const cond = (id: string): Req => ({ kind: 'condition', id })

/** Expected [parent chain, entry, requires] per placement. */
type Expect = { kind: string; labelKey: string; aliasKeys?: string[]; placements: [string[], string, Req[]][] }
const sidebarPair = (entry: string, extra: Req[] = []): Expect['placements'] => [
  [['page.chat'], entry, [...DESKTOP, ...extra]],
  [['page.chat'], entry, [...MOBILE, ...extra]],
]
const menuPair = (menu: string, extra: Req[] = []): Expect['placements'] => [
  [['page.chat', menu], 'menu', [...DESKTOP, ...extra]],
  [['page.chat', menu], 'menu', [...MOBILE, ...extra]],
]

const EXPECTED: Record<string, Expect> = {
  // The sidebar region: the picker a guide's "choose the session" step points at.
  'sessions.list': { kind: 'list', labelKey: 'pages.chatSidebar.sessions', placements: sidebarPair('sidebar') },
  'sessions.list-menu': { kind: 'button', labelKey: 'pages.chatSidebar.more_options', placements: sidebarPair('toolbar') },
  'sessions.list-menu.dashboards': { kind: 'menu-item', labelKey: 'commandCenter.all_title', placements: menuPair('sessions.list-menu') },
  'sessions.list-menu.view': {
    kind: 'menu-item', labelKey: 'pages.chatSidebar.switch_to_board_view',
    aliasKeys: ['pages.chatSidebar.switch_to_list_view'], placements: menuPair('sessions.list-menu'),
  },
  'sessions.list-menu.add-lanes': {
    kind: 'menu-item', labelKey: 'pages.chatSidebar.add_state_lanes',
    placements: menuPair('sessions.list-menu', [cond('sessions_board_view'), cond('board_missing_state_lanes')]),
  },
  'sessions.list-menu.clean-up': { kind: 'menu-item', labelKey: 'pages.chatSidebar.clean_up_sessions', placements: menuPair('sessions.list-menu') },
  'sessions.list-menu.switch-model': { kind: 'menu-item', labelKey: 'pages.chatSidebar.switch_all_to_model', placements: menuPair('sessions.list-menu') },
  'sessions.list-menu.manage-tags': { kind: 'menu-item', labelKey: 'pages.chatSidebar.manage_tags', placements: menuPair('sessions.list-menu') },
  'sessions.create-menu': {
    kind: 'button', labelKey: 'pages.chatSidebar.more_create_options',
    aliasKeys: ['pages.chatSidebar.create'], placements: sidebarPair('toolbar'),
  },
  'sessions.create-menu.new-folder': { kind: 'menu-item', labelKey: 'pages.chatSidebar.new_folder', placements: menuPair('sessions.create-menu') },
  'sessions.show-all-older': {
    kind: 'button', labelKey: 'pages.chatSidebar.show_all_older_sessions',
    placements: sidebarPair('sidebar', [cond('older_sessions_collapsed')]),
  },
  'sessions.row-duplicate': {
    kind: 'button', labelKey: 'pages.chatSidebar.duplicate',
    placements: [[['page.chat'], 'sidebar', [...DESKTOP, cond('has_open_sessions'), cond('pointer_on_session_row')]]],
  },
  // Behind the "New ephemeral chat ›" submenu on a wide screen; inline on a phone.
  'sessions.create-menu.ephemeral': {
    kind: 'menu-item', labelKey: 'pages.chatSidebar.new_ephemeral_chat',
    placements: [menuPair('sessions.create-menu')[0]],
  },
  'sessions.create-menu.incognito': {
    kind: 'menu-item', labelKey: 'components.welcomeView.incognito',
    placements: [
      [['page.chat', 'sessions.create-menu', 'sessions.create-menu.ephemeral'], 'menu', [...DESKTOP]],
      menuPair('sessions.create-menu')[1],
    ],
  },
  // The open session's row menu: reached by opening the session, not by a
  // pointer on its row.
  'sessions.row-menu': {
    kind: 'button', labelKey: 'pages.chatSidebar.more_options',
    placements: [[['page.chat'], 'sidebar', [...DESKTOP, cond('has_open_sessions'), cond('session_open')]]],
  },
  'sessions.row-menu.rename': {
    kind: 'menu-item', labelKey: 'components.sessionActionsMenu.rename',
    placements: [[['page.chat', 'sessions.row-menu'], 'menu', [...DESKTOP, cond('has_open_sessions'), cond('session_open')]]],
  },
  'sessions.row-menu.pin': {
    kind: 'menu-item', labelKey: 'components.sessionActionsMenu.pin', aliasKeys: ['components.sessionActionsMenu.unpin'],
    placements: [[['page.chat', 'sessions.row-menu'], 'menu', [...DESKTOP, cond('has_open_sessions'), cond('session_open')]]],
  },
  'sessions.row-close': {
    kind: 'button', labelKey: 'pages.chatSidebar.close_session', aliasKeys: ['pages.chatSidebar.close'],
    placements: [[['page.chat'], 'sidebar', [...DESKTOP, cond('has_open_sessions'), cond('pointer_on_session_row')]]],
  },
}

describe('sessions area locations', () => {
  it('indexes exactly the expected sessions.* ids', () => {
    const ids = index.locations.map(l => l.id).filter(id => id.startsWith('sessions.')).sort()
    expect(ids).toEqual(Object.keys(EXPECTED).sort())
  })

  for (const [id, exp] of Object.entries(EXPECTED)) {
    it(`${id}: label, kind and every placement`, () => {
      const loc = byId.get(id)
      expect(loc, id).toBeDefined()
      expect(loc!.kind).toBe(exp.kind)
      expect(loc!.label_key).toBe(exp.labelKey)
      expect(loc!.alias_keys ?? []).toEqual(exp.aliasKeys ?? [])
      expect(loc!.placements.map(p => [p.parent_ids, p.entry_kind, p.requires])).toEqual(exp.placements)
      for (const p of loc!.placements) {
        expect(p.surface_id).toBe('chat')
        expect(p.route).toBe('/chat')
      }
    })

    it(`${id}: one marker, in ChatSidebar (or the row menu it draws)`, () => {
      const src = id.startsWith('sessions.row-menu.') ? ROW_MENU_SRC : SIDEBAR_SRC
      expect(src.split(`uiLocation('${id}'`).length - 1).toBe(1)
    })
  }

  it('marks the desktop row More options trigger apart from the header menu', () => {
    const rowTriggers = SIDEBAR_SRC.split('\n').filter(l => l.includes("aria-label={i18nT('pages.chatSidebar.more_options')}"))
    expect(rowTriggers.length).toBe(3)
    expect(rowTriggers.filter(l => l.includes("uiLocation('sessions.list-menu')")).length).toBe(1)
    expect(rowTriggers.filter(l => l.includes("uiLocation('sessions.row-menu')")).length).toBe(1)
  })
})
