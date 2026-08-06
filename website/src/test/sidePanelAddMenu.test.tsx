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
    for (const label of ['Issues', 'Subagents', 'Workflows', 'Side', 'Browser']) {
      expect(screen.getByRole('menuitem', { name: label })).toBeTruthy()
    }
    // Pinned views are auto-managed and must never be offered here.
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

describe('newMenuSections', () => {
  const kinds = (o: { devMode: boolean; terminalEnabled: boolean }) =>
    newMenuSections(o).map(g => g.map(i => i.kind))

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

  it('groups by session output, workspaces, then diagnostics', () => {
    expect(kinds({ devMode: true, terminalEnabled: true })).toEqual([
      ['issues', 'subagents', 'workflows'],
      ['side', 'browser', 'terminal'],
      ['logs', 'context'],
    ])
  })

  it('drops a group the gates emptied instead of leaving a stray separator', () => {
    // Developer Mode off empties the diagnostics group entirely — the case the
    // empty-group filter exists for. No returned group is ever empty, under any
    // gate combination.
    for (const devMode of [false, true]) {
      for (const terminalEnabled of [false, true]) {
        for (const group of newMenuSections({ devMode, terminalEnabled })) {
          expect(group.length).toBeGreaterThan(0)
        }
      }
    }
    // Both gates closed: diagnostics gone outright, Terminal dropped from
    // Workspaces — two groups, not three with a hole.
    expect(kinds({ devMode: false, terminalEnabled: false })).toEqual([
      ['issues', 'subagents', 'workflows'],
      ['side', 'browser'],
    ])
    // Terminal back, diagnostics still gated.
    expect(kinds({ devMode: false, terminalEnabled: true })).toEqual([
      ['issues', 'subagents', 'workflows'],
      ['side', 'browser', 'terminal'],
    ])
  })
})
