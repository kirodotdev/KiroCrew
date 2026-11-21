/**
 * The chat page's panes in book pose (lib/foldCompanion.ts): the
 * transcript always holds one segment; an open side panel takes the other at
 * that segment's reported width and the sessions list steps aside; with the
 * panel closed the list and the transcript split at the fold. Readings are the
 * iPhone Duo's: a Safari tab at 951px reports two 475.5px segments, the Home
 * Screen web app at 867px reports 475.5px and 391.5px.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'
import type { ViewportSegment } from '../hooks/useViewportSegments'

const segmentsMock = vi.hoisted(() => ({ value: null as null | ViewportSegment[] }))
vi.mock('../hooks/useViewportSegments', async (orig) => ({
  ...(await orig<typeof import('../hooks/useViewportSegments')>()),
  useViewportSegments: () => segmentsMock.value,
}))
const safe = vi.hoisted(() => ({ left: 0, right: 0 }))
vi.mock('../hooks/useDrawerSwipe', async (orig) => ({
  ...(await orig<typeof import('../hooks/useDrawerSwipe')>()),
  useSafeInsets: () => ({ left: safe.left, right: safe.right }),
}))
const morph = vi.hoisted(() => ({ calls: [] as boolean[] }))
vi.mock('../lib/layoutFormTransition', async (orig) => ({
  ...(await orig<typeof import('../lib/layoutFormTransition')>()),
  // Records the morph and applies at once, as the module does without the API.
  runPaneAllocationChange: (apply: () => void, hidesSessions: boolean) => { morph.calls.push(hidesSessions); apply() },
}))

import {
  foldCompanionApplies, foldCompanionShown, foldGapWidth, foldPanelWidth,
  foldYieldToList, setFoldCompanionShown, useFoldCompanionLayout, useFoldCompanionYieldsToReveal, yieldFoldCompanion,
} from '../lib/foldCompanion'
import { panelInlineHost } from '../lib/touchLayoutForm'
import { usePanelLayout } from '../pages/chat/page/paneLayout'
import { foldHoldsRailCompact, SIDEBAR_MIN } from '../pages/chat/sidebarWidth'
import { CHAT_PANE_MIN_W } from '../pages/chat/SidePanel'

const TAB: ViewportSegment[] = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]
const WEB_APP: ViewportSegment[] = [{ x: 0, width: 475.5 }, { x: 475.5, width: 391.5 }]

afterEach(() => {
  cleanup()
  segmentsMock.value = null
  morph.calls = []
  safe.left = 0
  safe.right = 0
  document.documentElement.removeAttribute('dir')
})

describe('which layout the book pose gets', () => {
  const full = { desktop: true, listHost: true }
  it('panel open beside a fold: the companion layout', () => {
    expect(foldCompanionApplies({ segments: TAB, ...full, panelShown: true })).toBe(true)
  })
  it('panel closed beside a fold: the chat page split, the list stays', () => {
    expect(foldCompanionApplies({ segments: TAB, ...full, panelShown: false })).toBe(false)
  })
  it('flat (no segments reported): nothing changes', () => {
    expect(foldCompanionApplies({ segments: null, ...full, panelShown: true })).toBe(false)
  })
  it('the phone form, or a chat with no sessions list beside it: nothing changes', () => {
    expect(foldCompanionApplies({ segments: TAB, desktop: false, listHost: true, panelShown: true })).toBe(false)
    expect(foldCompanionApplies({ segments: TAB, desktop: true, listHost: false, panelShown: true })).toBe(false)
  })
})

describe("the panel stays inside the shell's safe-area box beside a hinge", () => {
  // 900px, segments 0-440 and 460-900, a 42px trailing inset: the shell's column
  // ends at 858, so a 440px panel would sit at 418-858, across the hinge.
  const HINGE: ViewportSegment[] = [{ x: 0, width: 440 }, { x: 460, width: 440 }]
  it('ends where the box does and starts past the fold, never at or before 440', () => {
    const width = foldPanelWidth({ segments: HINGE, leading: 'left', insets: { left: 0, right: 42 } })
    const panelStart = (900 - 42) - width
    expect(width).toBe(398)
    expect(panelStart).toBeGreaterThanOrEqual(460)
  })
  it('right to left: the trailing segment is the left-hand one, narrowed by the left inset', () => {
    expect(foldPanelWidth({ segments: HINGE, leading: 'right', insets: { left: 42, right: 0 } })).toBe(398)
  })
  it('takes the whole segment with no insets, as before', () => {
    expect(foldPanelWidth({ segments: HINGE, leading: 'left' })).toBe(440)
  })
})

describe('the panel takes the trailing segment at its reported width', () => {
  it('in a Safari tab', () => {
    expect(foldPanelWidth({ segments: TAB, leading: 'left' })).toBe(475.5)
  })
  it('in the Home Screen web app, with unequal halves', () => {
    expect(foldPanelWidth({ segments: WEB_APP, leading: 'left' })).toBe(391.5)
  })
  it('right to left: the trailing segment is the left-hand one', () => {
    expect(foldPanelWidth({ segments: WEB_APP, leading: 'right' })).toBe(475.5)
  })
})

describe("the side panel's host", () => {
  // matchMedia as a touch-first phone browser in mobile mode answers it, at a
  // desktop width, with the fold query on or off.
  function stubDevice({ touchFirst, mobileUa, narrow, folded }: { touchFirst: boolean; mobileUa: boolean; narrow: boolean; folded: boolean }) {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q.includes('pointer: coarse') || q.includes('hover: none') ? touchFirst
        : q.includes('max-width') ? narrow
        : q.includes('viewport-segments') ? folded
        : false,
      media: q, addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {},
    }))
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(mobileUa ? 'Mozilla/5.0 (Linux; Android 14) Mobile Safari/537.36' : 'Mozilla/5.0 (X11; Linux x86_64)')
  }
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

  it('stays inline when a fold turns a touch-first phone to the desktop form, and as the unfold lands', () => {
    stubDevice({ touchFirst: true, mobileUa: true, narrow: false, folded: true })
    expect(panelInlineHost({ isMobile: false })).toBe(true)
    stubDevice({ touchFirst: true, mobileUa: true, narrow: false, folded: false })
    expect(panelInlineHost({ isMobile: false })).toBe(true)
    expect(panelInlineHost({ isMobile: true })).toBe(true)
  })
  it('is the column in a desktop window, folded or flat', () => {
    stubDevice({ touchFirst: false, mobileUa: false, narrow: false, folded: true })
    expect(panelInlineHost({ isMobile: false })).toBe(false)
    stubDevice({ touchFirst: false, mobileUa: false, narrow: false, folded: false })
    expect(panelInlineHost({ isMobile: false })).toBe(false)
  })
  it('follows the phone/desktop form across a resize, as before', () => {
    stubDevice({ touchFirst: false, mobileUa: false, narrow: true, folded: false })
    expect(panelInlineHost({ isMobile: true })).toBe(true)
    stubDevice({ touchFirst: false, mobileUa: false, narrow: false, folded: false })
    expect(panelInlineHost({ isMobile: false })).toBe(false)
  })
})

describe('a reported fold gap', () => {
  it('is 0 where the segments meet, as on the iPhone Duo', () => {
    expect(foldGapWidth(TAB)).toBe(0)
    expect(foldGapWidth(WEB_APP)).toBe(0)
  })
  it('is the masked hinge where the device reports one', () => {
    expect(foldGapWidth([{ x: 0, width: 440 }, { x: 460, width: 440 }])).toBe(20)
  })
})

describe('a request to show the list while the companion layout holds', () => {
  afterEach(() => setFoldCompanionShown(false))
  type Req = { nonce: number; openFoldersOnly?: boolean } | null
  const mount = (companion: boolean, revealRequest: Req) => {
    const yieldToList = vi.fn()
    setFoldCompanionShown(companion)
    const hook = renderHook((p: { companion: boolean; revealRequest: Req }) =>
      useFoldCompanionYieldsToReveal({ companion: p.companion, revealRequest: p.revealRequest, yieldToList }), { initialProps: { companion, revealRequest } })
    return { yieldToList, rerender: hook.rerender }
  }
  it('a new session or folder reveal closes the panel and pins the list', () => {
    const { yieldToList, rerender } = mount(true, null)
    rerender({ companion: true, revealRequest: { nonce: 1 } })
    expect(yieldToList).toHaveBeenCalledTimes(1)
  })
  it("leaves the panel for a new chat's folder-open request", () => {
    const { yieldToList, rerender } = mount(true, null)
    rerender({ companion: true, revealRequest: { nonce: 1, openFoldersOnly: true } })
    expect(yieldToList).not.toHaveBeenCalled()
  })
  it('a request left in the store from before the layout appeared does not close panels opened later', () => {
    const { yieldToList, rerender } = mount(false, { nonce: 4 })
    setFoldCompanionShown(true)
    rerender({ companion: true, revealRequest: { nonce: 4 } })
    expect(yieldToList).not.toHaveBeenCalled()
    rerender({ companion: false, revealRequest: { nonce: 4 } })
    rerender({ companion: true, revealRequest: { nonce: 4 } })
    expect(yieldToList).not.toHaveBeenCalled()
  })
  it('does nothing without the companion layout', () => {
    const { yieldToList, rerender } = mount(false, null)
    rerender({ companion: false, revealRequest: { nonce: 1 } })
    expect(yieldToList).not.toHaveBeenCalled()
  })
})

describe('yielding the companion layout to the list', () => {
  it('pins the list, closes the panel, and clears the preview auto-hide so its end cannot unpin the list', () => {
    const autoHidden = { current: false as boolean | null }
    const setPinned = vi.fn()
    const closePanel = vi.fn()
    foldYieldToList({ autoHidden, setPinned, closePanel })()
    expect(autoHidden.current).toBeNull()
    expect(setPinned).toHaveBeenCalledWith(true)
    expect(closePanel).toHaveBeenCalledTimes(1)
  })
})

describe('the rail beside the companion layout', () => {
  const expandedRailW = 236
  it('stays compact so the transcript keeps the chat pane minimum in the leading segment', () => {
    expect(foldHoldsRailCompact({ segments: TAB, expandedRailW, leading: 'left', paneMin: CHAT_PANE_MIN_W })).toBe(true)
  })
  it('may expand beside the sessions list, as before', () => {
    expect(foldHoldsRailCompact({ segments: TAB, expandedRailW, leading: 'left' })).toBe(false)
    expect(expandedRailW + SIDEBAR_MIN).toBeLessThan(475.5)
  })
})

describe('the layout on screen trails a panel toggle by one morph', () => {
  const run = (initial: { wanted: boolean; segments: ViewportSegment[] | null; isMobile?: boolean }) =>
    renderHook((p: { wanted: boolean; segments: ViewportSegment[] | null; isMobile?: boolean }) =>
      useFoldCompanionLayout({ wanted: p.wanted, segments: p.segments, isMobile: p.isMobile ?? false, publish: true }), { initialProps: initial })

  it('the panel opening beside a fold that holds still runs the morph that hides the list', () => {
    const { result, rerender } = run({ wanted: false, segments: TAB })
    rerender({ wanted: true, segments: TAB })
    expect(morph.calls).toEqual([true])
    expect(result.current).toBe(true)
    expect(foldCompanionShown()).toBe(true)
  })

  it('the panel closing runs the morph that brings the list back', () => {
    const { result, rerender } = run({ wanted: true, segments: TAB })
    rerender({ wanted: false, segments: TAB })
    expect(morph.calls).toEqual([false])
    expect(result.current).toBe(false)
    expect(foldCompanionShown()).toBe(false)
  })

  it('the render a fold arrives in already shows the companion layout, so an open panel is never drawn hidden', () => {
    const seenByRender: boolean[] = []
    const { rerender } = renderHook((p: { wanted: boolean; segments: ViewportSegment[] | null }) => {
      const shown = useFoldCompanionLayout({ wanted: p.wanted, segments: p.segments, isMobile: false, publish: false })
      seenByRender.push(shown)
      return shown
    }, { initialProps: { wanted: false, segments: null as ViewportSegment[] | null } })
    seenByRender.length = 0
    rerender({ wanted: true, segments: TAB })
    expect(seenByRender[0]).toBe(true)
    expect(seenByRender.every(Boolean)).toBe(true)
  })

  it('touch-first sequence: phone form flat, a fold, the desktop form, the shell slot a render later: the open panel is drawn open in every render', () => {
    // As sessionsDrawer composes it: the store has the panel open, the layout
    // wanted follows the form and segments, and the panel is drawn open while
    // the layout on screen agrees with the wanted one, else as the screen has it.
    // The shell slot settling a render later only re-renders: the rule no longer
    // reads the slot (`listHost` is the full chat page), so it cannot lag there.
    const drawnOpen: boolean[] = []
    const { rerender } = renderHook((p: { isMobile: boolean; segments: ViewportSegment[] | null; slotRev: number }) => {
      const wanted = foldCompanionApplies({ segments: p.segments, desktop: !p.isMobile, listHost: true, panelShown: true })
      const shown = useFoldCompanionLayout({ wanted, segments: p.segments, isMobile: p.isMobile, publish: false })
      drawnOpen.push(shown === wanted ? true : shown)
      return p.slotRev
    }, { initialProps: { isMobile: true, segments: null as ViewportSegment[] | null, slotRev: 0 } })
    rerender({ isMobile: true, segments: TAB, slotRev: 0 })
    rerender({ isMobile: false, segments: TAB, slotRev: 0 })
    rerender({ isMobile: false, segments: TAB, slotRev: 1 })
    rerender({ isMobile: false, segments: null, slotRev: 1 })
    rerender({ isMobile: true, segments: null, slotRev: 1 })
    expect(drawnOpen.every(Boolean)).toBe(true)
    expect(morph.calls).toEqual([])
  })

  it('a fold or unfold lands at once: the form switch owns that motion', () => {
    const { result, rerender } = run({ wanted: false, segments: null })
    rerender({ wanted: true, segments: TAB })
    expect(result.current).toBe(true)
    rerender({ wanted: false, segments: null })
    expect(result.current).toBe(false)
    expect(morph.calls).toEqual([])
  })

  it('unmounting the page clears the shell flag', () => {
    const { unmount } = run({ wanted: true, segments: TAB })
    expect(foldCompanionShown()).toBe(true)
    unmount()
    expect(foldCompanionShown()).toBe(false)
  })
})

describe('asking for the sessions list while the companion holds', () => {
  it('closes the panel, so the list takes the leading segment back', () => {
    const { unmount } = renderHook(() => useFoldCompanionLayout({ wanted: true, segments: TAB, isMobile: false, publish: true }))
    const close = vi.fn()
    expect(yieldFoldCompanion(close)).toBe(true)
    expect(close).toHaveBeenCalledTimes(1)
    unmount()
  })
  it('does nothing without the companion layout', () => {
    const close = vi.fn()
    expect(yieldFoldCompanion(close)).toBe(false)
    expect(close).not.toHaveBeenCalled()
  })
})

describe('the side panel width beside a fold (usePanelLayout)', () => {
  const options = (over: Partial<Parameters<typeof usePanelLayout>[0]> = {}) => ({
    mode: '', dispatch: vi.fn() as never, isMobile: false, filteredSlots: [], slotsLoaded: true, activeSlot: null, isWelcomeState: false,
    showActionError: vi.fn(), currentSlot: undefined, chatFolders: [], chatFoldersError: null, chatFoldersLoaded: true, mobileNewSessionBusy: false,
    mobileNewSessionInFlightRef: { current: false }, setMobileNewSessionBusy: vi.fn(), setSidebarPinned: vi.fn(), sidebarPinned: true,
    railWidth: 74, winW: 951, effectiveSidebarWidth: 401.5, mobileSessions: false, openSidebar: vi.fn(), closeSidebar: vi.fn(), previewExpanded: false,
    sidebarOpen: true, flyout: { close: vi.fn() } as never, memorySwitch: { lockRef: { current: false } } as never,
    ...over,
  })

  beforeEach(() => { document.documentElement.setAttribute('dir', 'ltr') })

  it('flat at 951px: a panel with no room beside the list fills the chat column, as before', () => {
    const { result } = renderHook(() => usePanelLayout(options()))
    expect(result.current.panelFillWidth).toBe(951 - 74 - 401.5)
    expect(result.current.panelFoldWidth).toBeUndefined()
  })

  it('companion layout: the fill is suppressed and the panel takes the trailing segment', () => {
    segmentsMock.value = TAB
    const { result } = renderHook(() => usePanelLayout(options({ foldCompanion: true })))
    expect(result.current.panelFillWidth).toBeUndefined()
    expect(result.current.panelFoldWidth).toBe(475.5)
    expect(result.current.panelFoldInset).toBe(0)
  })

  it('companion layout beside a hinge with a 42px trailing safe-area inset: the panel ends at the box, past the fold', () => {
    segmentsMock.value = [{ x: 0, width: 440 }, { x: 460, width: 440 }]
    safe.right = 42
    const { result } = renderHook(() => usePanelLayout(options({ winW: 900, foldCompanion: true })))
    expect(result.current.panelFoldWidth).toBe(398)
    expect(result.current.panelFoldInset).toBe(20)
  })

  it('companion layout in the web app: the panel takes the 391.5px trailing segment', () => {
    segmentsMock.value = WEB_APP
    const { result } = renderHook(() => usePanelLayout(options({ winW: 867, foldCompanion: true })))
    expect(result.current.panelFillWidth).toBeUndefined()
    expect(result.current.panelFoldWidth).toBe(391.5)
  })

  it('companion layout right to left: the panel takes the left-hand segment and keeps the chat pane reclaim clear', () => {
    segmentsMock.value = WEB_APP
    document.documentElement.setAttribute('dir', 'rtl')
    const { result } = renderHook(() => usePanelLayout(options({ winW: 867, foldCompanion: true, collapsedReclaim: 8 })))
    expect(result.current.panelFoldWidth).toBe(475.5)
    expect(result.current.panelFoldInset).toBe(8)
  })

  it('left to right the reclaim pulls toward the rail, so the panel keeps only the fold gap', () => {
    segmentsMock.value = TAB
    const { result } = renderHook(() => usePanelLayout(options({ foldCompanion: true, collapsedReclaim: 8 })))
    expect(result.current.panelFoldInset).toBe(0)
  })

  it('companion layout with a masked hinge: the gap is reserved before the panel', () => {
    segmentsMock.value = [{ x: 0, width: 440 }, { x: 460, width: 440 }]
    const { result } = renderHook(() => usePanelLayout(options({ winW: 900, foldCompanion: true })))
    expect(result.current.panelFoldWidth).toBe(440)
    expect(result.current.panelFoldInset).toBe(20)
  })

  it('folded without the companion layout (an embedded chat, a popout): the panel width rules are as before', () => {
    segmentsMock.value = TAB
    const { result } = renderHook(() => usePanelLayout(options()))
    expect(result.current.panelFoldWidth).toBeUndefined()
    expect(result.current.panelFillWidth).toBe(951 - 74 - 401.5)
  })
})

describe('the shell flag has one writer', () => {
  it('a chat with no list leaves it alone', () => {
    const page = renderHook(() => useFoldCompanionLayout({ wanted: true, segments: TAB, isMobile: false, publish: true }))
    const embedded = renderHook(() => useFoldCompanionLayout({ wanted: false, segments: TAB, isMobile: false, publish: false }))
    act(() => {})
    expect(foldCompanionShown()).toBe(true)
    embedded.unmount()
    expect(foldCompanionShown()).toBe(true)
    page.unmount()
  })
})
