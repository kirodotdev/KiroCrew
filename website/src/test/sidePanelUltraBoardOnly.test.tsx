/**
 * Ultra Mode shows only the Board (docs/decisions/2026-11-02-ultra-mode-shows-only-the-board.md).
 *
 * With the Ultra Mode preview on, an Ultra Mode chat (a crew root or one of its
 * workers) shows no Dashboard tab and its + menu offers none, even when a
 * Dashboard tab is stored on its strip. A normal chat, and every chat with the
 * preview off, keep the Dashboard tab. Rendered through the real SidePanel.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, cleanup, fireEvent, screen, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { turnOnCrewModePreview } from './crewModePreview'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../pages/chat/command-center/CommandCenterPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/command-center/CrewWorkerList', () => ({ default: () => null, CrewWorkersTab: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => false,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'
import type { RootState } from '../store'

const SLOTS = [
  { key: 'crew-root', crew_mode_root: true, messages: 2 },
  { key: 'crew-worker', crew_root: 'crew-root', messages: 1 },
  { key: 'plain', messages: 2 },
]

/** The slot's strip with a Dashboard tab stored on it, the way turning Ultra Mode on leaves it. */
function Seeded({ slot, focus = false }: { slot: string; focus?: boolean }) {
  const tabsCtl = usePanelTabs(slot)
  if (!tabsCtl.tabs.some(t => t.kind === 'command-center')) tabsCtl.openView('command-center')
  else if (focus && tabsCtl.activeId !== 'command-center') tabsCtl.setActive('command-center')
  return <SidePanel tabsCtl={tabsCtl} slot={slot} onFileSave={async () => {}} onClose={() => {}} />
}

function show(slot: string, focus = false) {
  const s = createTestStore()
  const st = createTestStore({ dashboard: { ...s.getState().dashboard, slots: SLOTS } } as unknown as Partial<RootState>)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={st}><Seeded slot={slot} focus={focus} /></Provider>
    </QueryClientProvider>,
  )
}

const dashboardChip = () => screen.queryAllByRole('tab').find(t => /Dashboard/.test(t.textContent || '') || t.getAttribute('aria-label') === 'Dashboard')
const openMenu = () => act(() => {
  fireEvent.pointerDown(screen.getByRole('button', { name: 'Open side panel tab' }), { button: 0, ctrlKey: false, pointerType: 'mouse' })
})

describe('Ultra Mode shows only the Board', () => {
  beforeEach(() => { cleanup(); localStorage.clear(); __resetPanelTabs() })

  describe('preview on', () => {
    beforeEach(() => { turnOnCrewModePreview() })

    it('an Ultra Mode root shows the Board and no Dashboard tab, even one stored', () => {
      show('crew-root')
      expect(screen.getAllByRole('tab').some(t => t.textContent?.includes('Board'))).toBe(true)
      expect(dashboardChip()).toBeUndefined()
    })

    it("an Ultra Mode worker's view shows no Dashboard tab either", () => {
      show('crew-worker')
      expect(dashboardChip()).toBeUndefined()
    })

    it('a stored Dashboard tab in front falls back to a shown tab', () => {
      show('crew-root', true)
      const selected = screen.getAllByRole('tab').find(t => t.getAttribute('aria-selected') === 'true')
      expect(selected).toBeTruthy()
      expect(/Dashboard/.test(selected?.textContent || '')).toBe(false)
    })

    it("an Ultra Mode chat's + menu offers no Dashboard", () => {
      show('crew-root')
      openMenu()
      expect(screen.queryByRole('menuitem', { name: 'Dashboard' })).toBeNull()
    })

    it('a normal chat keeps its Dashboard tab and + menu entry', () => {
      show('plain')
      expect(dashboardChip()).toBeTruthy()
      openMenu()
      expect(screen.getByRole('menuitem', { name: 'Dashboard' })).toBeTruthy()
    })
  })

  it('with the preview off, a Crew Mode chat keeps its Dashboard tab', () => {
    show('crew-root')
    expect(dashboardChip()).toBeTruthy()
  })
})
