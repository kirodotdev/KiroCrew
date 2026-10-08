import { useEffect } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from './helpers'

// The Git tab lists the chat's repositories itself (`GitReposPanel`, keyed on
// the slot), so ActivityViewer has no reader for a project directory. A prop
// that is passed but never read is the shape of a leftover, and this pins the
// pass away at its one call site.
const viewerProps = vi.fn()
vi.mock('../pages/chat/ActivityViewer', () => ({
  default: (props: Record<string, unknown>) => {
    viewerProps(props)
    return <div data-testid="activity-viewer" />
  },
}))
vi.mock('../pages/chat/FilesHomePanel', () => ({ default: () => null }))
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
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'

function Harness() {
  const tabsCtl = usePanelTabs('slot-a')
  const openView = tabsCtl.openView
  useEffect(() => { openView('git') }, [openView])
  return <SidePanel tabsCtl={tabsCtl} slot="slot-a" projectDir="/repo" onFileOpen={vi.fn()} onFileSave={async () => {}} onClose={() => {}} />
}

describe('the Git tab body', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetPanelTabs()
    viewerProps.mockReset()
  })

  it('reaches ActivityViewer with the slot and no project directory', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <Provider store={createTestStore()}>
          <Harness />
        </Provider>
      </QueryClientProvider>,
    )
    await screen.findByTestId('activity-viewer')
    const gitView = viewerProps.mock.calls.map(([p]) => p as Record<string, unknown>).find(p => p.view === 'git')
    expect(gitView).toBeDefined()
    expect(gitView).toMatchObject({ slot: 'slot-a' })
    // The side panel keeps its own projectDir for the file tabs; none of it is
    // forwarded here, because nothing in ActivityViewer reads it.
    expect(gitView).not.toHaveProperty('projectDir')
  })
})
