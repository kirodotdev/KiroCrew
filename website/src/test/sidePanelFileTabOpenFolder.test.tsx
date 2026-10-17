/**
 * A file tab's header menu "Show folder in side panel" lands in the side panel
 * as a folder tab.
 *
 * The side panel is the host that owns the folder-tab opener, so it is the one
 * that hands `onFolderOpen` down to the file tab's MarkdownPanel. The panel body
 * is stubbed to a button that calls the handler it received, which pins the
 * wiring without mounting the editor.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({
  default: ({ filePath, onFolderOpen }: { filePath: string; onFolderOpen?: (p: string) => void }) => (
    <button type="button" data-testid="stub-show-in-files" disabled={!onFolderOpen} onClick={() => onFolderOpen?.(filePath.replace(/\/[^/]+$/, ''))}>
      show {filePath}
    </button>
  ),
}))
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
import { usePanelTabs } from '../hooks/usePanelTabs'

type TabsCtl = ReturnType<typeof usePanelTabs>

function Harness({ onReady }: { onReady: (ctl: TabsCtl) => void }) {
  const tabsCtl = usePanelTabs('slot-a')
  onReady(tabsCtl)
  return <SidePanel tabsCtl={tabsCtl} slot="slot-a" pins={[]} onFileSave={async () => {}} onClose={() => {}} />
}

function renderPanel() {
  let ctl: TabsCtl | undefined
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness onReady={c => { ctl = c }} />
      </Provider>
    </QueryClientProvider>,
  )
  return () => ctl as TabsCtl
}

describe('side panel file tab: "Show folder in side panel" opens a folder tab', () => {
  beforeEach(() => { localStorage.clear() })

  it('opens the folder as a focused folder tab carrying the file tab\'s origin chat', () => {
    const ctl = renderPanel()
    act(() => { ctl().openFile('/repo/alpha/notes.md', '# hi', 'slot-a') })

    fireEvent.click(screen.getByTestId('stub-show-in-files'))

    const folder = ctl().tabs.find(t => t.kind === 'folder')
    expect(folder).toMatchObject({ id: 'folder:/repo/alpha', path: '/repo/alpha', slot: 'slot-a' })
    expect(ctl().activeId).toBe('folder:/repo/alpha')
  })
})
