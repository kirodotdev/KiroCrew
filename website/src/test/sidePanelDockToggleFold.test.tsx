/**
 * The side panel's dock toggle beside a reported fold (SidePanel.tsx
 * `!dockLocked`, hooks/useSidePanelDock.ts). While a fold is reported the
 * panel docks right, since a bottom row would span both segments, so the
 * toggle that offers the bottom dock is not drawn. With no fold it is drawn.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import type { ViewportSegment } from '../hooks/useViewportSegments'

const segmentsMock = vi.hoisted(() => ({ value: null as null | ViewportSegment[] }))
vi.mock('../hooks/useViewportSegments', async (orig) => ({
  ...(await orig<typeof import('../hooks/useViewportSegments')>()),
  useViewportSegments: () => segmentsMock.value,
}))
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
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => false,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useIsMobile', async (orig) => ({
  ...(await orig<typeof import('../hooks/useIsMobile')>()),
  useIsMobile: () => false,
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'
import { i18nT } from '../i18n/t'

function Harness() {
  const tabsCtl = usePanelTabs('slot-a')
  return <SidePanel tabsCtl={tabsCtl} slot="slot-a" onFileSave={async () => {}} onClose={() => {}} />
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

const DOCK_TOGGLE = () => i18nT('pages.chatSidebar.more_options')

afterEach(() => {
  cleanup()
  segmentsMock.value = null
  __resetPanelTabs()
  localStorage.clear()
})

describe("the side panel's dock toggle beside a fold", () => {
  it('is drawn with no fold reported', () => {
    renderPanel()
    expect(screen.queryByRole('button', { name: DOCK_TOGGLE() })).not.toBeNull()
  })

  it('is not drawn while a fold is reported, whatever dock is stored', () => {
    localStorage.setItem('mc-side-panel-dock', 'bottom')
    segmentsMock.value = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]
    renderPanel()
    expect(screen.queryByRole('button', { name: DOCK_TOGGLE() })).toBeNull()
  })
})
