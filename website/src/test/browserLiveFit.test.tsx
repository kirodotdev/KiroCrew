import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import BrowserLiveCardCanvas from '../pages/chat/BrowserLiveCardCanvas'
import { browserLiveStore } from '../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'
import { drawnRect, mapPointToFrame } from '../hooks/useBrowserLive.input'
import { DEFAULT_FRAME, FALLBACK_MAX_HEIGHT, MIN_FRAME_PX, frameBoxStyle, frameMaxHeight } from '../pages/chat/browserLiveFit'

/**
 * The live card's frame fits the visible transcript: the box takes the
 * frame's ratio, its height is capped by the scroller minus the card chrome,
 * and takeover input still maps from the drawn frame rect when the box and
 * the frame disagree (letterbox).
 */

describe('frameMaxHeight', () => {
  it('is the scroller height minus chrome and margin', () => {
    expect(frameMaxHeight(800, 100, 16)).toBe(684)
  })
  it('never goes below the minimum frame height', () => {
    expect(frameMaxHeight(200, 150, 16)).toBe(MIN_FRAME_PX)
  })
  it('is null when nothing can be measured', () => {
    expect(frameMaxHeight(0, 100)).toBeNull()
    expect(frameMaxHeight(Number.NaN, 100)).toBeNull()
  })
  it('ignores a negative chrome measurement', () => {
    expect(frameMaxHeight(500, -40, 0)).toBe(500)
  })
})

describe('frameBoxStyle', () => {
  it('takes the frame ratio and narrows to keep height under the cap', () => {
    const s = frameBoxStyle({ w: 1280, h: 800 }, 400)
    expect(s.aspectRatio).toBe('1280 / 800')
    expect(s.maxHeight).toBe('400px')
    expect(s.width).toBe('100%')
    expect(s.maxWidth).toBe('calc(400px * 1.600000)')
    expect(s.marginInline).toBe('auto')
  })
  it('uses 16:10 before the first frame and a dvh cap when unmeasured', () => {
    const s = frameBoxStyle(null, null)
    expect(s.aspectRatio).toBe(`${DEFAULT_FRAME.w} / ${DEFAULT_FRAME.h}`)
    expect(s.maxHeight).toBe(FALLBACK_MAX_HEIGHT)
    expect(String(s.maxWidth)).toContain('100dvh')
  })
  it('falls back to 16:10 for a degenerate frame size', () => {
    expect(frameBoxStyle({ w: 0, h: 0 }, 300).aspectRatio).toBe('16 / 10')
  })
  it('a tall phone-shaped frame gets a narrow box, never a tall one', () => {
    // 390x844 frame in a 358px column capped at 500px: width = 500 * 0.462 = 231px.
    const s = frameBoxStyle({ w: 390, h: 844 }, 500)
    expect(s.maxWidth).toBe(`calc(500px * ${(390 / 844).toFixed(6)})`)
  })
})

describe('input mapping under letterbox', () => {
  const frame = { w: 1280, h: 800 }
  it('a box wider than the frame letterboxes left and right', () => {
    const r = drawnRect({ left: 0, top: 0, width: 1000, height: 400 }, frame)
    expect(r).toEqual({ left: 180, top: 0, width: 640, height: 400 })
  })
  it('maps the drawn rect, not the element box', () => {
    const box = { left: 0, top: 0, width: 1000, height: 400 }
    expect(mapPointToFrame(180, 0, box, frame)).toEqual({ x: 0, y: 0 })
    expect(mapPointToFrame(500, 200, box, frame)).toEqual({ x: 640, y: 400 })
    expect(mapPointToFrame(820, 400, box, frame)).toEqual({ x: 1279, y: 799 })
  })
  it('refuses a press in the side bars and clamps a release to the edge', () => {
    const box = { left: 0, top: 0, width: 1000, height: 400 }
    expect(mapPointToFrame(50, 200, box, frame)).toBeNull()
    expect(mapPointToFrame(50, 200, box, frame, true)).toEqual({ x: 0, y: 400 })
    expect(mapPointToFrame(990, 200, box, frame, true)).toEqual({ x: 1279, y: 400 })
  })
  it('a tall box letterboxes top and bottom', () => {
    const box = { left: 10, top: 20, width: 320, height: 400 }
    expect(drawnRect(box, frame)).toEqual({ left: 10, top: 120, width: 320, height: 200 })
    expect(mapPointToFrame(10, 100, box, frame)).toBeNull()
    expect(mapPointToFrame(170, 220, box, frame)).toEqual({ x: 640, y: 400 })
  })
})

describe('canvas reports the frame size', () => {
  const S = 'slot:fit'
  const frameOf = (seq: number, w: number, h: number): DecodedBrowserFrame => ({
    header: { type: 'browser_frame', session_key: S, stream: '', seq, w, h, mask: [] },
    jpeg: new Uint8Array([seq]),
    receivedAt: 1000 + seq,
  })
  let input: ReturnType<typeof vi.fn>
  let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

  beforeEach(() => {
    browserLiveStore.__resetForTests()
    browserLiveStore.applyEvent({ session_key: S, turn_id: 't1', state: 'in_control', url: 'https://x.test/' })
    input = vi.fn(async () => ({ ok: true }))
    restore = browserLiveStore.__setWritesForTests({ input, ack: vi.fn(async () => ({ ok: true })) })
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), fillRect: vi.fn(), fillStyle: '' } as unknown as CanvasRenderingContext2D)
    vi.stubGlobal('createImageBitmap', vi.fn(() => new Promise(() => {})))
  })
  afterEach(() => {
    browserLiveStore.__setWritesForTests(restore)
    browserLiveStore.__resetForTests()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('reports the first frame’s size once', () => {
    const onFrameSize = vi.fn()
    render(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive aria-label="frame" onFrameSize={onFrameSize} />)
    act(() => browserLiveStore.applyFrame(frameOf(1, 1280, 800)))
    expect(onFrameSize).toHaveBeenCalledTimes(1)
    expect(onFrameSize).toHaveBeenLastCalledWith({ w: 1280, h: 800 })
  })

  it('a click in a letterboxed box lands on the drawn frame, a side-bar press is dropped', async () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 1000, height: 400, right: 1000, bottom: 400, x: 0, y: 0, toJSON: () => ({}) })
    render(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive aria-label="frame" />)
    act(() => browserLiveStore.applyFrame(frameOf(1, 1280, 800)))
    const c = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(c, { clientX: 50, clientY: 200, button: 0, pointerId: 1 })
    expect(input).not.toHaveBeenCalled()
    fireEvent.pointerDown(c, { clientX: 500, clientY: 200, button: 0, pointerId: 1 })
    // A press goes out on the ordered send chain, a microtask later.
    await waitFor(() => expect(input).toHaveBeenCalled())
    expect(input.mock.calls[0][1]).toMatchObject({ action: 'down', x: 640, y: 400 })
  })
})
