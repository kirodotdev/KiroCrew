import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  resolvePanelTabs,
  panelTabDescriptor,
  panelTabKind,
  isPanelTabKind,
  MAX_PANEL_TABS_PER_APP,
  type PanelTabAppRecord,
} from '../hooks/panelTabRegistry'

const tab = (over: Record<string, unknown> = {}) => ({
  id: 'browser', title: 'Pippin', menuLabel: 'Pippin', icon: 'BookOpen', entry: 'ui/panel.mjs', ...over,
})
const app = (name: string, tabs: unknown[], over: Partial<PanelTabAppRecord> = {}): PanelTabAppRecord => ({
  name, enabled: true, manifest: { contributes: { panelTabs: tabs as never } }, ...over,
})

let warn: ReturnType<typeof vi.spyOn>
beforeEach(() => { warn = vi.spyOn(console, 'warn').mockImplementation(() => {}) })
afterEach(() => warn.mockRestore())

describe('resolvePanelTabs', () => {
  it('resolves an enabled app tab to an app:<name>:<id> descriptor', () => {
    const [d] = resolvePanelTabs([app('pippin', [tab()])])
    expect(d.kind).toBe('app:pippin:browser')
    expect(d).toMatchObject({ appName: 'pippin', tabId: 'browser', title: 'Pippin', menuLabel: 'Pippin', icon: 'BookOpen', entry: 'ui/panel.mjs' })
  })

  it('is empty by default (no apps, or none contributing)', () => {
    expect(resolvePanelTabs([])).toEqual([])
    expect(resolvePanelTabs([app('x', [])])).toEqual([])
  })

  it('skips a DISABLED app (enable is the user opt-in)', () => {
    expect(resolvePanelTabs([app('pippin', [tab()], { enabled: false })])).toEqual([])
  })

  it('orders tabs by app name, not response order, for a stable strip', () => {
    const kinds = resolvePanelTabs([
      app('zeta', [tab({ id: 'z' })]),
      app('alpha', [tab({ id: 'a' })]),
    ]).map(d => d.kind)
    expect(kinds).toEqual(['app:alpha:a', 'app:zeta:z'])
  })

  it('warns and skips a declaration missing id/title/menuLabel/entry', () => {
    expect(resolvePanelTabs([app('p', [tab({ id: '' })])])).toEqual([])
    expect(resolvePanelTabs([app('p', [tab({ title: '' })])])).toEqual([])
    expect(resolvePanelTabs([app('p', [tab({ menuLabel: '' })])])).toEqual([])
    expect(resolvePanelTabs([app('p', [tab({ entry: '' })])])).toEqual([])
    expect(warn).toHaveBeenCalled()
  })

  it('warns and skips a duplicate id within one app (does not throw)', () => {
    const out = resolvePanelTabs([app('p', [tab(), tab()])])
    expect(out.map(d => d.kind)).toEqual(['app:p:browser'])
    expect(warn).toHaveBeenCalled()
  })

  it('tolerates a hand-edited manifest without contributes', () => {
    expect(resolvePanelTabs([{ name: 'p', enabled: true, manifest: {} }])).toEqual([])
    expect(resolvePanelTabs([{ name: 'p', enabled: true }])).toEqual([])
  })

  it('caps one app at MAX_PANEL_TABS_PER_APP and warns about the overflow', () => {
    // The manifest enforces the same bound. A cap only the manifest checks would let
    // a hand-edited app.json render an unbounded strip here.
    const many = Array.from({ length: MAX_PANEL_TABS_PER_APP + 3 }, (_, i) => tab({ id: `t${i}` }))
    const out = resolvePanelTabs([app('p', many)])
    expect(out).toHaveLength(MAX_PANEL_TABS_PER_APP)
    expect(warn).toHaveBeenCalled()
  })

  it('caps per app, so a second app still contributes', () => {
    const many = Array.from({ length: MAX_PANEL_TABS_PER_APP + 1 }, (_, i) => tab({ id: `t${i}` }))
    const out = resolvePanelTabs([app('alpha', many), app('beta', [tab({ id: 'only' })])])
    expect(out).toHaveLength(MAX_PANEL_TABS_PER_APP + 1)
    expect(out.at(-1)?.kind).toBe('app:beta:only')
  })
})

describe('kind helpers', () => {
  it('panelTabKind / isPanelTabKind round-trip; core + ephemeral app kinds are not app tabs', () => {
    expect(panelTabKind('pippin', 'browser')).toBe('app:pippin:browser')
    expect(isPanelTabKind('app:pippin:browser')).toBe(true)
    expect(isPanelTabKind('app')).toBe(false) // ephemeral MCP app tab, not a contributed one
    expect(isPanelTabKind('files')).toBe(false)
  })

  it('panelTabDescriptor looks up over a resolved list', () => {
    const tabs = resolvePanelTabs([app('pippin', [tab()])])
    expect(panelTabDescriptor('app:pippin:browser', tabs)?.title).toBe('Pippin')
    expect(panelTabDescriptor('app:pippin:missing', tabs)).toBeUndefined()
  })
})
