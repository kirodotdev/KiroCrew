import { useEffect } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => <div data-testid="activity-viewer" /> }))
vi.mock('../pages/chat/FilesHomePanel', () => ({ default: ({ active }: { active?: boolean }) => <div data-testid="files-home" data-active={String(active)} /> }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
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
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { __resetPanelTabs, usePanelTabs, type ViewKind } from '../hooks/usePanelTabs'

function Harness({ view, hidden }: { view: ViewKind; hidden: boolean }) {
  const tabsCtl = usePanelTabs('slot-a')
  const openView = tabsCtl.openView
  useEffect(() => { openView(view) }, [openView, view])
  return <SidePanel tabsCtl={tabsCtl} slot="slot-a" projectDir="/repo" onFileOpen={vi.fn()} onFileSave={async () => {}} onClose={() => {}} panelHidden={hidden} />
}

function renderPanel(view: ViewKind, hidden: boolean) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness view={view} hidden={hidden} />
      </Provider>
    </QueryClientProvider>,
  )
}

describe('a side panel kept mounted but hidden', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetPanelTabs()
  })

  it('keeps the Files body mounted, so its tree survives, but inactive so its polling pauses', async () => {
    const view = renderPanel('files', false)
    expect((await screen.findByTestId('files-home')).dataset.active).toBe('true')
    view.unmount()
    __resetPanelTabs()
    renderPanel('files', true)
    expect((await screen.findByTestId('files-home')).dataset.active).toBe('false')
  })

  it.each([['workflows', 'activity-viewer']] as const)('does not keep the %s body (and its polling) mounted', async (view, testId) => {
    const shown = renderPanel(view, false)
    expect(await screen.findByTestId(testId)).toBeInTheDocument()
    shown.unmount()
    __resetPanelTabs()
    renderPanel(view, true)
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(screen.queryByTestId(testId)).toBeNull()
  })
})
