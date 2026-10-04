/**
 * #9695 — opening a file's working-tree diff from the pinned Files tab's
 * Changed segment opens a COEXISTING `wtdiff:<path>` tab via
 * `onOpenWorkingTreeDiff`, instead of flipping the plain file tab into diff
 * mode. A plain (non-diff) open still goes through `onFileOpen`.
 */
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
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
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

// FilesHomePanel is the pinned Files tab body. Mock it to expose two buttons
// that fire its `onFileOpen` with the diff flag off (plain open) and on (the
// Changed segment's Diff icon).
vi.mock('../pages/chat/FilesHomePanel', () => ({
  default: ({ onFileOpen }: { onFileOpen: (abs: string, diff: boolean, opts?: { line?: number }) => void }) => (
    <div>
      <button onClick={() => onFileOpen('/repo/a.ts', false)}>open-plain</button>
      <button onClick={() => onFileOpen('/repo/a.ts', true)}>open-diff</button>
    </div>
  ),
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { __resetPanelTabs, usePanelTabs } from '../hooks/usePanelTabs'

function Harness({ onFileOpen, onOpenWorkingTreeDiff }: {
  onFileOpen: (path: string, opts?: unknown) => void
  onOpenWorkingTreeDiff: (path: string) => void
}) {
  const tabsCtl = usePanelTabs('slot-a')
  const openView = tabsCtl.openView
  useEffect(() => { openView('files') }, [openView])
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      projectDir="/repo"
      onFileOpen={onFileOpen as never}
      onOpenWorkingTreeDiff={onOpenWorkingTreeDiff}
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderPanel(props: { onFileOpen: ReturnType<typeof vi.fn>; onOpenWorkingTreeDiff: ReturnType<typeof vi.fn> }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness {...props} />
      </Provider>
    </QueryClientProvider>,
  )
}

describe('#9695 Files-tab diff open routes to the coexisting working-tree-diff tab', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs() })
  afterEach(() => { vi.restoreAllMocks() })

  it('a Changed-segment diff open calls onOpenWorkingTreeDiff, NOT onFileOpen', () => {
    const onFileOpen = vi.fn()
    const onOpenWorkingTreeDiff = vi.fn()
    renderPanel({ onFileOpen, onOpenWorkingTreeDiff })
    fireEvent.click(screen.getByText('open-diff'))
    expect(onOpenWorkingTreeDiff).toHaveBeenCalledWith('/repo/a.ts')
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('a plain open still goes through onFileOpen (file view), not the diff opener', () => {
    const onFileOpen = vi.fn()
    const onOpenWorkingTreeDiff = vi.fn()
    renderPanel({ onFileOpen, onOpenWorkingTreeDiff })
    fireEvent.click(screen.getByText('open-plain'))
    expect(onFileOpen).toHaveBeenCalledWith('/repo/a.ts', { diffMode: false, line: undefined })
    expect(onOpenWorkingTreeDiff).not.toHaveBeenCalled()
  })

  it('falls back to flipping the file tab when the host supplies no diff opener', () => {
    const onFileOpen = vi.fn()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    function FallbackHarness() {
      const tabsCtl = usePanelTabs('slot-a')
      const openView = tabsCtl.openView
      useEffect(() => { openView('files') }, [openView])
      return (
        <SidePanel tabsCtl={tabsCtl} slot="slot-a" projectDir="/repo"
          onFileOpen={onFileOpen as never} onFileSave={async () => {}} onClose={() => {}} />
      )
    }
    render(
      <QueryClientProvider client={queryClient}>
        <Provider store={createTestStore()}><FallbackHarness /></Provider>
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByText('open-diff'))
    // No diff opener → pre-#9695 behaviour: flip the file tab into diff mode.
    expect(onFileOpen).toHaveBeenCalledWith('/repo/a.ts', { diffMode: true, line: undefined })
  })
})
