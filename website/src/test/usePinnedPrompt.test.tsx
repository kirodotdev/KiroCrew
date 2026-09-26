import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '../types'
import type { DisplayItem } from '../pages/chat/types'
import { usePinnedPrompt } from '../pages/chat/usePinnedPrompt'

/**
 * chat-core P5-d: the pinned-prompt geometry extracted from the main chat's
 * transcript controller into a host-agnostic hook. These tests pin the hook's
 * OWN contract — the part both ChatPage and ChatPane now share — at the seam
 * a layout-less DOM can still reach: hand-crafted rects on `[data-display-index]`
 * rows, the fold sentinel and the card.
 *
 *   1. it derives the pinned prompt from live geometry and the host's list,
 *   2. the per-scroll recompute coalesces to one animation frame,
 *   3. disabling drops the banner and re-enabling brings it back,
 *   4. the in-place jump glides the scroller to the target row minus the
 *      banner chrome (by live rect when mounted, by the host's estimate until
 *      then), and a user scroll aborts it.
 *
 * ChatPage's own virtualized jump stays covered by
 * useChatPageTranscriptEarlyController.coverage.test.tsx.
 */

interface QueuedFrame { id: number; cb: FrameRequestCallback }

let frames: QueuedFrame[] = []
let nextFrameId = 1
let clock = 0
let originalRaf: typeof requestAnimationFrame
let originalCancelRaf: typeof cancelAnimationFrame
let nowSpy: ReturnType<typeof vi.spyOn>
const detachedNodes: HTMLElement[] = []

const rect = (top: number, height: number): DOMRect => ({
  top, bottom: top + height, left: 0, right: 0, width: 0, height, x: 0, y: top, toJSON: () => ({}),
}) as DOMRect

function setRect(el: HTMLElement, top: number, height: number) {
  Object.defineProperty(el, 'getBoundingClientRect', { configurable: true, value: () => rect(top, height) })
}

function flushFrame(at: number) {
  const frame = frames.shift()
  expect(frame, 'expected a queued animation frame').toBeDefined()
  clock = at
  act(() => { frame!.cb(at) })
}

const message = (role: string, content: string, ts: string): ChatMessage => ({ role, content, cls: '', ts })
const single = (idx: number, role: string, content: string): DisplayItem => ({
  kind: 'single', idx, msg: message(role, content, `2026-09-08T00:00:0${idx}.000Z`),
})

function mountGeometry(rowCount: number) {
  const scroller = document.createElement('div')
  const fold = document.createElement('div')
  const card = document.createElement('div')
  scroller.append(fold, card)
  const rows = Array.from({ length: rowCount }, (_, index) => {
    const row = document.createElement('div')
    row.dataset.displayIndex = String(index)
    scroller.append(row)
    return row
  })
  document.body.append(scroller)
  detachedNodes.push(scroller)

  setRect(scroller, 0, 400)
  setRect(fold, 100, 0)
  setRect(card, 104, 70)
  rows.forEach((row, index) => {
    // index 3 is the first row below the hand-off line; index 4 is the next
    // prompt, mounted far enough down that it does not push the pinned card.
    const top = [0, 50, 100, 160, 300][index] ?? (300 + index * 100)
    setRect(row, top, 40)
  })

  let scrollTop = 200
  Object.defineProperties(scroller, {
    clientHeight: { configurable: true, get: () => 200 },
    scrollHeight: { configurable: true, get: () => 2000 },
    scrollTop: { configurable: true, get: () => scrollTop, set: (next: number) => { scrollTop = next } },
  })
  return { scroller, fold, card, rows, get scrollTop() { return scrollTop } }
}

function renderPin(requiresMountedHandoff = false) {
  const scrollerRef = { current: null as HTMLDivElement | null }
  const hook = renderHook(() => usePinnedPrompt({ scrollerRef, requiresMountedHandoff }))
  return { ...hook, scrollerRef }
}

const ITEMS: DisplayItem[] = [
  single(0, 'user', 'first prompt'),
  single(1, 'assistant', 'first reply'),
  single(2, 'user', 'second prompt ![shot](/tmp/shot.png)'),
  single(3, 'assistant', 'second reply'),
  single(4, 'user', 'next prompt'),
]

beforeEach(() => {
  frames = []
  nextFrameId = 1
  clock = 0
  originalRaf = globalThis.requestAnimationFrame
  originalCancelRaf = globalThis.cancelAnimationFrame
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    const id = nextFrameId++
    frames.push({ id, cb })
    return id
  }) as typeof requestAnimationFrame
  globalThis.cancelAnimationFrame = ((id: number) => { frames = frames.filter(f => f.id !== id) }) as typeof cancelAnimationFrame
  nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => clock)
  Object.defineProperty(window, 'matchMedia', {
    writable: true, configurable: true,
    value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  })
})

afterEach(() => {
  nowSpy.mockRestore()
  globalThis.requestAnimationFrame = originalRaf
  globalThis.cancelAnimationFrame = originalCancelRaf
  detachedNodes.splice(0).forEach(node => node.remove())
})

function wire(h: ReturnType<typeof renderPin>, g: ReturnType<typeof mountGeometry>, items = ITEMS) {
  act(() => {
    h.scrollerRef.current = g.scroller
    h.result.current.pinFoldRef.current = g.fold
    h.result.current.pinCardRef.current = g.card
    h.result.current.displayItemsRef.current = items
    h.result.current.onPinCollapsedHeight(60)
    h.result.current.updatePinnedPrompt()
  })
}

/** A row whose viewport rect tracks the scroller, the way a real one does. A
 *  fixture row with a STATIC rect reads as a destination that moves with every
 *  write — the animated-widget shape — which is the case the converge backstop
 *  exists for, not the normal landing. */
function trackScroll(el: HTMLElement, contentTop: number, height: number, g: { scrollTop: number }) {
  Object.defineProperty(el, 'getBoundingClientRect', {
    configurable: true,
    value: () => rect(contentTop - g.scrollTop, height),
  })
}

/** The host's steer when the target already sits in the DOM (or nowhere at
 *  all): nothing to mount, no estimate — the live row rect alone decides. */
const mountedSteer = { mountIndex: () => false, estimateRowTop: () => null }

describe('usePinnedPrompt (shared pinned-prompt geometry)', () => {
  it('pins the prompt above the fold, with its image sources, from live geometry', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    wire(h, g)
    expect(h.result.current.pinned).toMatchObject({
      idx: 2,
      ts: ITEMS[2].msg.ts,
      text: 'second prompt',
      images: ['/tmp/shot.png'],
      push: 0,
      // The SETTLED resting height reported by the card (60 in `wire`), not the
      // live card rect (70). The push geometry is resting-height-derived so the
      // hover peek cannot feed its own push — see the peek test below.
      bannerH: 60,
    })
  })

  it('coalesces scroll recomputes to one animation frame', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    wire(h, g)
    act(() => {
      h.result.current.onScrollPin()
      h.result.current.onScrollPin()
      h.result.current.onScrollPin()
    })
    expect(frames).toHaveLength(1)
    flushFrame(16)
    expect(h.result.current.pinned?.idx).toBe(2)
  })

  it('drops the banner while disabled and re-derives it when re-enabled', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    wire(h, g)
    expect(h.result.current.pinned).not.toBeNull()
    act(() => {
      h.result.current.pinEnabledRef.current = false
      h.result.current.updatePinnedPrompt()
    })
    expect(h.result.current.pinned).toBeNull()
    act(() => {
      h.result.current.pinEnabledRef.current = true
      h.result.current.updatePinnedPrompt()
    })
    expect(h.result.current.pinned?.idx).toBe(2)
  })

  it('drops the banner when the mounted window starts below the hand-off line', () => {
    const h = renderPin(true)
    const g = mountGeometry(14)
    const items = Array.from({ length: 68 }, (_, index) => single(
      index,
      index % 2 === 0 ? 'user' : 'assistant',
      `Turn ${Math.floor(index / 2) + 1} ${index % 2 === 0 ? 'prompt' : 'response'}`,
    ))
    // A fast upward jump has moved the viewport to the start, but React has not
    // replaced the old tail window yet. Every mounted row is below the fold.
    g.rows.forEach((row, offset) => {
      row.dataset.displayIndex = String(54 + offset)
      setRect(row, 500 + offset * 80, 40)
    })

    wire(h, g, items)

    expect(h.result.current.pinned).toBeNull()
  })

  it('reports nothing pinned when no prompt sits above the hand-off line', () => {
    const h = renderPin()
    const g = mountGeometry(2)
    // Both rows fully below the hand-off line: the first row is the hand-off
    // row and there is no earlier prompt to pin.
    setRect(g.rows[0], 300, 40)
    setRect(g.rows[1], 350, 40)
    wire(h, g, [single(0, 'user', 'only prompt'), single(1, 'assistant', 'reply')])
    expect(h.result.current.pinned).toBeNull()
  })

  it('glides the in-place jump to the target row minus the banner chrome, then converges', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    trackScroll(g.rows[2], 300, 40, g)
    wire(h, g)
    // Target row 2 sits at content y=300 while scrollTop=200 (viewport y=100),
    // so the raw landing is 300 minus the chrome: fold (100) +
    // pinPushTravel(bannerH 70 → 74) + 24px slack = 198 → 102.
    act(() => { h.result.current.jumpToPinnedPromptInPlace(2, mountedSteer) })
    expect(frames).toHaveLength(1)
    flushFrame(0)     // t=0: no movement yet
    expect(g.scrollTop).toBe(200)
    flushFrame(225)   // mid-travel: strictly between, i.e. a glide, not a teleport
    expect(g.scrollTop).toBeLessThan(200)
    expect(g.scrollTop).toBeGreaterThan(102)
    flushFrame(600)   // past the travel: on the goal, and STILL armed
    expect(g.scrollTop).toBe(102)
    expect(frames).toHaveLength(1)
    // Convergence: the goal must hold still for GLIDE_QUIET_MS across at least
    // two frames before the loop lets go.
    flushFrame(616)
    flushFrame(632)
    expect(frames).toHaveLength(1)
    flushFrame(900)
    expect(g.scrollTop).toBe(102)
    expect(frames).toHaveLength(0)
  })

  it('aborts the in-place glide on user scroll intent', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    trackScroll(g.rows[2], 300, 40, g)
    wire(h, g)
    act(() => { h.result.current.jumpToPinnedPromptInPlace(2, mountedSteer) })
    flushFrame(0)
    expect(frames).toHaveLength(1)
    // A wheel event is user scroll intent (attachUserScrollIntent): the glide
    // must stop writing scrollTop, drop its queued frame and leave the reader
    // where they are.
    act(() => { g.scroller.dispatchEvent(new Event('wheel')) })
    expect(frames).toHaveLength(0)
    expect(g.scrollTop).toBe(200)
  })

  it('steers an absent target by estimate, glides, and lands below the banner chrome', () => {
    const h = renderPin()
    const g = mountGeometry(2)
    wire(h, g)
    const mountIndex = vi.fn(() => true)
    const estimateRowTop = vi.fn(() => 600)

    act(() => {
      h.result.current.jumpToPinnedPromptInPlace(2, { mountIndex, estimateRowTop })
    })

    expect(mountIndex).toHaveBeenCalledWith(2, { unionOnly: true })
    expect(estimateRowTop).toHaveBeenCalledWith(2)
    expect(frames).toHaveLength(1)
    flushFrame(0)
    expect(g.scrollTop).toBe(200)
    flushFrame(225)
    expect(g.scrollTop).toBeGreaterThan(200)
    expect(g.scrollTop).toBeLessThan(402)
    flushFrame(600)
    expect(g.scrollTop).toBe(402)
  })

  it('queues no frame when the target is neither mounted nor estimable', () => {
    const h = renderPin()
    const g = mountGeometry(2)
    wire(h, g, [single(0, 'user', 'a'), single(1, 'assistant', 'b')])
    // No row and no estimate: there is no goal to glide toward, so nothing is
    // armed (a goal lost mid-travel is runConvergingGlide's `lost` end instead).
    act(() => { h.result.current.jumpToPinnedPromptInPlace(7, mountedSteer) })
    expect(frames).toHaveLength(0)
  })
})

/**
 * The pinned card grows past its resting size in two states — the hover peek and
 * the expansion — and `PinnedPrompt`'s `peek` is gated on `pushUp <= 0`. So if
 * the push geometry reads the LIVE card rect, the card's own growth decides
 * whether it is allowed to grow: peek grows the card, the taller card lengthens
 * `pinPushTravel`, the longer travel makes `push` positive, a positive push
 * closes the peek, the card shrinks, `push` returns to 0, and the peek reopens
 * under the still-resting pointer. Users saw the card flip between its one-line
 * and three-line heights for as long as the pointer stayed on it, and blink the
 * transcript row it hides. With macOS "Reduce motion" ON the height morph is
 * skipped, so each traversal costs one frame instead of 150ms and the throb
 * became a violent flicker.
 */
describe('usePinnedPrompt push geometry is resting-height-derived', () => {
  it('ignores a card grown by the peek, so the peek cannot close itself', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    // The incoming prompt sits 80px below the fold: clear of the RESTING travel
    // (ROW_PAD_Y + 60 = 64) and inside the PEEKED one (ROW_PAD_Y + 105 = 109).
    // That band is where the oscillation lived.
    setRect(g.rows[4], 180, 40)
    // Card currently showing three lines because the pointer is resting on it.
    setRect(g.card, 104, 105)
    wire(h, g)
    expect(h.result.current.pinned).toMatchObject({ idx: 2, push: 0, bannerH: 60 })
  })

  it('still pushes the banner out on the incoming prompt, using the resting height', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    // 40px below the fold: inside the resting travel of 64, so the card is
    // genuinely being pushed out and the push must be the remaining 24px.
    setRect(g.rows[4], 140, 40)
    wire(h, g)
    expect(h.result.current.pinned).toMatchObject({ idx: 2, push: 24, bannerH: 60 })
  })
})

/**
 * The fold stands in for the BUBBLE, not the row. UserMessage draws an action
 * strip (copy / copy link / pin / timestamp) under its bubble, and the hidden row
 * re-shows that strip in place (index.css `[data-pinned-standin]`), so the card
 * has to fold down to the bubble's bottom edge: a card reaching the ROW's bottom
 * would sit exactly over the controls the hand-off leaves visible.
 */
/** UserMessage's tree under a row: a `div[data-role="user"]` root whose direct
 *  children are the bubble and the `data-message-actions` strip. The hook scopes
 *  its probes to that root, so the fixture has to have it. */
function mountUserRow(row: HTMLElement) {
  const root = document.createElement('div')
  root.setAttribute('data-role', 'user')
  const bubble = document.createElement('div')
  bubble.className = 'message-bubble user-bubble'
  const strip = document.createElement('div')
  strip.setAttribute('data-message-actions', '')
  root.append(bubble, strip)
  row.append(root)
  return { root, bubble, strip }
}

describe('usePinnedPrompt folds the card to the bubble, leaving the action strip clear', () => {
  it('reports a live height whose bottom is the bubble bottom, with the strip below it uncovered', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    // The pinned row (index 2) is a tall prompt whose top has crossed the fold
    // (fold at 100): row 60..460, bubble 64..430, then a 4px gap and the 26px
    // action strip UserMessage renders under the bubble (434..460).
    setRect(g.rows[2], 60, 400)
    const { bubble, strip } = mountUserRow(g.rows[2])
    setRect(bubble, 64, 366)
    setRect(strip, 434, 26)
    // Push the incoming prompt far down so the card is not being pushed out.
    setRect(g.rows[3], 460, 40)
    setRect(g.rows[4], 900, 40)
    wire(h, g)
    // Card top is fold + ROW_PAD_Y = 104; bubble bottom is 430 → 326px tall.
    // The row's bottom (460) would have given 356px and buried the strip. The
    // strip, wholly below the folding card's bottom, is uncovered.
    expect(h.result.current.pinned).toMatchObject({ idx: 2, liveH: 326, stripUncovered: true })
  })

  it('keeps the strip uncovered once the card rests, while any of it is still below the card', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    // The same row scrolled 290px further: bubble bottom at fold + 40 (140),
    // strip 144..170. The card has reached its 60px clamp — its resting bottom
    // is fold + 4 + 60 = 164 — so no live height is reported, yet 6px of the
    // strip still show below the card. Keyed on the fold, this is where copy /
    // copy-link / pin vanished under the pointer and the band went blank.
    setRect(g.rows[2], -230, 400)
    const { bubble, strip } = mountUserRow(g.rows[2])
    setRect(bubble, -226, 366)
    setRect(strip, 144, 26)
    setRect(g.rows[3], 170, 40)
    setRect(g.rows[4], 900, 40)
    // The card itself at its resting rect: the strip test reads the card's LIVE
    // bottom (the fixture's default card is 10px taller than the resting height
    // it reports, for the push tests above).
    setRect(g.card, 104, 60)
    wire(h, g)
    expect(h.result.current.pinned?.liveH, 'the fold is over').toBeUndefined()
    expect(h.result.current.pinned).toMatchObject({ idx: 2, stripUncovered: true })
  })

  it('drops the mark on a later frame of the same pin, once the strip has slid under the card', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    setRect(g.rows[2], -230, 400)
    const { bubble, strip } = mountUserRow(g.rows[2])
    setRect(bubble, -226, 366)
    setRect(strip, 144, 26)
    setRect(g.rows[3], 170, 40)
    setRect(g.rows[4], 900, 40)
    setRect(g.card, 104, 60)
    wire(h, g)
    expect(h.result.current.pinned).toMatchObject({ idx: 2, stripUncovered: true })
    // 20px on: the strip's bottom (150) is above the card's resting bottom
    // (164), so the whole strip is under the card. The same message is still
    // pinned, so this is the same-message path of nextPinnedPromptState — the
    // flag has to be carried there or the marker stays stuck at `folding`.
    setRect(g.rows[2], -250, 400)
    setRect(bubble, -246, 366)
    setRect(strip, 124, 26)
    setRect(g.rows[3], 150, 40)
    act(() => { h.result.current.updatePinnedPrompt() })
    expect(h.result.current.pinned?.liveH, 'still at rest').toBeUndefined()
    expect(h.result.current.pinned).toMatchObject({ idx: 2, stripUncovered: false })
  })

  it('measures only the row\'s own strip and editing marker, never nodes inside the bubble', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    // The strip test geometry above (real strip 144..170, 6px still uncovered),
    // plus a message body that carries the hooks itself. The sanitizer strips
    // them (MarkdownRenderer.reservedDataHooks.test.tsx), and the hook must not
    // rely on that alone: the probes read the root's own attribute and its
    // direct children, so a forged strip earlier in document order is not the
    // one measured and a forged editing marker does not drop the pin.
    setRect(g.rows[2], -230, 400)
    const { bubble, strip } = mountUserRow(g.rows[2])
    setRect(bubble, -226, 366)
    setRect(strip, 144, 26)
    const forgedEditing = document.createElement('span')
    forgedEditing.setAttribute('data-message-editing', '')
    const forgedStrip = document.createElement('div')
    forgedStrip.setAttribute('data-message-actions', '')
    bubble.append(forgedEditing, forgedStrip)
    // Wholly above the card's resting bottom (164): measured, it would read as
    // covered and hide the real strip's last pixels.
    setRect(forgedStrip, 0, 20)
    setRect(g.rows[3], 170, 40)
    setRect(g.rows[4], 900, 40)
    setRect(g.card, 104, 60)
    wire(h, g)
    expect(h.result.current.pinned).toMatchObject({ idx: 2, stripUncovered: true })
  })

  it('measures a steer bubble too, through the shared message-bubble hook', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    setRect(g.rows[2], 60, 400)
    // A steer's bubble carries `message-bubble` but not `user-bubble`.
    const bubble = document.createElement('div')
    bubble.className = 'message-bubble'
    g.rows[2].append(bubble)
    setRect(bubble, 88, 300)
    setRect(g.rows[3], 460, 40)
    setRect(g.rows[4], 900, 40)
    wire(h, g)
    // 388 − 104 = 284, under the stand-in's 324px ceiling (row top 60 to bubble
    // bottom 388, less the row padding) — the fold is still in progress.
    expect(h.result.current.pinned).toMatchObject({ idx: 2, liveH: 284 })
  })

  it('caps the fold at the stand-in\'s natural height, so a steer\'s card reaches its bubble bottom', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    // The hand-off frame: row 2's top is ON the fold (100). A steer's accent
    // bubble starts under its badge, 28px below the row top instead of the 4px
    // of row padding a plain bubble has, so the box the card stands in for runs
    // from the card's top (104) to the bubble's bottom (428): 324px.
    setRect(g.rows[2], 100, 400)
    const bubble = document.createElement('div')
    bubble.className = 'message-bubble'
    g.rows[2].append(bubble)
    setRect(bubble, 128, 300)
    setRect(g.rows[3], 500, 40)
    setRect(g.rows[4], 900, 40)
    wire(h, g)
    // A ceiling of the bubble's own 300px would stop the card 24px short of the
    // bubble's bottom — a blank band above the strip re-shown under it.
    expect(h.result.current.pinned).toMatchObject({ idx: 2, liveH: 324 })
  })

  it('never takes a row that is being edited as the stand-in', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    // UserMessage's editing render is a `data-role="user"` root carrying
    // `data-message-editing`, with a textarea where the bubble was — no
    // `.message-bubble` at all. The stand-in state would hide the whole row,
    // editor and Send included, so the hook leaves the row visible and shows no
    // card at all.
    const editor = document.createElement('div')
    editor.setAttribute('data-role', 'user')
    editor.setAttribute('data-message-editing', '')
    g.rows[2].append(editor)
    wire(h, g)
    expect(h.result.current.pinned).toBeNull()
  })

  it('reads the strip as covered from the card\'s own bottom once the card has grown over it', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    // At the clamp with 6px of strip still below the card's RESTING bottom (164)
    // — the window where a pointer resting on the card opens the peek, or the
    // chevron expands it, and the card grows down over those pixels. The row
    // cannot show that growth; the card's own rect can. Peeked to three lines
    // the card runs 104..209, past the strip's bottom (170).
    setRect(g.rows[2], -230, 400)
    const { bubble, strip } = mountUserRow(g.rows[2])
    setRect(bubble, -226, 366)
    setRect(strip, 144, 26)
    setRect(g.rows[3], 170, 40)
    setRect(g.rows[4], 900, 40)
    setRect(g.card, 104, 105)
    wire(h, g)
    expect(h.result.current.pinned?.liveH, 'at rest').toBeUndefined()
    // The live read serves the strip flag alone: the push geometry still sees
    // the resting height (60), not the grown card.
    expect(h.result.current.pinned).toMatchObject({ idx: 2, push: 0, bannerH: 60, stripUncovered: false })
    // The peek closes: the same card back at its resting rect, and the 6px show.
    setRect(g.card, 104, 60)
    act(() => { h.result.current.updatePinnedPrompt() })
    expect(h.result.current.pinned).toMatchObject({ idx: 2, stripUncovered: true })
  })

  it('re-derives the flag when the card itself resizes, without waiting for a scroll', () => {
    // The peek and the expansion grow the card with no scroll to run the
    // recompute, so the hook observes the pinned card's size and re-runs it.
    const observed: Element[] = []
    let fire: (() => void) | null = null
    class RecordingResizeObserver {
      constructor(cb: ResizeObserverCallback) { fire = () => cb([], this as unknown as ResizeObserver) }
      observe(el: Element) { observed.push(el) }
      unobserve() {}
      disconnect() { fire = null }
    }
    const scope = globalThis as unknown as { ResizeObserver: unknown }
    const original = scope.ResizeObserver
    scope.ResizeObserver = RecordingResizeObserver
    try {
      const h = renderPin()
      const g = mountGeometry(5)
      setRect(g.rows[2], -230, 400)
      const { bubble, strip } = mountUserRow(g.rows[2])
      setRect(bubble, -226, 366)
      setRect(strip, 144, 26)
      setRect(g.rows[3], 170, 40)
      setRect(g.rows[4], 900, 40)
      setRect(g.card, 104, 60)
      wire(h, g)
      expect(h.result.current.pinned).toMatchObject({ idx: 2, stripUncovered: true })
      expect(observed, 'the pinned card is what the hook observes').toEqual([g.card])
      // The pointer rests on the card: the peek grows it to three lines, over
      // the strip, and the observer's report is the only recompute there is.
      setRect(g.card, 104, 105)
      act(() => { fire?.() })
      expect(h.result.current.pinned).toMatchObject({ idx: 2, stripUncovered: false })
    } finally {
      scope.ResizeObserver = original
    }
  })
})

/**
 * Reduced motion must remove the eased TRAVEL of the jump, not its convergence.
 * The landing is re-derived every frame because rows mount, images load and the
 * banner swaps DURING the jump — and the swap is caused by our own scroll write,
 * so it is only observable on the frame after it. Landing after a single frame
 * reads the geometry that was true BEFORE those shifts, which is the stale
 * landing (banner clipped or dropped) this self-driven glide replaced.
 */
describe('usePinnedPrompt in-place jump under reduced motion', () => {
  function reduceMotion() {
    Object.defineProperty(window, 'matchMedia', {
      writable: true, configurable: true,
      value: vi.fn().mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    })
  }

  it('lands instantly, then keeps re-deriving until the landing stops moving', () => {
    reduceMotion()
    const h = renderPin()
    const g = mountGeometry(5)
    trackScroll(g.rows[2], 300, 40, g)
    wire(h, g)
    act(() => { h.result.current.jumpToPinnedPromptInPlace(2, mountedSteer) })

    // Frame 1: no eased ramp — straight to the goal. chrome = fold 100 +
    // pinPushTravel(70) 74 + 24 slack = 198, so 200 + 100 - 198 = 102.
    flushFrame(0)
    expect(g.scrollTop).toBe(102)
    // ...and the loop is STILL armed. This is the fix: a single frame cannot see
    // the banner swap its own write causes.
    expect(frames).toHaveLength(1)

    // The banner that pins mid-jump is taller than the one we reserved for.
    setRect(g.card, 104, 120)
    // chrome = 100 + (4 + 120) + 24 = 248, row top = 300 - 102 = 198.
    flushFrame(16)
    expect(g.scrollTop).toBe(52)

    // Nothing moves any more; the loop lets go once the goal has been quiet for
    // GLIDE_QUIET_MS (timed from the last change at 16ms) over ≥ 2 frames.
    flushFrame(32)
    flushFrame(48)
    expect(g.scrollTop).toBe(52)
    expect(frames).toHaveLength(1)
    flushFrame(300)
    expect(g.scrollTop).toBe(52)
    expect(frames).toHaveLength(0)
  })

  it('stops at the converge backstop when the landing never settles', () => {
    reduceMotion()
    const h = renderPin()
    const g = mountGeometry(5)
    // Row rect does NOT track the scroller, so the derived goal keeps moving —
    // the animated-widget case. The wall-clock bound must still end the loop.
    wire(h, g)
    act(() => { h.result.current.jumpToPinnedPromptInPlace(2, mountedSteer) })
    flushFrame(0)
    expect(frames).toHaveLength(1)
    flushFrame(1990)
    expect(frames).toHaveLength(1)
    flushFrame(2000)
    expect(frames).toHaveLength(0)
  })
})
