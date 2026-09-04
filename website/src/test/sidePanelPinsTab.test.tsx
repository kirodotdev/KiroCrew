/**
 * Pins is an ON-DEMAND view, deliberately NOT a member of the default pinned
 * button group at the front of the tab strip.
 *
 * That block (Changes / Files / Artifacts) is prime real estate: always visible,
 * non-closable, ahead of every dynamic tab. Pins is not important enough to hold
 * a slot in it — a maintainer decision, and the reason these tests exist. An
 * earlier revision DID pin it (content-driven from `pins.length`) to avoid a
 * reveal claim; the claim is still gone, but the tab is reached the way Issues is
 * reached instead: the `+` menu, or ChatPage opening it on a session's first pin.
 *
 * So the ratchet has two halves: Pins must not appear in the strip on its own
 * (even for a session that HAS pins), and it must remain openable from the menu.
 * Re-pinning it would silently take the slot back.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import type { ChatPin } from '../api/pins'

// Heavy tab bodies are not what this drives -- only the strip's composition.
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
import { PINNED_VIEWS, usePanelTabs } from '../hooks/usePanelTabs'

const pin = (id: string): ChatPin => ({
  id, slot_key: 'slot-a', mid: `m-${id}`, message_ts: 'u1',
  role: 'user', preview: 'pinned', pinned_at: '2026-08-01T12:00:00Z',
})

function Harness({ pins }: { pins: ChatPin[] }) {
  const tabsCtl = usePanelTabs('slot-a')
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot="slot-a"
      pins={pins}
      onFileSave={async () => {}}
      onClose={() => {}}
    />
  )
}

function renderPanel(pins: ChatPin[]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Provider store={createTestStore()}>
        <Harness pins={pins} />
      </Provider>
    </QueryClientProvider>,
  )
}

const pinsTabChip = () => screen.queryByRole('tab', { name: /Pins/ })

describe('Pins stays out of the default pinned button group', () => {
  beforeEach(() => { localStorage.clear() })

  it('is not a member of PINNED_VIEWS', () => {
    // The list itself is the contract the strip renders from, so assert it
    // directly -- a future edit that re-pins Pins fails here first.
    expect(PINNED_VIEWS).not.toContain('pins')
  })

  it('shows no Pins tab for a session with no pins', () => {
    renderPanel([])
    expect(pinsTabChip()).toBeNull()
  })

  it('shows no Pins tab even for a session that HAS pins', () => {
    // The distinguishing case. A content-driven pinned entry would surface the
    // tab here; an on-demand view must not, or it is back in the default group
    // under a different name.
    renderPanel([pin('p1'), pin('p2')])
    expect(pinsTabChip()).toBeNull()
  })

  it('is still openable from the + menu', () => {
    renderPanel([])
    act(() => {
      fireEvent.pointerDown(
        screen.getByRole('button', { name: 'Open side panel tab' }),
        { button: 0, ctrlKey: false, pointerType: 'mouse' },
      )
    })
    act(() => { fireEvent.click(screen.getByRole('menuitem', { name: 'Pins' })) })
    expect(pinsTabChip()).not.toBeNull()
  })
})
