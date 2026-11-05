import { afterEach, describe, expect, it } from 'vitest'

import {
  DEFAULT_RAIL_LAYOUT,
  RAIL_LAYOUT_KEY,
  addGroup,
  applyVisibleOrder,
  commitRail,
  insertPin,
  isEntityPin,
  matePinId,
  removePin,
  sessionPinId,
  setRailLabel,
  moveGroup,
  moveToGroup,
  parseRailLayout,
  removeGroup,
  renameGroup,
  resetRailLayout,
  readRailLayout,
  resolveRailLayout,
  type RailGroup,
  type RailLayout,
} from './railLayout'
import { railGroupHeaderId as H, regroupFlat } from '../shell/nav/railGroups'

const ALL = ['chat', 'members', 'schedule', 'artifacts', 'apps', 'apps-library']
const layout = (groups: RailLayout['groups'], hidden: string[] = []): RailLayout => ({ version: 1, groups, hidden })

afterEach(() => localStorage.clear())

describe('railLayout', () => {
  it('falls back to the shipped default when nothing is stored', () => {
    expect(readRailLayout()).toEqual(DEFAULT_RAIL_LAYOUT)
    expect(resolveRailLayout(readRailLayout(), ALL).map(g => g.items)).toEqual([
      ['chat', 'members', 'schedule', 'artifacts'], ['apps', 'apps-library'],
    ])
  })

  it('refuses malformed or unknown-version values instead of throwing', () => {
    for (const raw of ['{', '[]', '{"version":2,"groups":[{"id":"a","items":[]}]}', '{"version":1,"groups":[]}']) {
      expect(parseRailLayout(raw)).toBeNull()
    }
    localStorage.setItem(RAIL_LAYOUT_KEY, 'not json')
    expect(readRailLayout()).toEqual(DEFAULT_RAIL_LAYOUT)
  })

  it('keeps the first occurrence of a duplicated group or item', () => {
    const parsed = parseRailLayout(JSON.stringify(layout([
      { id: 'a', items: ['chat', 'chat', 7 as unknown as string] },
      { id: 'a', items: ['members'] },
      { id: 'b', items: ['chat', 'schedule'] },
    ])))
    expect(parsed?.groups).toEqual([{ id: 'a', items: ['chat'] }, { id: 'b', items: ['schedule'] }])
  })

  it('skips unavailable ids and places unlisted ones in their default group', () => {
    const stored = layout([{ id: 'apps', items: ['apps', 'gone'] }, { id: 'workspace', items: ['schedule'] }])
    expect(resolveRailLayout(stored, ALL)).toEqual([
      { id: 'apps', items: ['apps', 'apps-library'] },
      { id: 'workspace', items: ['schedule', 'chat', 'members', 'artifacts'] },
    ])
  })

  it('sends an unlisted id to the last group when its default group was removed', () => {
    expect(resolveRailLayout(layout([{ id: 'mine', items: [] }]), ['chat'])).toEqual([{ id: 'mine', items: ['chat'] }])
  })

  it('places a never-listed app in the group the caller names, in available order', () => {
    const groupOf = (id: string) => (id.startsWith('app:') ? 'apps' : 'workspace')
    const groups = resolveRailLayout(DEFAULT_RAIL_LAYOUT, [...ALL, 'app:b', 'app:a'], groupOf)
    expect(groups[1].items).toEqual(['apps', 'apps-library', 'app:b', 'app:a'])
  })

  it('never shows a hidden id', () => {
    const groups = resolveRailLayout(layout(DEFAULT_RAIL_LAYOUT.groups, ['members']), ALL)
    expect(groups.flatMap(g => g.items)).not.toContain('members')
  })

  it('keeps an unavailable id in its stored slot through a reorder', () => {
    const stored = layout([{ id: 'workspace', items: ['chat', 'webhooks', 'members'] }])
    const next = applyVisibleOrder(stored, [{ id: 'workspace', items: ['members', 'chat'] }])
    expect(next.groups[0].items).toEqual(['members', 'webhooks', 'chat'])
  })
})

describe('regroupFlat', () => {
  const groups = [{ id: 'workspace', items: ['chat', 'members'] }, { id: 'apps', items: ['apps'] }]

  it('moves a row across groups by its position relative to the headers', () => {
    const flat = [H('workspace'), 'chat', H('apps'), 'members', 'apps']
    expect(regroupFlat(flat, groups).map(g => g.items)).toEqual([['chat'], ['members', 'apps']])
  })

  it('puts a row dropped above the first header into the first group', () => {
    const flat = ['apps', H('workspace'), 'chat', 'members', H('apps')]
    expect(regroupFlat(flat, groups).map(g => g.items)).toEqual([['apps', 'chat', 'members'], []])
  })
})

describe('group edits', () => {
  const g = (): RailGroup[] => [
    { id: 'workspace', items: ['chat', 'members'] },
    { id: 'apps', items: ['apps'] },
  ]

  it('moves a row to the end of another group', () => {
    expect(moveToGroup(g(), 'chat', 'apps').map(x => x.items)).toEqual([['members'], ['apps', 'chat']])
  })

  it('adds a group with an unused id', () => {
    const { groups, groupId } = addGroup([...g(), { id: 'group-3', items: [] }], '')
    expect(groupId).toBe('group-4')
    expect(groups.at(-1)).toEqual({ id: 'group-4', title: '', items: [] })
  })

  it('folds a removed group into the one above it, and refuses to remove the last group', () => {
    expect(removeGroup(g(), 'apps')).toEqual([{ id: 'workspace', items: ['chat', 'members', 'apps'] }])
    expect(removeGroup(g(), 'workspace')).toEqual([{ id: 'apps', items: ['chat', 'members', 'apps'] }])
    expect(removeGroup([g()[0]], 'workspace')).toEqual([g()[0]])
  })

  it('reorders groups and clears a blank rename back to the shipped title', () => {
    expect(moveGroup(g(), 'apps', -1).map(x => x.id)).toEqual(['apps', 'workspace'])
    expect(moveGroup(g(), 'apps', 1).map(x => x.id)).toEqual(['workspace', 'apps'])
    expect(renameGroup(g(), 'apps', '  Tools ')[1].title).toBe('Tools')
    expect(renameGroup(g(), 'apps', '   ')[1].title).toBeUndefined()
  })

  it('unpins into the stored hidden set and keeps the slot for a later re-pin', () => {
    commitRail([{ id: 'workspace', items: ['chat'] }, { id: 'apps', items: ['apps'] }], { add: 'members' })
    expect(readRailLayout().hidden).toEqual(['members'])
    expect(readRailLayout().groups[0].items.slice(0, 2)).toEqual(['chat', 'members'])
    commitRail(resolveRailLayout(readRailLayout(), ['chat', 'members', 'apps']), { remove: 'members' })
    expect(resolveRailLayout(readRailLayout(), ['chat', 'members', 'apps'])[0].items).toEqual(['chat', 'members'])
  })

  it('resets to the shipped default', () => {
    commitRail([{ id: 'x', items: [] }])
    resetRailLayout()
    expect(readRailLayout()).toEqual(DEFAULT_RAIL_LAYOUT)
  })
})

describe('rail pins and rail-only names', () => {
  const base = (): RailLayout => ({
    version: DEFAULT_RAIL_LAYOUT.version,
    groups: [{ id: 'workspace', items: ['chat', 'members'] }, { id: 'apps', items: ['apps'] }],
    hidden: [],
  })

  it('names entity pins by prefix', () => {
    expect(sessionPinId('dash-1')).toBe('session:dash-1')
    expect(matePinId('kiro')).toBe('mate:kiro')
    expect(isEntityPin('session:dash-1') && isEntityPin('mate:kiro')).toBe(true)
    expect(isEntityPin('chat')).toBe(false)
  })

  it('inserts a pin before a row, at a group end, and moves an existing pin', () => {
    const a = insertPin(base(), 'session:s1', 'workspace', 'members')
    expect(a.groups[0].items).toEqual(['chat', 'session:s1', 'members'])
    const b = insertPin(a, 'session:s1', 'apps', null)
    expect(b.groups[0].items).toEqual(['chat', 'members'])
    expect(b.groups[1].items).toEqual(['apps', 'session:s1'])
    // An unknown group falls back to the first; a missing beforeId appends.
    expect(insertPin(base(), 'mate:m', 'nope', 'gone').groups[0].items).toEqual(['chat', 'members', 'mate:m'])
  })

  it('pinning a hidden row un-hides it', () => {
    expect(insertPin({ ...base(), hidden: ['schedule'] }, 'schedule', 'workspace', null).hidden).toEqual([])
  })

  it('removes a pin outright with its label, without hiding it', () => {
    const pinned = setRailLabel(insertPin(base(), 'mate:m', 'apps', null), 'mate:m', 'Release')
    const out = removePin(pinned, 'mate:m')
    expect(out.groups.flatMap(g => g.items)).not.toContain('mate:m')
    expect(out.hidden).toEqual([])
    expect(out.labels).toBeUndefined()
  })

  it('sets, trims and clears a rail-only name', () => {
    const named = setRailLabel(base(), 'session:s1', '  Standup  ')
    expect(named.labels).toEqual({ 'session:s1': 'Standup' })
    expect(setRailLabel(named, 'session:s1', '   ').labels).toBeUndefined()
    expect(setRailLabel(named, 'session:s1', null).labels).toBeUndefined()
  })

  it('round-trips labels through storage and drops malformed ones', () => {
    const raw = JSON.stringify({ ...base(), labels: { 'session:s1': 'Standup', bad: 3, empty: ' ' } })
    expect(parseRailLayout(raw)?.labels).toEqual({ 'session:s1': 'Standup' })
    expect(parseRailLayout(JSON.stringify(base()))?.labels).toBeUndefined()
  })

  it('keeps labels through a reorder commit', () => {
    localStorage.setItem(RAIL_LAYOUT_KEY, JSON.stringify(setRailLabel(base(), 'chat', 'Chats')))
    commitRail([{ id: 'workspace', items: ['members', 'chat'] }, { id: 'apps', items: ['apps'] }])
    expect(readRailLayout().labels).toEqual({ chat: 'Chats' })
  })
})
