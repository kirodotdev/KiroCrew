/**
 * Goldens for the Crewmates area (`areas/members.ts`) in the committed index.
 */
import { describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'

interface Placement {
  surface_id: string
  route: string
  parent_ids: string[]
  entry_kind: string
  requires: { kind: string; id?: string; flag?: string; location?: string; value?: string; when?: string }[]
}
interface Loc { id: string; kind: string; label_key: string; placements: Placement[]; terms?: Record<string, string[]> }
interface Index { locations: Loc[]; labels: Record<string, Record<string, string>> }

const INDEX_FILE = path.resolve(__dirname, '../../../src/kiro_crew/docs/ui-index.generated.json')
const index = JSON.parse(fs.readFileSync(INDEX_FILE, 'utf-8')) as Index
const byId = new Map(index.locations.map(l => [l.id, l]))

const cond = (id: string) => ({ kind: 'condition', id })
const vp = (value: string) => ({ kind: 'viewport', value })
// An open crewmate chat folds the roster (and its "+" header) away on a wide
// screen; the switcher's "Show the full roster" brings it back.
const ROSTER_SHOWN = { kind: 'shown_by', location: 'members.switcher.show-roster', when: 'crewmate_roster_folded' }
const ROSTER_SHOWN_PHONE = { kind: 'shown_by', location: 'members.back', when: 'crewmate_chat_open_phone' }

// id -> [kind, label key, en label, zh-CN label, parents, entry, route, requires]
const EXPECTED: Record<string, [string, string, string, string, string[], string, string, Placement['requires']]> = {
  // The roster: the picker a guide's "choose the crewmate" step points at.
  'members.roster-list': ['list', 'pages.membersPage.title', 'Crewmates', '队友', ['page.members'], 'content', '/members', []],
  'members.new': ['button', 'pages.membersPage.add_member', 'New crewmate', '新建队友', ['page.members'], 'content', '/members', [cond('no_crewmates')]],
  'members.new-advanced': ['button', 'pages.membersPage.create_advanced', 'Advanced', '高级', ['page.members'], 'content', '/members', [cond('no_crewmates')]],
  'members.add-menu': ['button', 'pages.membersPage.add_menu', 'Add…', '添加…', ['page.members'], 'toolbar', '/members', [cond('has_crewmates'), ROSTER_SHOWN, ROSTER_SHOWN_PHONE]],
  'members.add-menu.new-team': ['menu-item', 'pages.membersPage.team_new', 'New team', '新建团队', ['page.members', 'members.add-menu'], 'menu', '/members', [cond('has_crewmates'), ROSTER_SHOWN, ROSTER_SHOWN_PHONE]],
  'members.back': ['button', 'pages.membersPage.back_to_roster', 'Back to crewmates', '返回队友列表', ['page.members'], 'header', '/members', [vp('mobile'), cond('crewmate_selected')]],
  'members.switcher': ['button', 'pages.membersPage.switch_crewmate', 'Switch crewmate', '切换队友', ['page.members'], 'header', '/members', [vp('desktop'), cond('crewmate_selected')]],
  'members.switcher.show-roster': ['menu-item', 'pages.membersPage.roster_show', 'Show the full roster', '显示完整队友列表', ['page.members', 'members.switcher'], 'menu', '/members', [vp('desktop'), cond('crewmate_selected')]],
  'members.edit': ['button', 'pages.membersPage.profile_card', 'Profile', '资料卡', ['page.members'], 'content', '/members', [cond('crewmate_selected')]],
  // The profile card's Permissions row: a crewmate's tools, behind its card.
  'members.permissions': ['button', 'pages.membersPage.profile_permissions', 'Permissions', '权限', ['page.members'], 'content', '/members', [cond('crewmate_selected'), { kind: 'shown_by', location: 'members.edit', when: 'crewmate_profile_closed' }]],
  'members.details': ['toggle', 'pages.membersPage.panel_toggle', 'Dashboard & files', '仪表板和文件', ['page.members'], 'header', '/members', [cond('crewmate_selected'), cond('crewmate_panel_not_docked')]],
  'agents.add': ['button', 'pages.kiroCrewAgentsPage.add_crew_member', 'New crewmate', '新建队友', ['page.capabilities', 'tab.capabilities.crews'], 'toolbar', '/capabilities?tab=crews', []],
  'agents.create-first': ['button', 'pages.kiroCrewAgentsPage.create_your_first_crew', 'Create your first crewmate', '创建你的第一位队友', ['page.capabilities', 'tab.capabilities.crews'], 'content', '/capabilities?tab=crews', [cond('no_crewmates')]],
  'agents.edit-avatar': ['button', 'components.avatarBuilder.edit_avatar', 'Edit avatar', '编辑头像', ['page.capabilities', 'tab.capabilities.crews'], 'header', '/capabilities?tab=crews', [cond('crewmate_editor_open')]],
  'agents.delete': ['button', 'pages.kiroCrewAgentsPage.delete_crew', 'Delete crewmate', '删除队友', ['page.capabilities', 'tab.capabilities.crews'], 'content', '/capabilities?tab=crews', [cond('crewmate_editor_open'), cond('crewmate_danger_zone_open')]],
  'agents.chat': ['button', 'memoryV2.chat_member', 'Chat with this crewmate', '与这位队友聊天', ['page.capabilities', 'tab.capabilities.crews'], 'header', '/capabilities?tab=crews', [cond('crewmate_editor_open')]],
  'agents.manage-memory': ['button', 'pages.kiroCrewAgentsPage.manage_private_memory', 'Manage memory', '管理记忆', ['page.capabilities', 'tab.capabilities.crews'], 'content', '/capabilities?tab=crews', [cond('crewmate_editor_open'), cond('crewmate_place_pane_open'), cond('crewmate_memory_manageable')]],
}

describe('the Crewmates area in the index', () => {
  it('registers exactly the expected Crewmates locations', () => {
    const ours = index.locations.map(l => l.id).filter(id => id.startsWith('members.') || id.startsWith('agents.')).sort()
    expect(ours).toEqual(Object.keys(EXPECTED).sort())
  })

  for (const [id, [kind, key, en, zh, parents, entry, route, requires]] of Object.entries(EXPECTED)) {
    it(`${id}: one placement with its exact path, label and requirements`, () => {
      const loc = byId.get(id)!
      expect(loc.kind).toBe(kind)
      expect(loc.label_key).toBe(key)
      expect(index.labels.en[key]).toBe(en)
      expect(index.labels['zh-CN'][key]).toBe(zh)
      expect(loc.placements).toHaveLength(1)
      const [p] = loc.placements
      expect(p.parent_ids).toEqual(parents)
      expect(p.entry_kind).toBe(entry)
      expect(p.route).toBe(route)
      expect(p.requires).toEqual(requires)
    })
  }

  it('carries newcomer terms in English and Chinese on every location but the bare menu and the picker', () => {
    for (const id of Object.keys(EXPECTED)) {
      // The roster picker is guide machinery: find_ui never ranks it.
      if (id === 'members.add-menu' || id === 'members.roster-list') continue
      const terms = byId.get(id)!.terms!
      expect(terms.en.length, id).toBeGreaterThan(0)
      expect(terms['zh-CN'].length, id).toBeGreaterThan(0)
    }
    expect(byId.get('members.new')!.terms!['zh-CN']).toContain('创建代理')
    expect(byId.get('members.add-menu')!.terms!.en).toContain('advanced crewmate setup')
  })

  it("marks agents.delete on the Danger zone's unarmed delete only, never the armed confirm", () => {
    const page = fs.readFileSync(path.resolve(__dirname, '../pages/KiroCrewAgentsPage.tsx'), 'utf-8')
    expect(page.split("uiLocation('agents.delete')").length - 1).toBe(1)
    const confirm = page.split('\n').find(l => l.includes('data-testid="confirm-delete-crew"'))!
    expect(confirm).not.toContain('uiLocation(')
  })

  it('binds no guide: crewmate.create anchors inside the create dialog, not on these buttons', () => {
    for (const id of Object.keys(EXPECTED)) expect((byId.get(id) as { guide_ref?: unknown }).guide_ref).toBeUndefined()
  })
})
