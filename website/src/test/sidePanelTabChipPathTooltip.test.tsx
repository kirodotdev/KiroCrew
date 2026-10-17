/**
 * A side-panel tab chip must expose the full path of the file it holds as its
 * hover tooltip.
 *
 * The chip's visible label is `basename(path)` inside a `max-w-[240px]`
 * truncating span, so on its own it answers neither "what is this file called"
 * for a long name nor "which of these two `index.ts` tabs is which" for a deep
 * tree. The breadcrumb bar in the panel body already carries the full path;
 * the chip is the surface a user hovers first, and it carried no tooltip at all
 * for a non-pinned tab.
 *
 * The ratchet has two halves: a tab that HAS a path must advertise that path,
 * and the icon-only pinned chips must keep the accessible name they need for a
 * glyph with no visible text.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

// Heavy tab bodies are not what this drives — only the strip's chips.
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
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs } from '../hooks/usePanelTabs'

type TabsCtl = ReturnType<typeof usePanelTabs>

function Harness({ onReady }: { onReady: (ctl: TabsCtl) => void }) {
  const tabsCtl = usePanelTabs('slot-a')
  onReady(tabsCtl)
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      pins={[]}
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
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

describe('side panel tab chip exposes the full path as its tooltip', () => {
  beforeEach(() => { localStorage.clear() })

  it('titles a file tab with the absolute path, not the basename', () => {
    const ctl = renderPanel()
    const path = '/Users/dev/workspace/src/very/deeply/nested/package/notes.md'
    act(() => { ctl().openFile(path, '# hi', 'slot-a') })

    const chip = screen.getByRole('tab', { name: /notes\.md/ })
    // The whole point: the truncated label says `notes.md`, the tooltip says
    // which `notes.md`.
    expect(chip.getAttribute('title')).toBe(path)
  })

  it('distinguishes two same-named files in different directories', () => {
    const ctl = renderPanel()
    const a = '/repo/alpha/index.ts'
    const b = '/repo/beta/index.ts'
    act(() => { ctl().openFile(a, 'a', 'slot-a') })
    act(() => { ctl().openFile(b, 'b', 'slot-a') })

    // Both chips carry the identical visible label, so the tooltip is the only
    // thing that tells them apart.
    const titles = screen.getAllByRole('tab')
      .map(el => el.getAttribute('title'))
      .filter((t): t is string => t != null)
    expect(titles).toContain(a)
    expect(titles).toContain(b)
  })

  it('titles a folder tab with its path', () => {
    const ctl = renderPanel()
    const path = '/repo/packages/design-system/src'
    act(() => { ctl().openFolder(path, 'slot-a') })

    expect(screen.getByRole('tab', { name: /src/ }).getAttribute('title')).toBe(path)
  })

  it('keeps a name on the icon-only pinned chips', () => {
    // Pinned views are icon-only while inactive and have no path, so they must
    // still fall back to their title — otherwise this change would strip the
    // only name a glyph-only chip has.
    renderPanel()
    const files = screen.getByRole('tab', { name: 'Files' })
    expect(files.getAttribute('title') || files.getAttribute('aria-label')).toBe('Files')
  })
})
