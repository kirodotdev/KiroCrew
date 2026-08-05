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

import SidePanel from '../pages/chat/SidePanel'
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
    for (const label of ['Issues', 'Subagents', 'Workflows', 'Logs', 'Side', 'Browser']) {
      expect(screen.getByRole('menuitem', { name: label })).toBeTruthy()
    }
    // Pinned views are auto-managed and must never be offered here.
    expect(screen.queryByRole('menuitem', { name: 'Files' })).toBeNull()
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
})
