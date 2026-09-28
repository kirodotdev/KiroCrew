import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '../types'
import type { DisplayItem } from '../pages/chat/types'
import {
  useChatPageTranscriptEarlyController,
} from '../pages/chat/useChatPageTranscriptController'

/**
 * The early transcript controller owns DOM-driven pin geometry and the two
 * pinned-prompt jump modes, none of which a full ChatPage mount under happy-dom
 * (no layout) can reach.  Keep this test at that seam: a full ChatPage mount
 * hides these callbacks behind the virtualizer, while the real hook below keeps
 * the rAF/geometry behaviour under test.
 */

interface QueuedFrame {
  id: number
  cb: FrameRequestCallback
}

let frames: QueuedFrame[] = []
let nextFrameId = 1
let clock = 0
let originalRaf: typeof requestAnimationFrame
let originalCancelRaf: typeof cancelAnimationFrame
let nowSpy: ReturnType<typeof vi.spyOn>
const detachedNodes: HTMLElement[] = []

const rect = (top: number, height: number): DOMRect => ({
  top,
  bottom: top + height,
  left: 0,
  right: 0,
  width: 0,
  height,
  x: 0,
  y: top,
  toJSON: () => ({}),
}) as DOMRect

function setRect(el: HTMLElement, top: number, height: number) {
  Object.defineProperty(el, 'getBoundingClientRect', {
    configurable: true,
    value: () => rect(top, height),
  })
}

/** A row whose viewport rect tracks the scroller, the way a real one does. */
function trackScroll(el: HTMLElement, contentTop: number, height: number, g: { scrollTop: number }) {
  Object.defineProperty(el, 'getBoundingClientRect', {
    configurable: true,
    value: () => rect(contentTop - g.scrollTop, height),
  })
}

function flushFrame(at: number) {
  const frame = frames.shift()
  expect(frame, 'expected a queued animation frame').toBeDefined()
  clock = at
  act(() => { frame!.cb(at) })
}

const message = (role: string, content: string, ts: string): ChatMessage => ({
  role,
  content,
  cls: '',
  ts,
})

const single = (idx: number, role: string, content: string): DisplayItem => ({
  kind: 'single',
  idx,
  msg: message(role, content, `2026-08-30T00:00:0${idx}.000Z`),
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
    // index 3 is the first row below the handoff line; index 4 is the next
    // prompt, mounted far enough down that it does not push the pinned card.
    const top = [0, 50, 100, 160, 300][index] ?? (300 + index * 100)
    setRect(row, top, 40)
  })

  let scrollTop = 200
  Object.defineProperties(scroller, {
    clientHeight: { configurable: true, get: () => 200 },
    scrollHeight: { configurable: true, get: () => 2000 },
    scrollTop: {
      configurable: true,
      get: () => scrollTop,
      set: (next: number) => { scrollTop = next },
    },
  })

  return {
    scroller,
    fold,
    card,
    rows,
    get scrollTop() { return scrollTop },
  }
}

function renderEarly(
  mountIndex: (index: number) => boolean = () => false,
  estimateRowTop: (index: number) => number | null = () => null,
) {
  const vGetFollowRef = { current: () => false }
  const slotRunningRef = { current: false }
  const mountIndexRef = { current: vi.fn(mountIndex) }
  const estimateRowTopRef = { current: vi.fn(estimateRowTop) }
  const scrollerRef = { current: null as HTMLDivElement | null }
  const vScrollToBottomRef = { current: vi.fn() }
  const scrollToDisplayIndex = vi.fn()
  const hook = renderHook(() => useChatPageTranscriptEarlyController({
    activeTip: null,
    mountIndexRef,
    estimateRowTopRef,
    scrollerRef: scrollerRef as never,
    scrollToDisplayIndex: scrollToDisplayIndex as never,
    slotRunningRef,
    vGetFollowRef,
    vScrollToBottomRef,
  }))
  return { ...hook, vGetFollowRef, mountIndexRef, estimateRowTopRef, scrollerRef, vScrollToBottomRef, scrollToDisplayIndex }
}

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
  globalThis.cancelAnimationFrame = ((id: number) => {
    frames = frames.filter(frame => frame.id !== id)
  }) as typeof cancelAnimationFrame
  nowSpy = vi.spyOn(performance, 'now').mockImplementation(() => clock)
})

afterEach(() => {
  nowSpy.mockRestore()
  globalThis.requestAnimationFrame = originalRaf
  globalThis.cancelAnimationFrame = originalCancelRaf
  detachedNodes.splice(0).forEach(node => node.remove())
})

describe('useChatPageTranscriptEarlyController pinned prompt coverage', () => {
  it('skips a machine opener behind the fold, pins the user prompt above it, and throttles scroll recomputes', () => {
    // The nudge at index 2 is the row that has scrolled behind the band, but a
    // machine opener is never a pin candidate: the walk continues up to the
    // user's own prompt at index 0 (utils/pinnedPrompt isPrompt).
    const { result, scrollerRef } = renderEarly()
    const geometry = mountGeometry(5)
    const items: DisplayItem[] = [
      single(0, 'user', 'first prompt'),
      single(1, 'assistant', 'first reply'),
      single(2, 'nudge', '[auto-nudge cycle 2]\ninspect the latest logs'),
      single(3, 'assistant', 'second reply'),
      single(4, 'user', 'next prompt'),
    ]

    act(() => {
      scrollerRef.current = geometry.scroller
      result.current.pinFoldRef.current = geometry.fold
      result.current.pinCardRef.current = geometry.card
      result.current.displayItemsRef.current = items
      result.current.onPinCollapsedHeight(60)
      result.current.updatePinnedPrompt()
    })

    expect(result.current.pinned).toMatchObject({
      idx: 0,
      raw: 'first prompt',
      full: 'first prompt',
      push: 0,
      // The height reported by `onPinCollapsedHeight` (60), not the live card
      // rect (70): the push geometry is derived from the card's SETTLED resting
      // height so a card grown by the hover peek cannot feed its own push. See
      // usePinnedPrompt.test.tsx.
      bannerH: 60,
    })

    act(() => {
      result.current.onScrollPin()
      result.current.onScrollPin()
    })
    expect(frames).toHaveLength(1)
    flushFrame(16)
    expect(result.current.pinned?.idx).toBe(0)
  })

  it('glides a near pinned jump after the unioned rows measure, then converges on the landing', () => {
    const near = renderEarly(() => false)
    const nearGeometry = mountGeometry(3)
    const items: DisplayItem[] = [
      single(0, 'user', 'first prompt'),
      single(1, 'assistant', 'reply'),
      single(2, 'user', 'target prompt'),
    ]
    act(() => {
      near.scrollerRef.current = nearGeometry.scroller
      near.result.current.pinFoldRef.current = nearGeometry.fold
      near.result.current.pinCardRef.current = nearGeometry.card
      // Content top 500: viewport y=300 at scrollTop 200. Landing = 500 minus
      // the chrome (fold 100 + pinPushTravel(70) 74 + 24 slack = 198) = 302.
      trackScroll(nearGeometry.rows[2], 500, 40, nearGeometry)
      near.result.current.displayItemsRef.current = items
      near.result.current.onPinCollapsedHeight(60)
      near.result.current.scrollToPinnedPrompt(2)
    })
    // A near target unions the window; nothing is replaced.
    expect(near.mountIndexRef.current).toHaveBeenCalledWith(2, { unionOnly: true })

    // Three stable observations of the unioned row enter the glide.
    flushFrame(0)
    flushFrame(16)
    flushFrame(32)
    expect(nearGeometry.scrollTop).toBe(200)
    flushFrame(257)   // mid-travel: between, so a glide rather than a teleport
    expect(nearGeometry.scrollTop).toBeGreaterThan(200)
    expect(nearGeometry.scrollTop).toBeLessThan(302)
    flushFrame(482)   // travel over: on the goal, loop still armed to converge
    expect(nearGeometry.scrollTop).toBe(302)
    expect(frames).toHaveLength(1)
    flushFrame(498)
    flushFrame(514)
    flushFrame(750)   // quiet for GLIDE_QUIET_MS over ≥ 2 frames: settled
    expect(nearGeometry.scrollTop).toBe(302)
    expect(frames).toHaveLength(0)
    expect(near.scrollToDisplayIndex).not.toHaveBeenCalled()
  })

  it('glides a far pinned jump toward the height-index estimate instead of teleporting', () => {
    // The target row is NOT mounted (the fixture holds rows 0-1 only) and the
    // virtualizer reports it far. The jump must not replace the window and must
    // not teleport: it steers by the estimate the virtualizer supplies, moving
    // through intermediate positions, and converges once the goal holds still.
    const far = renderEarly(() => true, (index) => (index === 2 ? 1500 : null))
    const farGeometry = mountGeometry(2)
    const items: DisplayItem[] = [
      single(0, 'user', 'first prompt'),
      single(1, 'assistant', 'reply'),
      single(2, 'user', 'target prompt'),
    ]
    act(() => {
      far.scrollerRef.current = farGeometry.scroller
      far.result.current.pinFoldRef.current = farGeometry.fold
      far.result.current.pinCardRef.current = farGeometry.card
      far.result.current.displayItemsRef.current = items
      far.result.current.onPinCollapsedHeight(60)
      far.result.current.scrollToPinnedPrompt(2)
    })
    expect(far.mountIndexRef.current).toHaveBeenCalledWith(2, { unionOnly: true })
    expect(far.estimateRowTopRef.current).toHaveBeenCalledWith(2)

    // Estimate 1500 minus the chrome (198) = 1302, inside the 1800 max.
    flushFrame(0)
    expect(farGeometry.scrollTop).toBe(200)
    flushFrame(225)
    expect(farGeometry.scrollTop).toBeGreaterThan(200)
    expect(farGeometry.scrollTop).toBeLessThan(1302)
    flushFrame(450)
    expect(farGeometry.scrollTop).toBe(1302)
    expect(frames).toHaveLength(1)
    flushFrame(466)
    flushFrame(482)
    flushFrame(720)
    expect(farGeometry.scrollTop).toBe(1302)
    expect(frames).toHaveLength(0)
    // The old far path: an instant scroll through the mounted-row poll.
    expect(far.scrollToDisplayIndex).not.toHaveBeenCalled()
  })
})
