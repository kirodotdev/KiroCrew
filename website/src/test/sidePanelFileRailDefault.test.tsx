import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
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
vi.mock('../pierre/tree', () => ({
  PierreWorkspaceTree: () => <div data-testid="file-browser-tree" />,
}))
vi.mock('../components/MarkdownPanel', async () => {
  const React = await vi.importActual<typeof import('react')>('react')
  return {
    default: React.forwardRef(function MockMarkdownPanel({ railOpen, onRailToggle, browserRail }: {
      railOpen?: boolean
      onRailToggle?: () => void
      browserRail?: React.ReactNode
    }, _ref) {
      return (
        <div data-testid="markdown-panel" data-rail-open={String(railOpen)}>
          {onRailToggle && <button onClick={onRailToggle}>Toggle file browser</button>}
          {railOpen && browserRail}
        </div>
      )
    }),
  }
})

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'

function Harness() {
  const tabsCtl = usePanelTabs('slot-a')
  const openFile = tabsCtl.openFile
  useEffect(() => {
    openFile('/repo/README.md', '# README', 'slot-a')
  }, [openFile])
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      projectDir="/repo"
      onFileOpen={vi.fn()}
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness />
      </Provider>
    </QueryClientProvider>,
  )
}

describe('file tab project tree rail', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetPanelTabs()
    vi.spyOn(api, 'projectTree').mockResolvedValue({ root: '/repo', paths: [], repo: true })
    vi.spyOn(api, 'projectGitStatus').mockResolvedValue({ repo: true, files: [] })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('starts hidden and remains available through the file toolbar toggle', async () => {
    renderPanel()
    const panel = await screen.findByTestId('markdown-panel')
    expect(panel).toHaveAttribute('data-rail-open', 'false')
    expect(screen.queryByTestId('file-browser-tree')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Toggle file browser' }))
    expect(panel).toHaveAttribute('data-rail-open', 'true')
    expect(screen.getByTestId('file-browser-tree')).toBeInTheDocument()
  })

  it('honours an explicit saved preference to show the rail', async () => {
    localStorage.setItem('mc-files-rail-open', '1')
    renderPanel()
    expect(await screen.findByTestId('file-browser-tree')).toBeInTheDocument()
  })

  it('keeps the toggle and denied notice reachable when the tree read is refused', async () => {
    localStorage.setItem('mc-files-rail-open', '1')
    vi.mocked(api.projectTree).mockRejectedValue(
      new ApiError(403, 'denied', JSON.stringify({ code: 'access_denied' })),
    )
    renderPanel()

    expect(await screen.findByText('No access to this folder')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Toggle file browser' })).toBeInTheDocument()
    expect(screen.getByLabelText('Refresh')).toBeInTheDocument()
    expect(screen.queryByTestId('file-browser-tree')).toBeNull()
  })

})
