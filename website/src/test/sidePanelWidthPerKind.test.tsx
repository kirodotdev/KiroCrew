/**
 * Per-kind side panel width, end to end through the real strip.
 *
 * The panel remembers one width per tab bucket (`sidePanelWidthGroup`) instead
 * of one for the whole panel. Three contracts:
 *
 * 1. The rendered width follows the ACTIVE tab, so switching Git → Browser
 *    changes it without the user touching the handle.
 * 2. A drag persists under the active bucket's key alone, leaving every other
 *    bucket's stored width intact.
 * 3. A bucket with no stored width of its own inherits the pre-grouping key, so
 *    an existing install keeps the width it had rather than resetting.
 *
 * Bodies are stubbed as in sidePanelLeadingTab.test.tsx; only the strip, the
 * root's inline size and localStorage are driven.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { forwardRef } from 'react'
import { render, screen, act, cleanup, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
// forwardRef because the file tab passes a ref through; a plain function stub
// renders but warns, and the doc-bucket case here does open a file tab.
vi.mock('../components/MarkdownPanel', () => ({ default: forwardRef(() => null) }))
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
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'
import { SIDE_PANEL_WIDTH_KEY, sidePanelDimKey } from '../pages/chat/sidePanelWidth'
import { SIDE_PANEL_MOTION_MS, sidePanelDimTransition } from '../pages/chat/sidePanelMount'

/** Default width when nothing is stored for the bucket (SidePanel's own). */
const DEFAULT_W = 460

let ctl: ReturnType<typeof usePanelTabs> | null = null

type PanelProps = { expanded?: boolean; fillWidth?: number }

function Harness({ slot = 'chat-1', expanded, fillWidth }: { slot?: string } & PanelProps) {
  const tabsCtl = usePanelTabs(slot)
  ctl = tabsCtl
  return (
    <SidePanel
      tabsCtl={tabsCtl}
      slot={slot}
      onFileSave={async () => {}}
      onClose={() => {}}
      canDockBottom={false}
      expanded={expanded}
      fillWidth={fillWidth}
    />
  )
}

function renderPanel(props: PanelProps = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const store = createTestStore()
  // One client and one store across rerenders on purpose: a fresh pair would
  // remount the panel, and a remount resets the very motion state under test.
  const tree = (p: PanelProps) => (
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <Harness {...p} />
      </Provider>
    </QueryClientProvider>
  )
  const result = render(tree(props))
  return {
    ...result,
    /** Re-render with `next` merged over the props already in effect. */
    setProps: (next: PanelProps) => result.rerender(tree({ ...props, ...next })),
  }
}

/** The panel root's rendered width in px. */
const renderedWidth = () => screen.getByTestId('side-panel-root').style.width

/** The panel root's inline `transition`, empty string when it carries none. */
const transitionStyle = () => screen.getByTestId('side-panel-root').style.transition

/** Drag the left-edge resize handle by `dx` (negative widens).
 *
 *  The move and the release are separate `act` batches on purpose: the panel
 *  persists the width its last render committed, and a browser delivers
 *  pointermove and pointerup as separate tasks with a render between them. One
 *  batch would leave the release reading the pre-drag width — a harness
 *  artifact, not the behavior under test. */
function dragHandle(dx: number) {
  const handle = screen.getAllByRole('separator').find(el => el.getAttribute('aria-orientation') === 'vertical')!
  const from = 1000
  act(() => {
    fireEvent.pointerDown(handle, { pointerId: 1, pointerType: 'mouse', button: 0, clientX: from, clientY: 300 })
    fireEvent.pointerMove(handle, { pointerId: 1, pointerType: 'mouse', clientX: from + dx, clientY: 300 })
  })
  act(() => {
    fireEvent.pointerUp(handle, { pointerId: 1, pointerType: 'mouse', clientX: from + dx, clientY: 300 })
  })
}

/** Press the handle and move it WITHOUT releasing, so the panel is observed
 *  mid-drag. The release is the caller's job (or the test's cleanup). */
function startDrag(dx: number) {
  const handle = screen.getAllByRole('separator').find(el => el.getAttribute('aria-orientation') === 'vertical')!
  act(() => {
    fireEvent.pointerDown(handle, { pointerId: 1, pointerType: 'mouse', button: 0, clientX: 1000, clientY: 300 })
    fireEvent.pointerMove(handle, { pointerId: 1, pointerType: 'mouse', clientX: 1000 + dx, clientY: 300 })
  })
}

/** Release a gesture opened by {@link startDrag}, at the same offset. */
function releaseDrag(dx: number) {
  const handle = screen.getAllByRole('separator').find(el => el.getAttribute('aria-orientation') === 'vertical')!
  act(() => {
    fireEvent.pointerUp(handle, { pointerId: 1, pointerType: 'mouse', clientX: 1000 + dx, clientY: 300 })
  })
}

/** Wait out a group switch's tween so `dimAnimating` has cleared and the panel
 *  carries no transition. Without this the gate reads as open for the whole
 *  synchronous test and a "stays instant" assertion cannot fail. */
async function settleMotion() {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, SIDE_PANEL_MOTION_MS + 20))
  })
}

describe('SidePanel width per tab kind', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetPanelTabs()
    ctl = null
    // Wide enough that the responsive clamp (window − SIDE_PANEL_RESERVED_W)
    // never bites at the widths under test.
    window.innerWidth = 1600
  })

  it('renders the bucket default when nothing is stored', () => {
    renderPanel()
    act(() => { ctl!.openView('git') })
    expect(renderedWidth()).toBe(`${DEFAULT_W}px`)
  })

  it('follows the active tab: Git and Browser carry their own widths', () => {
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'git'), '380')
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'browser'), '900')
    renderPanel()
    act(() => { ctl!.openView('git') })
    expect(renderedWidth()).toBe('380px')
    act(() => { ctl!.openView('browser') })
    expect(renderedWidth()).toBe('900px')
    // And back — the switch reads the bucket, it does not overwrite it.
    act(() => { ctl!.setActive('git') })
    expect(renderedWidth()).toBe('380px')
  })

  it('persists a drag under the active bucket only', () => {
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'browser'), '900')
    renderPanel()
    act(() => { ctl!.openView('git') })
    dragHandle(-40)
    expect(localStorage.getItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'git'))).toBe(String(DEFAULT_W + 40))
    // The other bucket is untouched, in storage and on screen.
    expect(localStorage.getItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'browser'))).toBe('900')
    act(() => { ctl!.openView('browser') })
    expect(renderedWidth()).toBe('900px')
  })

  it('keeps a drag in the bucket it started on when the active tab changes mid-drag', () => {
    // An agent-driven app tab can auto-open while the handle is held
    // (`openApp` sets `activeId`). The gesture belongs to the bucket the user
    // grabbed, so the release lands there and the newcomer keeps its own width.
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'browser'), '900')
    renderPanel()
    act(() => { ctl!.openView('git') })

    startDrag(-40)
    act(() => { ctl!.openView('browser') })
    releaseDrag(-40)

    expect(localStorage.getItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'git'))).toBe(String(DEFAULT_W + 40))
    expect(localStorage.getItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'browser'))).toBe('900')
  })

  it('leaves the pre-grouping key alone, so a downgrade still finds a width', () => {
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '600')
    renderPanel()
    act(() => { ctl!.openView('git') })
    dragHandle(-40)
    expect(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)).toBe('600')
  })

  it('inherits the pre-grouping width in every bucket on upgrade', () => {
    localStorage.setItem(SIDE_PANEL_WIDTH_KEY, '600')
    renderPanel()
    act(() => { ctl!.openView('git') })
    expect(renderedWidth()).toBe('600px')
    act(() => { ctl!.openView('browser') })
    expect(renderedWidth()).toBe('600px')
  })

  it('shares one bucket across the document readers', () => {
    renderPanel()
    act(() => { ctl!.openFile('/repo/notes.md', '# notes') })
    dragHandle(-40)
    expect(localStorage.getItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'doc'))).toBe(String(DEFAULT_W + 40))
    act(() => { ctl!.openDiff('/repo/other.ts', 'a', 'b') })
    expect(renderedWidth()).toBe(`${DEFAULT_W + 40}px`)
  })

  it('eases the width on the panel\'s own open/close curve', () => {
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'git'), '380')
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'browser'), '900')
    renderPanel()
    act(() => { ctl!.openView('git') })
    // Same tween the dock wrapper opens with, so a switch reads as the same
    // gesture as opening the panel rather than as a jump.
    expect(transitionStyle()).toBe(sidePanelDimTransition('width').transition)
    act(() => { ctl!.openView('browser') })
    expect(renderedWidth()).toBe('900px')
    expect(transitionStyle()).toBe(sidePanelDimTransition('width').transition)
  })

  it('drops the transition while the handle is being dragged', () => {
    renderPanel()
    act(() => { ctl!.openView('git') })
    expect(transitionStyle()).toBe(sidePanelDimTransition('width').transition)
    // A transition during a drag makes the edge lag the pointer.
    startDrag(-40)
    expect(transitionStyle()).toBe('')
    expect(renderedWidth()).toBe(`${DEFAULT_W + 40}px`)
    // …and it comes back on release, with the width already settled so nothing
    // animates from the drag itself.
    act(() => {
      fireEvent.pointerUp(screen.getAllByRole('separator').find(el => el.getAttribute('aria-orientation') === 'vertical')!,
        { pointerId: 1, pointerType: 'mouse', clientX: 960, clientY: 300 })
    })
    expect(transitionStyle()).toBe(sidePanelDimTransition('width').transition)
  })

  // The transition is gated to the group switch this file's other tests cover.
  // Maximize and the browser tab's fill-width mode reach the same
  // `effectiveWidth`, and on the base branch they moved the edge INSTANTLY, so
  // easing them would be a behavior change this feature does not need. These
  // two guard that: without the gate the edge animates and both assertions fail.
  it('leaves maximize instant, as it was before per-kind width', async () => {
    localStorage.setItem(sidePanelDimKey(SIDE_PANEL_WIDTH_KEY, 'git'), '380')
    const { setProps } = renderPanel()
    act(() => { ctl!.openView('git') })
    await settleMotion()
    expect(transitionStyle()).toBe('')

    act(() => { setProps({ expanded: true }) })
    // The width really moved, so the assertion below is about motion rather
    // than about a prop that never took effect.
    expect(renderedWidth()).not.toBe('380px')
    expect(transitionStyle()).toBe('')
  })

  it('leaves the browser tab\'s fill-width mode instant', async () => {
    const { setProps } = renderPanel()
    act(() => { ctl!.openView('browser') })
    await settleMotion()
    expect(transitionStyle()).toBe('')

    act(() => { setProps({ fillWidth: 1200 }) })
    expect(renderedWidth()).toBe('1200px')
    expect(transitionStyle()).toBe('')
  })

  afterEach(() => cleanup())
})
