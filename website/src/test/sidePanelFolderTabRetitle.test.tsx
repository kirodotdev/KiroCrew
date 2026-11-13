/**
 * When a folder tab navigates into a subfolder in place, its chip is retitled to
 * the subfolder's name — not the whole path. The gateway on Windows reports a
 * native path (`C:\proj\src`), which a `/`-only split would show whole, so the
 * side panel's `onPathChange` handler must take the name with `pathBasename`
 * (via `utils/pathBasename`), the way the first-open title (usePanelTabs) does.
 *
 * FolderPanel is stubbed to a button that fires `onPathChange(next)`, which pins
 * the retitle wiring without mounting the file browser.
 */
import { afterEach, describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, fireEvent, cleanup } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
// The stub re-targets the folder tab to whatever path the test asks for, which
// is what a real in-place navigation does through `onPathChange`.
vi.mock('../pages/chat/FolderPanel', () => ({
  default: ({ onPathChange }: { onPathChange?: (p: string) => void }) => (
    <div>
      <button type="button" data-testid="nav-win" onClick={() => onPathChange?.('C:\\proj\\src')}>win</button>
      <button type="button" data-testid="nav-posix" onClick={() => onPathChange?.('/home/u/proj/src')}>posix</button>
    </div>
  ),
}))
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
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../components/sessionEnvironmentHooks', () => ({
  useSlotEnvironmentPlacement: () => ({ remote: null, error: null }),
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs } from '../hooks/usePanelTabs'

type TabsCtl = ReturnType<typeof usePanelTabs>

function Harness({ slot, onReady }: { slot: string; onReady: (ctl: TabsCtl) => void }) {
  const tabsCtl = usePanelTabs(slot)
  onReady(tabsCtl)
  return <SidePanel tabsCtl={tabsCtl} slot={slot} pins={[]} onFileSave={async () => {}} onClose={() => {}} />
}

// A fresh slot per render: `usePanelTabs` persists its tabs per slot, so sharing
// one slot across the cases would rehydrate the previous tab into the next.
let slotSeq = 0
function renderPanel() {
  const slot = `slot-${slotSeq++}`
  let ctl: TabsCtl | undefined
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness slot={slot} onReady={c => { ctl = c }} />
      </Provider>
    </QueryClientProvider>,
  )
  return { ctl: () => ctl as TabsCtl, slot }
}

describe('side panel folder tab: in-place navigation retitles to the folder name', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => cleanup())

  it('titles a Windows subfolder by its name, not the whole C:\\ path', () => {
    const { ctl, slot } = renderPanel()
    act(() => { ctl().openFolder('C:\\proj', slot) })

    fireEvent.click(screen.getByTestId('nav-win'))

    const folder = ctl().tabs.find(t => t.kind === 'folder')
    expect(folder?.path).toBe('C:\\proj\\src')
    expect(folder?.title).toBe('src')
  })

  it('still titles a POSIX subfolder by its name (existing behaviour unchanged)', () => {
    const { ctl, slot } = renderPanel()
    act(() => { ctl().openFolder('/home/u/proj', slot) })

    fireEvent.click(screen.getByTestId('nav-posix'))

    const folder = ctl().tabs.find(t => t.kind === 'folder')
    expect(folder?.path).toBe('/home/u/proj/src')
    expect(folder?.title).toBe('src')
  })
})
