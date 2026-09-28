/**
 * Middle-click on a side-panel tab closes it, the same way its × does.
 *
 * The binding lives on the `TabChip` element in `SidePanel.tsx`
 * (`onAuxClick` → `onClose`, guarded by `closable` and `e.button === 1`). It is
 * the file-tab half of the middle-click-to-close convention every tabbed app
 * ships (issue #9406); `SessionTabStrip.test.tsx` pins the same gesture on the
 * sidebar session strip.
 *
 * Four contracts, each of which a plausible refactor breaks silently:
 *
 * 1. A middle-click on a file tab closes THAT tab — routed through the strip's
 *    existing close path (`usePanelTabs.closeTab`), not a second close.
 * 2. It closes only that tab: its neighbours stay.
 * 3. It suppresses the default action. On Linux/X11 a middle-click is also a
 *    primary-selection paste, so an unsuppressed default lands a stray paste
 *    into a focused input; `preventDefault()` is the guarantee that it does not.
 * 4. It does not fall through to tab SELECTION, and a right-button aux click is
 *    ignored entirely — only button 1 closes.
 *
 * Bodies are stubbed as in `sidePanelLeadingTab.test.tsx`; only the strip is
 * driven. The strip model is exposed through a harness so a case can open the
 * file tabs a chat page would, then read back which tabs survived.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

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
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'

const SLOT = 'chat-1'

/** Exposes the strip model so a case can open file tabs and read back which
 *  tabs survived, the way the chat page owns the strip. */
let ctl: ReturnType<typeof usePanelTabs> | null = null

function Harness() {
  const tabsCtl = usePanelTabs(SLOT)
  ctl = tabsCtl
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot={SLOT}
      onFileSave={async () => {}}
      onClose={() => {}}
      canDockBottom={false}
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

/** A middle press produces `auxclick`, not `click`, and testing-library has no
 *  shorthand for it — so dispatch the real event React's onAuxClick listens for.
 *  Returns the event so a case can read `defaultPrevented`. */
function auxClick(el: HTMLElement, button: number): MouseEvent {
  const ev = new MouseEvent('auxclick', { bubbles: true, cancelable: true, button })
  fireEvent(el, ev)
  return ev
}

/** The file tab whose title is `name` (file chips show their basename label). */
const fileTab = (name: string) => screen.getByRole('tab', { name: new RegExp(name) })

describe('SidePanel tab middle-click-to-close', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs(); ctl = null })

  it('closes the middle-clicked file tab through the existing close path, and only that tab', () => {
    renderPanel()
    act(() => {
      ctl!.openFile('/srv/one.md', 'one', SLOT)
      ctl!.openFile('/srv/two.md', 'two', SLOT)
    })
    expect(ctl!.tabs.some(t => t.id === 'file:/srv/one.md')).toBe(true)
    expect(ctl!.tabs.some(t => t.id === 'file:/srv/two.md')).toBe(true)

    auxClick(fileTab('one\\.md'), 1)

    // The middle-clicked tab is gone; its neighbour stays.
    expect(ctl!.tabs.some(t => t.id === 'file:/srv/one.md')).toBe(false)
    expect(ctl!.tabs.some(t => t.id === 'file:/srv/two.md')).toBe(true)
    expect(screen.queryByRole('tab', { name: /one\.md/ })).toBeNull()
    expect(screen.getByRole('tab', { name: /two\.md/ })).toBeInTheDocument()
  })

  it('suppresses the default action so an X11 primary-selection paste cannot land', () => {
    renderPanel()
    act(() => { ctl!.openFile('/srv/one.md', 'one', SLOT) })
    const ev = auxClick(fileTab('one\\.md'), 1)
    expect(ev.defaultPrevented).toBe(true)
  })

  it('does not select the tab on middle-click, and ignores a right-button aux click', () => {
    renderPanel()
    act(() => {
      ctl!.openFile('/srv/one.md', 'one', SLOT)
      ctl!.openFile('/srv/two.md', 'two', SLOT)
      ctl!.setActive('file:/srv/two.md')
    })
    expect(ctl!.activeId).toBe('file:/srv/two.md')

    // A right-button aux click on the inactive tab neither closes nor selects it.
    auxClick(fileTab('one\\.md'), 2)
    expect(ctl!.tabs.some(t => t.id === 'file:/srv/one.md')).toBe(true)
    expect(ctl!.activeId).toBe('file:/srv/two.md')

    // A middle-click closes the inactive tab without first making it active —
    // focus stays on the tab the user was on.
    auxClick(fileTab('one\\.md'), 1)
    expect(ctl!.tabs.some(t => t.id === 'file:/srv/one.md')).toBe(false)
    expect(ctl!.activeId).toBe('file:/srv/two.md')
  })

  it('leaves a non-closable pinned view alone on middle-click', () => {
    // Pinned views (Changes / Artifacts / Files) render without a close control
    // (`closable={false}`); the same guard must keep middle-click from closing
    // them, or the strip could lose a permanent tab to a stray aux click.
    renderPanel()
    const before = ctl!.tabs.map(t => t.id)
    expect(before).toContain('files')
    auxClick(fileTab('Files'), 1)
    expect(ctl!.tabs.map(t => t.id)).toEqual(before)
  })
})
