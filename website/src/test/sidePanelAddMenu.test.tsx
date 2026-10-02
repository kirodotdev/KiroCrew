/**
 * The side panel's "+" tab menu runs on the shared shadcn/Radix dropdown.
 *
 * Guards the two things the previous hand-rolled menu owned by hand and that a
 * regression would silently take away: the menu opens from the trigger with its
 * items exposed under the WAI-ARIA menu roles, and selecting an item opens that
 * view as a tab. Escape covers the dismissal path Radix now owns instead of the
 * document-level mousedown listener this replaced.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

// Heavy tab bodies — none of them are what this test drives.
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
// Terminal off / Developer Mode off: the menu then lists exactly the views the
// assertions below name, with no environment-dependent extras.
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => false,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel, { newMenuSections, NEW_MENU_LABEL_KEY } from '../pages/chat/SidePanel'
import { usePanelTabs } from '../hooks/usePanelTabs'
import { PREVIEW_DASHBOARD, setPreviewFlag } from '../utils/previewFlags'

function Harness() {
  const tabsCtl = usePanelTabs('slot-a')
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness />
      </Provider>
    </QueryClientProvider>,
  )
}

const openMenu = () => act(() => {
  fireEvent.pointerDown(
    screen.getByRole('button', { name: 'Open side panel tab' }),
    { button: 0, ctrlKey: false, pointerType: 'mouse' },
  )
})

describe('side panel + menu (shadcn dropdown)', () => {
  beforeEach(() => { localStorage.clear() })

  it('opens as an ARIA menu with the view items', () => {
    renderPanel()
    expect(screen.queryByRole('menu')).toBeNull()
    openMenu()
    expect(screen.getByRole('menu')).toBeTruthy()
    for (const label of ['Pins', 'Issues', 'Subagents', 'Workflows', 'Side Chat', 'Browser']) {
      expect(screen.getByRole('menuitem', { name: label })).toBeTruthy()
    }
    // The permanently pinned views are always in the strip already, so they must
    // never be offered here.
    expect(screen.queryByRole('menuitem', { name: 'Files' })).toBeNull()
    // Diagnostics are behind Developer Mode, which this harness has off.
    expect(screen.queryByRole('menuitem', { name: 'Logs' })).toBeNull()
    expect(screen.queryByRole('menuitem', { name: 'Context breakdown' })).toBeNull()
  })

  it('opens the picked view as a tab', () => {
    renderPanel()
    openMenu()
    act(() => { fireEvent.click(screen.getByRole('menuitem', { name: 'Workflows' })) })
    expect(screen.getByRole('tab', { name: /Workflows/ })).toBeTruthy()
  })

  it('dismisses on Escape', () => {
    renderPanel()
    openMenu()
    act(() => { fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' }) })
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('renders one separator between the groups and none at the edges', () => {
    renderPanel()
    openMenu()
    const menu = screen.getByRole('menu')
    const kids = Array.from(menu.children)
    const seps = kids.filter(el => el.getAttribute('role') === 'separator')
    // Developer Mode is off in this harness, so the whole diagnostics group is
    // gone and only two groups survive: session output, then Side + Browser
    // (Terminal is disabled too). Two groups, one rule.
    expect(seps).toHaveLength(1)
    expect(kids[0].getAttribute('role')).toBe('menuitem')
    expect(kids[kids.length - 1].getAttribute('role')).toBe('menuitem')
    // Rules separate groups, so no two are adjacent.
    const roles = kids.map(el => el.getAttribute('role'))
    expect(roles.join(' ')).not.toContain('separator separator')
  })
})

describe('side panel Dashboard view behind the Dynamic Dashboard preview', () => {
  beforeEach(() => { localStorage.clear() })

  it('offers no Dashboard entry and withholds a persisted Dashboard tab while the preview is off', () => {
    // A tab the user opened before the flag went off (or that the store still
    // holds) must not stay on the strip: the withdrawal is the same one a host
    // applies, so it covers the bucket, not just the menu.
    const Seeded = () => {
      const tabsCtl = usePanelTabs('slot-a')
      if (!tabsCtl.tabs.some(t => t.kind === 'command-center')) tabsCtl.openView('command-center')
      return <SidePanel tabsCtl={tabsCtl} slot="slot-a" onFileSave={async () => {}} onClose={() => {}} />
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <Provider store={createTestStore()}>
          <Seeded />
        </Provider>
      </QueryClientProvider>,
    )
    expect(screen.queryByRole('tab', { name: /Dashboard/ })).toBeNull()
    expect(screen.queryByTestId('command-center-panel')).toBeNull()
    openMenu()
    expect(screen.queryByRole('menuitem', { name: 'Dashboard' })).toBeNull()
    // Every other session-output row is still there: only this view is gated.
    expect(screen.getByRole('menuitem', { name: 'Pins' })).toBeTruthy()
  })

  it('offers the Dashboard entry, and opens it as a tab, once the preview is on', () => {
    localStorage.setItem(PREVIEW_DASHBOARD, '1')
    renderPanel()
    openMenu()
    act(() => { fireEvent.click(screen.getByRole('menuitem', { name: 'Dashboard' })) })
    expect(screen.getByRole('tab', { name: /Dashboard/ })).toBeTruthy()
  })

  it('brings a withheld Dashboard tab back in the same tick the toggle flips on', () => {
    const Seeded = () => {
      const tabsCtl = usePanelTabs('slot-a')
      if (!tabsCtl.tabs.some(t => t.kind === 'command-center')) tabsCtl.openView('command-center')
      return <SidePanel tabsCtl={tabsCtl} slot="slot-a" onFileSave={async () => {}} onClose={() => {}} />
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <Provider store={createTestStore()}>
          <Seeded />
        </Provider>
      </QueryClientProvider>,
    )
    expect(screen.queryByRole('tab', { name: /Dashboard/ })).toBeNull()
    act(() => { setPreviewFlag(PREVIEW_DASHBOARD, true) })
    expect(screen.getByRole('tab', { name: /Dashboard/ })).toBeTruthy()
  })
})

describe('newMenuSections', () => {
  const kinds = (o: { devMode: boolean; terminalEnabled: boolean; summaryEnabled?: boolean }) =>
    newMenuSections({ summaryEnabled: true, ...o }).map(g => g.items.map(i => i.kind))

  it('partitions every catalogued view exactly once', () => {
    // Both gates open, so nothing is filtered but the auto-pinned views. Any
    // view added to NEW_MENU_LABEL_KEY without being placed in a group — or
    // placed in two — fails here instead of quietly vanishing from the menu.
    const flat = kinds({ devMode: true, terminalEnabled: true }).flat()
    const pinned = ['changes', 'files', 'artifacts']
    const catalogued = Object.keys(NEW_MENU_LABEL_KEY).filter(k => !pinned.includes(k))
    expect([...flat].sort()).toEqual([...catalogued].sort())
    expect(new Set(flat).size).toBe(flat.length)
  })

  it('hides Summary while session summaries are disabled', () => {
    // The feature is opt-in and its settings toggle ships separately, so
    // advertising the row while the flag is false sends every reader to a panel
    // that says it is off and offers no way to change that.
    const flat = kinds({ devMode: true, terminalEnabled: true, summaryEnabled: false }).flat()
    expect(flat).not.toContain('summary')
    // Only that row goes — its group still carries the rest, so the group is not
    // dropped and nothing else is collateral.
    expect(kinds({ devMode: true, terminalEnabled: true, summaryEnabled: false })[0])
      .toEqual(['command-center', 'pins', 'issues', 'links', 'subagents', 'workflows', 'git'])
  })

  it('keeps each group id fixed however the gates fall', () => {
    // The group id is the menu's React key. If it moved when a gate resolved,
    // React would remount the group and detach the row under the user's cursor
    // — `summaryEnabled` in particular starts undefined and flips when its
    // request lands, i.e. potentially mid-click. So the id a group reports must
    // depend only on its declaration, never on which rows survived.
    const idsFor = (o: { devMode: boolean; terminalEnabled: boolean; summaryEnabled: boolean }) =>
      newMenuSections(o).map(g => g.id)

    // Gating a row must not touch its group's id.
    expect(idsFor({ devMode: true, terminalEnabled: true, summaryEnabled: false }))
      .toEqual(idsFor({ devMode: true, terminalEnabled: true, summaryEnabled: true }))

    // Dropping a whole group must not renumber the survivors: with Developer
    // Mode off the diagnostics group disappears, and the two that remain keep
    // the ids they had.
    expect(idsFor({ devMode: true, terminalEnabled: true, summaryEnabled: true }))
      .toEqual(['session-output', 'workspaces', 'diagnostics'])
    expect(idsFor({ devMode: false, terminalEnabled: true, summaryEnabled: true }))
      .toEqual(['session-output', 'workspaces'])

    // And ids stay unique, or two groups would collide on one key.
    for (const devMode of [false, true]) {
      for (const summaryEnabled of [false, true]) {
        const ids = idsFor({ devMode, terminalEnabled: true, summaryEnabled })
        expect(new Set(ids).size).toBe(ids.length)
      }
    }
  })

  it('groups by session output, workspaces, then diagnostics', () => {
    expect(kinds({ devMode: true, terminalEnabled: true })).toEqual([
      ['command-center', 'summary', 'pins', 'issues', 'links', 'subagents', 'workflows', 'git'],
      ['side', 'browser', 'terminal'],
      ['logs', 'context', 'crewlog'],
    ])
  })

  it('drops a group the gates emptied instead of leaving a stray separator', () => {
    // Developer Mode off empties the diagnostics group entirely — the case the
    // empty-group filter exists for. No returned group is ever empty, under any
    // gate combination.
    for (const devMode of [false, true]) {
      for (const terminalEnabled of [false, true]) {
        for (const group of newMenuSections({ devMode, terminalEnabled, summaryEnabled: true })) {
          expect(group.items.length).toBeGreaterThan(0)
        }
      }
    }
    // Both gates closed: diagnostics gone outright, Terminal dropped from
    // Workspaces — two groups, not three with a hole.
    expect(kinds({ devMode: false, terminalEnabled: false })).toEqual([
      ['command-center', 'summary', 'pins', 'issues', 'links', 'subagents', 'workflows', 'git'],
      ['side', 'browser'],
    ])
    // Terminal back, diagnostics still gated.
    expect(kinds({ devMode: false, terminalEnabled: true })).toEqual([
      ['command-center', 'summary', 'pins', 'issues', 'links', 'subagents', 'workflows', 'git'],
      ['side', 'browser', 'terminal'],
    ])
  })
})
