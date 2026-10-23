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
// Crewmates is still a preview: every control on the page names the switch.
const CREW_PREVIEW = { kind: 'preview_flag', flag: 'mc-preview-crew', location: 'setting:developer.mates' }

// id -> [kind, label key, en label, zh-CN label, parents, entry, route, requires]
const EXPECTED: Record<string, [string, string, string, string, string[], string, string, Placement['requires']]> = {
  // The roster: the picker a guide's "choose the crewmate" step points at.
  'members.roster-list': ['list', 'pages.membersPage.title', 'Mates', '队友', ['page.members'], 'content', '/members', [CREW_PREVIEW]],
  'members.new': ['button', 'pages.membersPage.add_member', 'New mate', '新建队友', ['page.members'], 'content', '/members', [CREW_PREVIEW, cond('no_crewmates')]],
  'members.new-advanced': ['button', 'pages.membersPage.create_advanced', 'Advanced', '高级', ['page.members'], 'content', '/members', [CREW_PREVIEW, cond('no_crewmates')]],
  'members.empty.import': ['button', 'pages.membersPage.import_from_file', 'Import from file', '从文件导入', ['page.members'], 'content', '/members', [CREW_PREVIEW, cond('no_crewmates')]],
  'members.add-menu': ['button', 'pages.membersPage.add_menu', 'Add…', '添加…', ['page.members'], 'toolbar', '/members', [CREW_PREVIEW, cond('has_crewmates'), ROSTER_SHOWN, ROSTER_SHOWN_PHONE]],
  // Creating once the roster has a crewmate: the header "+" menu's first item, and the switcher list's last.
  'members.add-menu.new': ['menu-item', 'pages.membersPage.add_member', 'New mate', '新建队友', ['page.members', 'members.add-menu'], 'menu', '/members', [CREW_PREVIEW, cond('has_crewmates'), ROSTER_SHOWN, ROSTER_SHOWN_PHONE]],
  'members.switcher.new': ['menu-item', 'pages.membersPage.add_member', 'New mate', '新建队友', ['page.members', 'members.switcher'], 'menu', '/members', [CREW_PREVIEW, vp('desktop'), cond('crewmate_selected')]],
  'members.back': ['button', 'pages.membersPage.toggle_roster', 'Toggle mates', '切换队友列表', ['page.members'], 'header', '/members', [CREW_PREVIEW, vp('mobile'), cond('crewmate_selected')]],
  'members.switcher': ['button', 'pages.membersPage.switch_crewmate', 'Switch mate', '切换队友', ['page.members'], 'header', '/members', [CREW_PREVIEW, vp('desktop'), cond('crewmate_selected')]],
  'members.add-menu.import': ['menu-item', 'pages.membersPage.import_from_file', 'Import from file', '从文件导入', ['page.members', 'members.add-menu'], 'menu', '/members', [CREW_PREVIEW, cond('has_crewmates'), ROSTER_SHOWN, ROSTER_SHOWN_PHONE]],
  'members.switcher.import': ['menu-item', 'pages.membersPage.import_from_file', 'Import from file', '从文件导入', ['page.members', 'members.switcher'], 'menu', '/members', [CREW_PREVIEW, vp('desktop'), cond('crewmate_selected')]],
  'members.switcher.show-roster': ['menu-item', 'pages.membersPage.roster_show', 'Show the full roster', '显示完整队友列表', ['page.members', 'members.switcher'], 'menu', '/members', [CREW_PREVIEW, vp('desktop'), cond('crewmate_selected')]],
  // Under the switcher chip: one pill per crewmate with an unread message, then a "+N" pill opening the list (#19045).
  'members.switcher.unread': ['button', 'pages.membersPage.unread_message', 'Unread message', '未读消息', ['page.members'], 'header', '/members', [CREW_PREVIEW, vp('desktop'), cond('crewmate_selected')]],
  'members.switcher.unread-more': ['button', 'pages.membersPage.switch_crewmate', 'Switch mate', '切换队友', ['page.members'], 'header', '/members', [CREW_PREVIEW, vp('desktop'), cond('crewmate_selected')]],
  'members.edit': ['button', 'pages.membersPage.profile_card', 'Profile', '资料卡', ['page.members'], 'content', '/members', [CREW_PREVIEW, cond('crewmate_selected')]],
  // The profile card's Profile tab (the card opens on Sessions): the step before the Settings row.
  'members.profile-tab': ['tab', 'pages.membersPage.profile_tab', 'Profile', '资料', ['page.members'], 'content', '/members', [CREW_PREVIEW, cond('crewmate_selected'), { kind: 'shown_by', location: 'members.edit', when: 'crewmate_profile_closed' }]],
  // The profile card's Settings row: the door to the crewmate's own settings page, behind its card's Profile tab.
  'members.settings': ['button', 'pages.membersPage.profile_settings', 'Settings', '设置', ['page.members'], 'content', '/members', [CREW_PREVIEW, cond('crewmate_selected'), { kind: 'shown_by', location: 'members.edit', when: 'crewmate_profile_closed' }, { kind: 'shown_by', location: 'members.profile-tab', when: 'crewmate_profile_tab_closed' }]],
  // The Settings page's Permissions row: a crewmate's tools, one more push in.
  'members.permissions': ['button', 'pages.membersPage.profile_permissions', 'Permissions', '权限', ['page.members'], 'content', '/members', [CREW_PREVIEW, cond('crewmate_selected'), { kind: 'shown_by', location: 'members.edit', when: 'crewmate_profile_closed' }, { kind: 'shown_by', location: 'members.profile-tab', when: 'crewmate_profile_tab_closed' }, { kind: 'shown_by', location: 'members.settings', when: 'crewmate_settings_closed' }]],
  'members.details': ['toggle', 'pages.membersPage.panel_toggle', 'Dashboard & files', '仪表板和文件', ['page.members'], 'header', '/members', [CREW_PREVIEW, cond('crewmate_selected'), cond('crewmate_panel_not_docked')]],
  'agents.add': ['button', 'pages.kiroCrewAgentsPage.add_crew_member', 'New mate', '新建队友', ['page.capabilities', 'tab.capabilities.crews'], 'toolbar', '/capabilities?tab=crews', []],
  'agents.create-first': ['button', 'pages.kiroCrewAgentsPage.create_your_first_crew', 'Create your first mate', '创建你的第一位队友', ['page.capabilities', 'tab.capabilities.crews'], 'content', '/capabilities?tab=crews', [cond('no_crewmates')]],
  'agents.edit-avatar': ['button', 'components.avatarBuilder.edit_avatar', 'Edit avatar', '编辑头像', ['page.capabilities', 'tab.capabilities.crews', 'agents.section-model'], 'tab', '/capabilities?tab=crews', [cond('crewmate_editor_open')]],
  'agents.delete': ['button', 'pages.kiroCrewAgentsPage.delete_crew', 'Delete mate', '删除队友', ['page.capabilities', 'tab.capabilities.crews', 'agents.section-danger'], 'tab', '/capabilities?tab=crews', [cond('crewmate_editor_open')]],
  'agents.crew-list': ['list', 'pages.kiroCrewAgentsPage.agents', 'Agents', '代理', ['page.capabilities', 'tab.capabilities.crews'], 'content', '/capabilities?tab=crews', []],
  'agents.model': ['field', 'pages.kiroCrewAgentsPage.edit_model', 'Edit default model', '编辑默认模型', ['page.capabilities', 'tab.capabilities.crews', 'agents.section-model'], 'tab', '/capabilities?tab=crews', [cond('crewmate_editor_open')]],
  // The editor's section rail rows a guide opens on the way (CrewEditorNextRail).
  'agents.section-model': ['tab', 'crewEditorNext.section_general', 'General', '常规', ['page.capabilities', 'tab.capabilities.crews'], 'content', '/capabilities?tab=crews', [cond('crewmate_editor_open')]],
  'agents.section-danger': ['tab', 'crewEditorNext.section_danger', 'Danger Zone', '危险区域', ['page.capabilities', 'tab.capabilities.crews'], 'content', '/capabilities?tab=crews', [cond('crewmate_editor_open')]],
}

describe('the Mates area in the index', () => {
  it('registers exactly the expected Mates locations', () => {
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
      // The pickers are guide machinery: find_ui never ranks them.
      if (id === 'members.add-menu' || id === 'members.roster-list' || id === 'agents.crew-list' || id.startsWith('agents.section-')) continue
      const terms = byId.get(id)!.terms!
      expect(terms.en.length, id).toBeGreaterThan(0)
      expect(terms['zh-CN'].length, id).toBeGreaterThan(0)
    }
    expect(byId.get('members.new')!.terms!['zh-CN']).toContain('创建代理')
    expect(byId.get('members.add-menu')!.terms!.en).toContain('advanced crewmate setup')
  })

  it("marks agents.delete on the Danger zone's unarmed delete only, never the armed confirm", () => {
    const editor = fs.readFileSync(path.resolve(__dirname, '../components/crew/CrewEditorNextDialog.tsx'), 'utf8')
    expect(editor.split("uiLocation('agents.delete')").length - 1).toBe(1)
    const confirm = editor.split('\n').find(l => l.includes('data-testid="next-confirm-delete-crew"'))!
    expect(confirm).not.toContain('uiLocation(')
  })

  it('binds no guide: crewmate.create anchors inside the create dialog, not on these buttons', () => {
    for (const id of Object.keys(EXPECTED)) expect((byId.get(id) as { guide_ref?: unknown }).guide_ref).toBeUndefined()
  })
})
