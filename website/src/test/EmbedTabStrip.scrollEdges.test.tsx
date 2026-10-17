/**
 * The embed tab strip hides its scrollbar entirely (`scrollbarWidth: none`),
 * so once tabs overflow there is NO signal at all that more exist — the strip
 * simply ends. The cue is the shared `useScrollEdges` measurement painting a
 * gradient over the clipped edge, the same treatment the sibling strips
 * (FollowUpBar's scroll row, SidePanelLayout's tab strip, the file-explorer TabStrip)
 * ship.
 *
 * These tests pin the wiring, and each names what reverting it breaks:
 *
 *   - a strip that fits shows no cue (revert symptom: a permanent fade lies
 *     that tabs are hidden),
 *   - a clipped strip cues the hidden side only (revert symptom: no signal at
 *     all — the original defect),
 *   - the cues follow the strip as it scrolls (needs the hook's scroll
 *     listener, not a one-shot read),
 *   - the merged callback ref keeps the plain stripRef alive: the wheel
 *     translation the strip already shipped must still land on the same node
 *     (revert symptom: wiring the hook silently kills wheel scrolling).
 *
 * jsdom does no layout, so scroll geometry is stubbed — the stub is what makes
 * the derivation testable, mirroring FollowUpBar.scrollEdges.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, cleanup, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import EmbedTabStrip from '../components/EmbedTabStrip'
import { updateSlot } from '../store/dashboardSlice'
import type { RootState } from '../store'

/** `hidden` px of content beyond the right edge, `scrolled` px already past the left. */
function stubGeometry({ hidden, scrolled = 0 }: { hidden: number; scrolled?: number }) {
  const proto = window.HTMLElement.prototype
  vi.spyOn(proto, 'clientWidth', 'get').mockReturnValue(320)
  vi.spyOn(proto, 'scrollWidth', 'get').mockReturnValue(320 + hidden)
  vi.spyOn(proto, 'scrollLeft', 'get').mockReturnValue(scrolled)
}

type Slots = RootState['dashboard']['slots']

const SLOTS = [
  { key: 'chat-1', title: 'First Chat' },
  { key: 'chat-2', title: 'Second Chat' },
] as unknown as Slots

/** Tab state the host plugin injects onto `window`. */
interface EmbedTabsWindow extends Window {
  __kirocrewTabs?: string[]
  __kirocrewActiveTabIndex?: number
}
const tabsWindow = window as EmbedTabsWindow

function makeStore() {
  const defaults = createTestStore().getState()
  return createTestStore({
    dashboard: { ...defaults.dashboard, slots: SLOTS },
    chat: { ...defaults.chat, activeSlot: 'chat-1' },
  })
}

function renderStrip() {
  return renderWithProviders(<EmbedTabStrip />, {
    store: makeStore(),
    route: '/embed/chat/chat-1',
  })
}

const leftCue = () => screen.queryByTestId('embed-tab-strip-cue-left')
const rightCue = () => screen.queryByTestId('embed-tab-strip-cue-right')
const scrollerOf = (container: HTMLElement) =>
  container.querySelector('.overflow-x-auto') as HTMLElement

describe('EmbedTabStrip scroll-edge cues', () => {
  beforeEach(() => {
    sessionStorage.clear()
    tabsWindow.__kirocrewTabs = ['chat-1', 'chat-2']
    tabsWindow.__kirocrewActiveTabIndex = 0
    if (!window.ResizeObserver) {
      window.ResizeObserver = class {
        observe() {}
        unobserve() {}
        disconnect() {}
      } as unknown as typeof ResizeObserver
    }
  })
  afterEach(() => {
    vi.restoreAllMocks()
    cleanup()
    delete tabsWindow.__kirocrewTabs
    delete tabsWindow.__kirocrewActiveTabIndex
    sessionStorage.clear()
  })

  it('shows no cue when every tab fits', () => {
    stubGeometry({ hidden: 0 })
    renderStrip()
    expect(leftCue()).toBeNull()
    expect(rightCue()).toBeNull()
  })

  it('cues only the side hiding content when the strip overflows', () => {
    stubGeometry({ hidden: 240 })
    renderStrip()
    expect(rightCue()).toBeTruthy()
    // The cue is paint, not surface: it sits over the edge tabs, so letting it
    // catch clicks would put a dead zone on the tab (and its close button)
    // underneath, and it must stay silent to assistive tech.
    expect(rightCue()).toHaveClass('pointer-events-none')
    expect(rightCue()).toHaveAttribute('aria-hidden', 'true')
    // Nothing is hidden to the left at offset 0; a cue there would point at
    // content that does not exist.
    expect(leftCue()).toBeNull()
  })

  it('follows the strip as it scrolls', () => {
    stubGeometry({ hidden: 240 })
    const { container } = renderStrip()
    expect(leftCue()).toBeNull()

    // Scrolled to the far end: the hidden side flips.
    stubGeometry({ hidden: 240, scrolled: 240 })
    fireEvent.scroll(scrollerOf(container))
    expect(leftCue()).toBeTruthy()
    expect(rightCue()).toBeNull()
  })

  it('keeps the wheel translation working on the same node', () => {
    // The hook attaches through a merged callback ref; if that merge drops the
    // plain stripRef, the onWheel handler reads a null ref and the gesture
    // dies. scrollLeft is stubbed as a read-only getter, so observe that the
    // handler runs without throwing on the node the hook is bound to.
    stubGeometry({ hidden: 240 })
    const { container } = renderStrip()
    const scroller = scrollerOf(container)
    expect(() => fireEvent.wheel(scroller, { deltaY: 40 })).not.toThrow()
    // And the hook is live on that same node: scrolling it moves the cue.
    stubGeometry({ hidden: 240, scrolled: 240 })
    fireEvent.scroll(scroller)
    expect(leftCue()).toBeTruthy()
  })

  it('remeasures when a slot is retitled, without any scroll, resize, or tab change', () => {
    // Auto-titling: a fresh tab renders as its bare slug and is retitled after
    // the first turn. The label comes from redux `slots`, so `tabs` stays
    // identity-stable and the strip keeps its own box — neither the tab-keyed
    // half of the remeasure effect, nor the ResizeObserver, nor a scroll event
    // reports the wider content. Only the slots-keyed remeasure can. Reverting
    // that dep leaves a freshly retitled strip clipping with no cue (the
    // original defect, with scrollbarWidth: none leaving no fallback signal).
    stubGeometry({ hidden: 0 })
    const store = makeStore()
    renderWithProviders(<EmbedTabStrip />, { store, route: '/embed/chat/chat-1' })
    expect(rightCue()).toBeNull()

    vi.restoreAllMocks()
    stubGeometry({ hidden: 240 })
    act(() => {
      store.dispatch(updateSlot({ key: 'chat-1', title: 'A much longer auto-generated session title' }))
    })
    expect(rightCue()).toBeTruthy()
  })
})
