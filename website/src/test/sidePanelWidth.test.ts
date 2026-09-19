/**
 * The side panel's per-kind size buckets (`pages/chat/sidePanelWidth.ts`).
 *
 * Two contracts a refactor breaks silently:
 *
 * 1. The mapping is TOTAL over `TabKind` and collides only where two kinds are
 *    deliberately one reading shape. A kind that fell through to a shared bucket
 *    by accident would make one tab's drag move another's width — the defect the
 *    grouping exists to remove.
 * 2. A group with no stored size of its own inherits the pre-grouping key before
 *    the built-in default. That is the whole upgrade path: without it, every
 *    existing install's panel snaps to 460 on first launch after the change.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import type { TabKind } from '../hooks/usePanelTabs'
import {
  SIDE_PANEL_DEFAULT_GROUP,
  SIDE_PANEL_HEIGHT_KEY,
  SIDE_PANEL_WIDTH_KEY,
  loadSidePanelDim,
  sidePanelDimKey,
  sidePanelWidthGroup,
} from '../pages/chat/sidePanelWidth'

/** Every `TabKind`, spelled out so a kind added to the union without a thought
 *  about its width shows up here as a compile error rather than as a surprise
 *  shared bucket. */
const ALL_KINDS: readonly TabKind[] = [
  'changes', 'issues', 'links', 'files', 'artifacts', 'subagents', 'workflows',
  'logs', 'context', 'side', 'browser', 'git', 'summary', 'pins',
  'file', 'diff', 'artifact', 'terminal', 'folder', 'app', 'app:pippin:browser',
]

describe('sidePanelWidthGroup', () => {
  it('gives the views the user parks on their own bucket', () => {
    expect(sidePanelWidthGroup('git')).toBe('git')
    expect(sidePanelWidthGroup('browser')).toBe('browser')
    expect(sidePanelWidthGroup('changes')).toBe('changes')
    expect(sidePanelWidthGroup('terminal')).toBe('terminal')
    // The three the complaint named must be mutually distinct, or the feature
    // does nothing for the case that motivated it.
    expect(new Set(['git', 'browser', 'file'].map(k => sidePanelWidthGroup(k as TabKind))).size).toBe(3)
  })

  it('groups the document readers, which share a comfortable measure', () => {
    expect(sidePanelWidthGroup('file')).toBe('doc')
    expect(sidePanelWidthGroup('diff')).toBe('doc')
    expect(sidePanelWidthGroup('artifact')).toBe('doc')
    // A folder tree is navigation, not a document.
    expect(sidePanelWidthGroup('folder')).toBe('folder')
  })

  it('pools every app-contributed tab, so uninstalling an app leaks no key', () => {
    expect(sidePanelWidthGroup('app')).toBe('app')
    expect(sidePanelWidthGroup('app:pippin:browser')).toBe('app')
    expect(sidePanelWidthGroup('app:other:notes')).toBe('app')
  })

  it('falls back to the shared bucket for a kindless tab (a host leading tab)', () => {
    expect(sidePanelWidthGroup(null)).toBe(SIDE_PANEL_DEFAULT_GROUP)
    expect(sidePanelWidthGroup(undefined)).toBe(SIDE_PANEL_DEFAULT_GROUP)
  })

  it('is total over TabKind and never returns an empty group', () => {
    for (const kind of ALL_KINDS) {
      const group = sidePanelWidthGroup(kind)
      expect(group, `kind ${kind}`).toBeTruthy()
    }
  })

  it('collides only on the two intended pools', () => {
    const byGroup = new Map<string, TabKind[]>()
    for (const kind of ALL_KINDS) {
      const g = sidePanelWidthGroup(kind)
      byGroup.set(g, [...(byGroup.get(g) ?? []), kind])
    }
    const shared = [...byGroup.entries()].filter(([, kinds]) => kinds.length > 1).map(([g]) => g)
    expect(shared.sort()).toEqual(['app', 'doc'])
  })
})

describe('sidePanelDimKey', () => {
  it('suffixes the base key with the group', () => {
    expect(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'git')).toBe('mc-side-panel-width:git')
    expect(sidePanelDimKey(SIDE_PANEL_HEIGHT_KEY, 'doc')).toBe('mc-side-panel-height:doc')
  })

  it('never collides with the ungrouped key it migrates from', () => {
    expect(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'git')).not.toBe(SIDE_PANEL_WIDTH_KEY)
  })
})

describe('loadSidePanelDim', () => {
  const load = (group: string) =>
    loadSidePanelDim({ base: SIDE_PANEL_WIDTH_KEY, group, min: 320, fallback: 460 })

  beforeEach(() => { localStorage.clear() })

  it('prefers the group key', () => {
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '700')
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'git'), '380')
    expect(load('git')).toBe(380)
  })

  it('inherits the pre-grouping width for a group that has none (upgrade path)', () => {
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '700')
    expect(load('git')).toBe(700)
    expect(load('browser')).toBe(700)
  })

  it('falls back to the default with nothing stored', () => {
    expect(load('git')).toBe(460)
  })

  it('ignores a value under the floor at either key', () => {
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'git'), '10')
    expect(load('git')).toBe(460)
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '600')
    // The group key is still unusable, so the ungrouped one answers.
    expect(load('git')).toBe(600)
  })

  it('ignores junk', () => {
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'git'), 'wide')
    expect(load('git')).toBe(460)
  })
})
