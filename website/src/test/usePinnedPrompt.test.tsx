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
 *   4. the in-place jump (unvirtualized hosts) glides the scroller to the
 *      target row minus the banner chrome, and a user scroll aborts it.
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

function renderPin() {
  const scrollerRef = { current: null as HTMLDivElement | null }
  const hook = renderHook(() => usePinnedPrompt({ scrollerRef }))
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
      bannerH: 70,
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

  it('glides the in-place jump to the target row minus the banner chrome', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    wire(h, g)
    // Target row 2 sits at viewport y=100 while the scroller starts at y=0 and
    // scrollTop=200, so the raw landing is 200 + 100 = 300 minus the chrome:
    // fold (100) + pinPushTravel(bannerH 70 → 74) + 24px slack = 198 → 102.
    act(() => { h.result.current.jumpToPinnedPromptInPlace(2) })
    expect(frames).toHaveLength(1)
    flushFrame(0)     // t=0: no movement yet
    expect(g.scrollTop).toBe(200)
    flushFrame(600)   // past GLIDE_MS: settled at the goal
    expect(g.scrollTop).toBe(102)
    expect(frames).toHaveLength(0)
  })

  it('aborts the in-place glide on user scroll intent', () => {
    const h = renderPin()
    const g = mountGeometry(5)
    wire(h, g)
    act(() => { h.result.current.jumpToPinnedPromptInPlace(2) })
    flushFrame(0)
    // A wheel event is user scroll intent (attachUserScrollIntent): the glide
    // must stop writing scrollTop and leave the reader where they are.
    act(() => { g.scroller.dispatchEvent(new Event('wheel')) })
    flushFrame(16)
    expect(g.scrollTop).toBe(200)
    expect(frames).toHaveLength(0)
  })

  it('does not throw when the target row is not mounted', () => {
    const h = renderPin()
    const g = mountGeometry(2)
    wire(h, g, [single(0, 'user', 'a'), single(1, 'assistant', 'b')])
    expect(() => act(() => { h.result.current.jumpToPinnedPromptInPlace(7) })).not.toThrow()
    expect(frames).toHaveLength(0)
  })
})
