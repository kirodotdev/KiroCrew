/**
 * Regression for #4074: the side-panel PINNED group (`side-panel-fixed-tabs`)
 * scrolls under `scrollbar-none` just like the dynamic session-tab group, and
 * its own comment notes it overflows at 320px once a host prepends leading
 * chips — but it shipped with NO edge cue, so a clipped pinned/leading chip
 * row simply ended with no signal. The fix wires a second `useScrollEdges`
 * inline (the dynamic group already has one) to paint a gradient over the
 * clipped edge.
 *
 * Each spec names what reverting the fix breaks:
 *   - a group that fits shows no cue (a permanent fade would lie that chips are
 *     hidden),
 *   - a clipped group cues the hidden side only (no cue at all is the defect),
 *   - the cues follow the group as it scrolls (needs the hook's scroll
 *     listener, not a one-shot read).
 *
 * jsdom does no layout, so scroll geometry is stubbed on the prototype. Both
 * strip groups share `.overflow-x-auto`, so assertions target the
 * fixed-tabs cue test-ids specifically. Harness mirrors
 * sidePanelLeadingTab.test.tsx.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, act, cleanup } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => <div data-testid="web-preview-body" /> }))
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
import { usePanelTabs } from '../hooks/usePanelTabs'
import type { SidePanelLeadingTab } from '../pages/chat/SidePanel'

const THREE_IDS = ['crew-notes', 'crew-work-log', 'crew-dashboard'] as const
const THREE_TITLES = ['Notes', 'Work log', 'Dashboard'] as const

function leadingTabsFor(ids: readonly string[]): SidePanelLeadingTab[] {
  return ids.map(id => {
    const title = THREE_TITLES[THREE_IDS.indexOf(id as typeof THREE_IDS[number])]
    return {
      id,
      title,
      icon: <span data-testid={`leading-icon-${id}`} />,
      render: () => <div data-testid={`leading-body-${id}`}>{`radar ${title.toLowerCase()}`}</div>,
    }
  })
}

/** The pinned group only overflows once a host prepends leading chips, so the
 *  harness supplies all three leading tabs — the strip's own comment names this
 *  as the overflow case. */
function Harness() {
  const slot = 'member-radar'
  const tabsCtl = usePanelTabs(slot, undefined, { leadingIds: THREE_IDS })
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot={slot}
      onFileSave={async () => {}}
      onClose={undefined}
      canDockBottom={false}
      onActiveTabChange={() => {}}
      leadingTabs={leadingTabsFor(THREE_IDS)}
    />
  )
}

function renderPanel() {
  const store = createTestStore()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <Harness />
      </QueryClientProvider>
    </Provider>,
  )
}

/** `hidden` px of content beyond the right edge, `scrolled` px already past the left. */
function stubGeometry({ hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(300)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(300 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(scrolled)
}

const leftCue = () => screen.queryByTestId('side-panel-fixed-tabs-cue-left')
const rightCue = () => screen.queryByTestId('side-panel-fixed-tabs-cue-right')
const fixedScroller = () => document.querySelector('[data-testid="side-panel-fixed-tabs"]') as HTMLElement

describe('SidePanel fixed-tabs scroll-edge cues (#4074)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    cleanup()
  })

  it('shows no cue when the pinned group fits', () => {
    stubGeometry({ hidden: 0 })
    renderPanel()
    act(() => { fixedScroller().dispatchEvent(new Event('scroll')) })
    expect(leftCue()).toBeNull()
    expect(rightCue()).toBeNull()
  })

  it('cues only the clipped (right) edge at the start of an overflowing group', () => {
    stubGeometry({ hidden: 400, scrolled: 0 })
    renderPanel()
    act(() => { fixedScroller().dispatchEvent(new Event('scroll')) })
    expect(leftCue()).toBeNull()
    expect(rightCue()).not.toBeNull()
  })

  it('cues both edges when scrolled into the middle', () => {
    stubGeometry({ hidden: 400, scrolled: 200 })
    renderPanel()
    act(() => { fixedScroller().dispatchEvent(new Event('scroll')) })
    expect(leftCue()).not.toBeNull()
    expect(rightCue()).not.toBeNull()
  })
})
