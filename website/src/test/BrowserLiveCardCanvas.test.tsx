import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { createRef } from 'react'

import BrowserLiveCardCanvas, { type BrowserLiveCanvasHandle } from '../pages/chat/BrowserLiveCardCanvas'
import { browserLiveStore } from '../hooks/useBrowserLive'
import type { DecodedBrowserFrame } from '../hooks/useBrowserLive.frame'

/**
 * The painter: frames reach the canvas only while visible, the newest frame
 * wins when decodes queue up, password boxes are painted over after the
 * image, replay walks the ring on the clock, and takeover input is mapped to
 * frame pixels and posted (moves coalesced, Esc kept for hand-back).
 */

const S = 'slot:abc'
const frame = (seq: number, over: Partial<DecodedBrowserFrame['header']> = {}): DecodedBrowserFrame => ({
  header: { type: 'browser_frame', session_key: S, seq, w: 640, h: 400, mask: [], ...over },
  jpeg: new Uint8Array([seq]),
  receivedAt: 1000 + seq * 100,
})

let ctx: { drawImage: ReturnType<typeof vi.fn>; fillRect: ReturnType<typeof vi.fn>; fillStyle: string }
let decodes: Array<{ resolve: (v: unknown) => void }>
let writes: { input: ReturnType<typeof vi.fn> }
let restore: ReturnType<typeof browserLiveStore.__setWritesForTests>

function flushDecodes() { for (const d of decodes.splice(0)) d.resolve({ close: vi.fn() }) }

beforeEach(() => {
  browserLiveStore.__resetForTests()
  browserLiveStore.applyEvent({ session_key: S, turn_id: 't1', state: 'live', url: 'https://x.test/' })
  writes = { input: vi.fn(async () => ({ ok: true, sent: 1 })) }
  restore = browserLiveStore.__setWritesForTests({ ...writes, ack: vi.fn(async () => ({ ok: true })) })
  ctx = { drawImage: vi.fn(), fillRect: vi.fn(), fillStyle: '' }
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx as unknown as CanvasRenderingContext2D)
  decodes = []
  vi.stubGlobal('createImageBitmap', vi.fn(() => new Promise(resolve => { decodes.push({ resolve }) })))
  vi.spyOn(HTMLCanvasElement.prototype, 'getBoundingClientRect').mockReturnValue({ left: 0, top: 0, width: 320, height: 200, right: 320, bottom: 200, x: 0, y: 0, toJSON: () => ({}) })
})
afterEach(() => {
  browserLiveStore.__setWritesForTests(restore)
  browserLiveStore.__resetForTests()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

/** Let the ordered sends go out (each waits for the one before). */
const settle = () => act(async () => { for (let i = 0; i < 20; i += 1) await Promise.resolve() })

const mount = (over: Partial<React.ComponentProps<typeof BrowserLiveCardCanvas>> = {}) => {
  const ref = createRef<BrowserLiveCanvasHandle>()
  const utils = render(<BrowserLiveCardCanvas ref={ref} sessionKey={S} turnId="t1" visible interactive={false} aria-label="frame" {...over} />)
  return { ref, ...utils }
}

describe('painting', () => {
  it('sizes the canvas to the frame and draws it once decoded', async () => {
    mount()
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas') as HTMLCanvasElement
    expect(canvas.width).toBe(640)
    expect(canvas.height).toBe(400)
    expect(ctx.drawImage).not.toHaveBeenCalled()
    await act(async () => { flushDecodes(); await Promise.resolve() })
    expect(ctx.drawImage).toHaveBeenCalledTimes(1)
  })

  it('paints the password boxes over the image, in the card colour', async () => {
    mount()
    act(() => browserLiveStore.applyFrame(frame(1, { mask: [[10, 20, 100, 30], [0, 0, 5, 5]] })))
    await act(async () => { flushDecodes(); await Promise.resolve() })
    expect(ctx.fillRect.mock.calls).toEqual([[10, 20, 100, 30], [0, 0, 5, 5]])
    // drawImage first, then the mask: the order the spy saw them in.
    expect(ctx.drawImage.mock.invocationCallOrder[0]).toBeLessThan(ctx.fillRect.mock.invocationCallOrder[0])
  })

  it('drops frames that arrive while a decode is in flight and paints the newest', async () => {
    mount()
    act(() => {
      browserLiveStore.applyFrame(frame(1))
      browserLiveStore.applyFrame(frame(2))
      browserLiveStore.applyFrame(frame(3))
    })
    expect(createImageBitmap).toHaveBeenCalledTimes(1)
    await act(async () => { flushDecodes(); await Promise.resolve(); await Promise.resolve() })
    // The second decode is frame 3; frame 2 was never decoded.
    expect(createImageBitmap).toHaveBeenCalledTimes(2)
    await act(async () => { flushDecodes(); await Promise.resolve() })
    expect(ctx.drawImage).toHaveBeenCalledTimes(2)
  })

  it('paints nothing while off screen, then the latest frame on return', async () => {
    const { rerender } = mount({ visible: false })
    act(() => { browserLiveStore.applyFrame(frame(1)); browserLiveStore.applyFrame(frame(2)) })
    expect(createImageBitmap).not.toHaveBeenCalled()
    rerender(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive={false} aria-label="frame" />)
    expect(createImageBitmap).toHaveBeenCalledTimes(1)
    const blob = (createImageBitmap as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as Blob
    expect(blob.size).toBe(1)
  })

  it('replay walks the frames on the clock and ends by itself', async () => {
    vi.useFakeTimers()
    const { ref } = mount()
    const frames = [frame(1), frame(2), frame(3)]
    let done = false
    act(() => { void ref.current!.replay(frames).then(() => { done = true }) })
    expect(createImageBitmap).toHaveBeenCalledTimes(1)
    await act(async () => { flushDecodes(); await Promise.resolve() })
    // Recorded 100 ms apart.
    await act(async () => { vi.advanceTimersByTime(100); flushDecodes(); await Promise.resolve() })
    expect(createImageBitmap).toHaveBeenCalledTimes(2)
    await act(async () => { vi.advanceTimersByTime(100); flushDecodes(); await Promise.resolve() })
    expect(createImageBitmap).toHaveBeenCalledTimes(3)
    expect(done).toBe(false)
    await act(async () => { vi.advanceTimersByTime(600); flushDecodes(); await Promise.resolve() })
    expect(done).toBe(true)
  })
})

describe('takeover input', () => {
  it('posts nothing while not interactive', () => {
    mount()
    act(() => browserLiveStore.applyFrame(frame(1)))
    fireEvent.pointerDown(screen.getByTestId('browser-live-canvas'), { clientX: 10, clientY: 10, button: 0 })
    fireEvent.keyDown(screen.getByTestId('browser-live-canvas'), { key: 'a' })
    expect(writes.input).not.toHaveBeenCalled()
  })

  it('maps a click to frame pixels through the 320x200 box', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    await settle()
    expect(writes.input.mock.calls.map(c => c[1])).toEqual([
      expect.objectContaining({ type: 'mouse', action: 'down', x: 320, y: 200, button: 'left', clicks: 1 }),
      expect.objectContaining({ type: 'mouse', action: 'up', x: 320, y: 200, button: 'left' }),
    ])
  })

  it('coalesces moves to one per tick and flushes a pending move before a down', async () => {
    vi.useFakeTimers()
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerMove(canvas, { clientX: 10, clientY: 10, buttons: 0 })
    fireEvent.pointerMove(canvas, { clientX: 20, clientY: 20, buttons: 0 })
    fireEvent.pointerMove(canvas, { clientX: 30, clientY: 30, buttons: 0 })
    expect(writes.input).not.toHaveBeenCalled()
    act(() => { vi.advanceTimersByTime(16) })
    await settle()
    expect(writes.input).toHaveBeenCalledTimes(1)
    expect(writes.input.mock.calls[0][1]).toMatchObject({ action: 'move', x: 60, y: 60 })
    fireEvent.pointerMove(canvas, { clientX: 40, clientY: 40, buttons: 0 })
    fireEvent.pointerDown(canvas, { clientX: 40, clientY: 40, button: 0, pointerId: 1 })
    await settle()
    expect(writes.input.mock.calls.slice(1).map(c => c[1].action)).toEqual(['move', 'down'])
  })

  it('a key goes to the page; Esc goes to hand-back and never to the page', async () => {
    const onHandBack = vi.fn()
    mount({ interactive: true, onHandBack })
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.keyDown(canvas, { key: 'Enter', code: 'Enter' })
    fireEvent.keyUp(canvas, { key: 'Enter', code: 'Enter' })
    fireEvent.keyDown(canvas, { key: 'Escape', code: 'Escape' })
    fireEvent.keyUp(canvas, { key: 'Escape', code: 'Escape' })
    await settle()
    expect(writes.input.mock.calls.map(c => c[1])).toEqual([
      expect.objectContaining({ type: 'key', action: 'down', key: 'Enter' }),
      expect.objectContaining({ type: 'key', action: 'up', key: 'Enter' }),
    ])
    expect(onHandBack).toHaveBeenCalledTimes(1)
  })

  it('a keystroke typed into the page never reaches the window-level shortcut handler', async () => {
    // useKeyboardShortcuts listens on `window`; an Alt+digit that bubbled there
    // would switch session and unmount the card mid-takeover.
    const windowSaw = vi.fn()
    window.addEventListener('keydown', windowSaw)
    window.addEventListener('keyup', windowSaw)
    try {
      mount({ interactive: true, onHandBack: vi.fn() })
      const canvas = screen.getByTestId('browser-live-canvas')
      fireEvent.keyDown(canvas, { key: '1', code: 'Digit1', altKey: true })
      fireEvent.keyUp(canvas, { key: '1', code: 'Digit1', altKey: true })
      fireEvent.keyDown(canvas, { key: 'Escape', code: 'Escape' })
      await settle()
      expect(writes.input.mock.calls.map(c => c[1])).toEqual([
        expect.objectContaining({ type: 'key', action: 'down', key: '1', modifiers: 1 }),
        expect.objectContaining({ type: 'key', action: 'up', key: '1' }),
      ])
      expect(windowSaw).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', windowSaw)
      window.removeEventListener('keyup', windowSaw)
    }
  })

  it('a release past the drawn frame still posts, clamped to the edge', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    fireEvent.pointerUp(canvas, { clientX: 900, clientY: -30, button: 0, pointerId: 1 })
    await settle()
    const actions = writes.input.mock.calls.map(c => c[1])
    expect(actions.at(-1)).toMatchObject({ type: 'mouse', action: 'up', x: 639, y: 0, button: 'left' })
  })

  // happy-dom's WheelEvent drops the pointer fields, so the event is built by hand.
  const wheel = (canvas: HTMLElement, deltaY: number) => {
    const ev = new Event('wheel', { cancelable: true, bubbles: true })
    for (const [k, v] of Object.entries({ clientX: 160, clientY: 100, deltaX: 0, deltaY, deltaMode: 0, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false })) {
      Object.defineProperty(ev, k, { value: v })
    }
    canvas.dispatchEvent(ev)
    return ev
  }

  it('a wheel tick is posted with the pointer position and the deltas', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const ev = wheel(screen.getByTestId('browser-live-canvas'), 40)
    expect(ev.defaultPrevented).toBe(true)
    await settle()
    expect(writes.input.mock.calls[0][1]).toMatchObject({ type: 'wheel', x: 320, y: 200, dx: 0, dy: 40 })
  })

  /** The first press's POST stays in flight until `release()`. */
  const slowFirstDown = () => {
    let release!: () => void
    let held = false
    writes.input.mockImplementation((_k: string, ev: { action?: string }) => {
      if (ev.action === 'down' && !held) {
        held = true
        return new Promise(r => { release = () => r({ ok: true, sent: 1 }) })
      }
      return Promise.resolve({ ok: true, sent: 1 })
    })
    return () => release()
  }

  it('a drag keeps its order behind a slow press, and its moves coalesce to one', async () => {
    vi.useFakeTimers()
    const release = slowFirstDown()
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 10, clientY: 10, button: 0, buttons: 1, pointerId: 1 })
    await settle()
    for (const x of [20, 30, 40, 50, 60]) {
      fireEvent.pointerMove(canvas, { clientX: x, clientY: 10, buttons: 1 })
      act(() => { vi.advanceTimersByTime(16) })
      await settle()
    }
    fireEvent.pointerUp(canvas, { clientX: 60, clientY: 10, button: 0, pointerId: 1 })
    fireEvent.pointerMove(canvas, { clientX: 70, clientY: 10, buttons: 0 })
    act(() => { vi.advanceTimersByTime(16) })
    await settle()
    // Only the press is out; nothing has overtaken it.
    expect(writes.input.mock.calls.map(c => c[1].action)).toEqual(['down'])
    release()
    await settle()
    expect(writes.input.mock.calls.map(c => [c[1].action, c[1].x])).toEqual([['down', 20], ['move', 120], ['up', 120], ['move', 140]])
  })

  it('desktop wheels keep their order behind a slow press and add up while it is out', async () => {
    const release = slowFirstDown()
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    await settle()
    wheel(canvas, 10)
    wheel(canvas, 20)
    wheel(canvas, 30)
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    await settle()
    expect(writes.input).toHaveBeenCalledTimes(1)
    release()
    await settle()
    expect(writes.input.mock.calls.map(c => [c[1].type, c[1].action, c[1].dy])).toEqual([
      ['mouse', 'down', undefined], ['wheel', undefined, 60], ['mouse', 'up', undefined],
    ])
  })

  it('counts clicks itself: a second press 100 ms later is a double click, 600 ms later a single', async () => {
    vi.useFakeTimers()
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    const click = () => {
      fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, detail: 0 })
      fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, detail: 0 })
    }
    click()
    act(() => { vi.advanceTimersByTime(100) })
    click()
    act(() => { vi.advanceTimersByTime(600) })
    click()
    await settle()
    expect(writes.input.mock.calls.map(c => [c[1].action, c[1].clicks])).toEqual([
      ['down', 1], ['up', 1], ['down', 2], ['up', 2], ['down', 1], ['up', 1],
    ])
  })

  // happy-dom builds no TouchList, so touch events are built by hand.
  const touch = (el: HTMLElement, type: string, pts: Array<[number, number]>, ended = false) => {
    const ev = new Event(type, { bubbles: true, cancelable: true })
    const list = pts.map(([clientX, clientY], i) => ({ identifier: i, clientX, clientY }))
    Object.defineProperty(ev, 'touches', { value: ended ? [] : list })
    Object.defineProperty(ev, 'changedTouches', { value: list.slice(0, 1) })
    el.dispatchEvent(ev)
    return ev
  }
  const tap = (canvas: HTMLElement, x = 160, y = 100) => {
    touch(canvas, 'touchstart', [[x, y]])
    return touch(canvas, 'touchend', [[x, y]], true)
  }

  it('a tap focuses the keyboard proxy inside the touchend itself, then clicks the page in order', async () => {
    writes.input.mockImplementation(async (_k: string, ev: { action?: string }) =>
      ev.action === 'up' ? { sent: 1, focus: { editable: true, kind: 'number', mode: '' } } : { sent: 1 })
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    let focusedInHandler = false
    canvas.addEventListener('touchend', () => { focusedInHandler = document.activeElement === proxy })
    const end = tap(canvas)
    // Synchronously, in the same task as the tap: iOS opens no keyboard otherwise.
    expect(focusedInHandler).toBe(true)
    // The phone's own emulated mouse click is cancelled: it would focus the canvas.
    expect(end.defaultPrevented).toBe(true)
    expect(proxy.inputMode).toBe('text')
    await settle()
    expect(document.activeElement).toBe(proxy)
    expect(proxy.inputMode).toBe('decimal')
    expect(writes.input.mock.calls.map(c => [c[1].type, c[1].action, c[1].x, c[1].y])).toEqual([['mouse', 'down', 320, 200], ['mouse', 'up', 320, 200]])
  })

  it('a tap on a password field types into a password box', async () => {
    writes.input.mockImplementation(async () => ({ sent: 1, focus: { editable: true, kind: 'password', mode: '' } }))
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    tap(screen.getByTestId('browser-live-canvas'))
    await settle()
    expect((screen.getByTestId('browser-live-type-proxy') as HTMLInputElement).type).toBe('password')
  })

  it('a tap on something that takes no typing closes the keyboard again', async () => {
    writes.input.mockImplementation(async () => ({ sent: 1, focus: { editable: false, kind: '', mode: '' } }))
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    tap(screen.getByTestId('browser-live-canvas'))
    const proxy = screen.getByTestId('browser-live-type-proxy')
    expect(document.activeElement).toBe(proxy)
    await settle()
    expect(document.activeElement).not.toBe(proxy)
  })

  it('a swipe scrolls the page with wheel deltas (finger up, page down) and clicks nothing', async () => {
    vi.useFakeTimers()
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    touch(canvas, 'touchstart', [[160, 150]])
    const mv = touch(canvas, 'touchmove', [[160, 120]])
    touch(canvas, 'touchmove', [[160, 100]])
    expect(mv.defaultPrevented).toBe(true)
    touch(canvas, 'touchend', [[160, 100]], true)
    await settle()
    expect(document.activeElement).not.toBe(screen.getByTestId('browser-live-type-proxy'))
    // 50 CSS px up in a 320x200 box drawing a 640x400 frame: 100 frame px down, at the start point.
    expect(writes.input.mock.calls.map(c => c[1])).toEqual([{ type: 'wheel', x: 320, y: 300, dx: 0, dy: 100, modifiers: 0, w: 640 }])
  })

  it('a finger that wanders less than the slop is still a tap', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    touch(canvas, 'touchstart', [[160, 100]])
    touch(canvas, 'touchmove', [[164, 104]])
    touch(canvas, 'touchend', [[164, 104]], true)
    await settle()
    expect(writes.input.mock.calls.map(c => c[1].action)).toEqual(['down', 'up'])
  })

  it('a touch makes no pointer-event clicks of its own (touch events carry it)', () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 7, pointerType: 'touch' })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 7, pointerType: 'touch' })
    expect(writes.input).not.toHaveBeenCalled()
  })

  it('a mouse press gives the typing proxy focus, at the pointer, and the canvas wears the focus ring', () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    expect(document.activeElement).toBe(proxy)
    expect(proxy.style.left).toBe('160px')
    expect(proxy.style.top).toBe('100px')
    // No phone keyboard handling on a desktop press.
    expect(proxy.inputMode).toBe('')
    expect(canvas.getAttribute('data-proxy-focus')).toBe('true')
  })

  it('keyboard focus on the canvas (Tab) moves to the typing proxy while in control', () => {
    mount({ interactive: true })
    const canvas = screen.getByTestId('browser-live-canvas')
    act(() => canvas.focus())
    expect(document.activeElement).toBe(screen.getByTestId('browser-live-type-proxy'))
    expect(canvas.getAttribute('data-proxy-focus')).toBe('true')
  })

  it('desktop IME: a composition types its text into the page and no placeholder key goes out', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    await settle()
    writes.input.mockClear()
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    const fill = proxy.value
    fireEvent.keyDown(proxy, { key: 'Process', code: 'KeyN', keyCode: 229 })
    proxy.dispatchEvent(new Event('compositionstart'))
    fireEvent.keyDown(proxy, { key: 'Process', code: 'KeyI', keyCode: 229, isComposing: true })
    edit(proxy, fill + '你')
    proxy.dispatchEvent(new Event('compositionend'))
    // The canvas fallback never forwards a placeholder key either.
    fireEvent.keyDown(canvas, { key: 'Process', code: 'KeyN', keyCode: 229 })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    await settle()
    expect(sent()).toEqual([text('你')])
  })

  it('desktop keys on the proxy: Enter and ArrowLeft go as keys in order, Esc hands back', async () => {
    const onHandBack = vi.fn()
    mount({ interactive: true, onHandBack })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    await settle()
    writes.input.mockClear()
    const proxy = screen.getByTestId('browser-live-type-proxy')
    expect(document.activeElement).toBe(proxy)
    fireEvent.keyDown(proxy, { key: 'Enter', code: 'Enter', keyCode: 13 })
    fireEvent.keyUp(proxy, { key: 'Enter', code: 'Enter', keyCode: 13 })
    fireEvent.keyDown(proxy, { key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37 })
    fireEvent.keyUp(proxy, { key: 'ArrowLeft', code: 'ArrowLeft', keyCode: 37 })
    fireEvent.keyDown(proxy, { key: 'a', code: 'KeyA', keyCode: 65, ctrlKey: true })
    fireEvent.keyUp(proxy, { key: 'a', code: 'KeyA', keyCode: 65, ctrlKey: true })
    fireEvent.keyDown(proxy, { key: 'Escape', code: 'Escape', keyCode: 27 })
    await settle()
    expect(writes.input.mock.calls.map(c => [c[1].action, c[1].key])).toEqual([
      ['down', 'Enter'], ['up', 'Enter'], ['down', 'ArrowLeft'], ['up', 'ArrowLeft'], ['down', 'a'], ['up', 'a'],
    ])
    expect(onHandBack).toHaveBeenCalledTimes(1)
    expect(document.activeElement).not.toBe(proxy)
  })

  /** The keyboard edits the proxy; the browser then fires `input`. */
  const edit = (el: HTMLInputElement, value: string) => {
    el.value = value
    el.dispatchEvent(new Event('input', { bubbles: true }))
  }
  const sent = () => writes.input.mock.calls.map(c => c[1])
  const text = (t: string) => ({ type: 'key', action: 'press', key: '', text: t, modifiers: 0 })
  const bs = [expect.objectContaining({ action: 'down', key: 'Backspace' }), expect.objectContaining({ action: 'up', key: 'Backspace' })]

  it('typed text and Backspace in an empty proxy reach the page; the proxy forgets each', async () => {
    mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    const fill = proxy.value
    edit(proxy, fill + '7 ')
    expect(proxy.value).toBe(fill)
    edit(proxy, '')
    expect(proxy.value).toBe(fill)
    edit(proxy, fill + 'ab ')
    expect(proxy.value).toBe(fill)
    await settle()
    expect(sent()).toEqual([text('7 '), ...bs, text('ab ')])
  })

  it('a composing keyboard (Android words, pinyin) types live and its replacement is diffed', async () => {
    mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    const fill = proxy.value
    proxy.dispatchEvent(new Event('compositionstart'))
    edit(proxy, fill + 'ni')
    edit(proxy, fill + 'nih')
    expect(proxy.value).toBe(fill + 'nih')
    edit(proxy, fill + '你好')
    proxy.dispatchEvent(new Event('compositionend'))
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    await settle()
    expect(proxy.value).toBe(fill)
    expect(sent()).toEqual([text('ni'), text('h'), ...bs, ...bs, ...bs, text('你好')])
  })

  it('keys on the proxy: named keys go as keys, printable ones and Backspace through the edit, none reach the window', async () => {
    const windowSaw = vi.fn()
    window.addEventListener('keydown', windowSaw)
    try {
      mount({ interactive: true })
      const proxy = screen.getByTestId('browser-live-type-proxy')
      expect(fireEvent.keyDown(proxy, { key: '5', code: 'Digit5', keyCode: 53 })).toBe(true)
      expect(fireEvent.keyDown(proxy, { key: 'Backspace', code: 'Backspace', keyCode: 8 })).toBe(true)
      fireEvent.keyDown(proxy, { key: 'Enter', code: 'Enter', keyCode: 13 })
      fireEvent.keyUp(proxy, { key: 'Enter', code: 'Enter', keyCode: 13 })
      await settle()
      expect(writes.input.mock.calls.map(c => [c[1].action, c[1].key])).toEqual([['down', 'Enter'], ['up', 'Enter']])
      expect(windowSaw).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', windowSaw)
    }
  })

  it('a failed send does not stop the ones after it', async () => {
    writes.input.mockImplementationOnce(async () => { throw new Error('net') })
    mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    edit(proxy, proxy.value + 'a')
    edit(proxy, proxy.value + 'b')
    await settle()
    expect(sent()).toEqual([text('a'), text('b')])
  })

  it('what is still queued when the person hands back never reaches the page', async () => {
    let release!: () => void
    writes.input.mockImplementationOnce(() => new Promise(r => { release = () => r({ sent: 1 }) }))
    const { rerender } = mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    edit(proxy, proxy.value + 'a')
    edit(proxy, proxy.value + 'b')
    await settle()
    rerender(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive={false} aria-label="frame" />)
    release()
    await settle()
    expect(sent()).toEqual([text('a')])
  })

  it('autocorrect replacing the word being typed (no composition) is diffed, not appended', async () => {
    mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    const fill = proxy.value
    for (const v of ['t', 'te', 'teh']) edit(proxy, fill + v)
    edit(proxy, fill + 'the ')
    await settle()
    expect(proxy.value).toBe(fill)
    expect(sent()).toEqual([text('t'), text('e'), text('h'), ...bs, ...bs, text('he ')])
  })

  it('a password is forgotten at every keystroke, and a blur forgets any word', async () => {
    writes.input.mockImplementation(async (_k: string, ev: { action?: string }) =>
      ev.action === 'up' ? { sent: 1, focus: { editable: true, kind: 'password', mode: '' } } : { sent: 1 })
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    tap(screen.getByTestId('browser-live-canvas'))
    await settle()
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    const fill = proxy.value
    edit(proxy, fill + 's')
    expect(proxy.value).toBe(fill)
    proxy.type = 'text'
    edit(proxy, fill + 'wor')
    expect(proxy.value).toBe(fill + 'wor')
    act(() => proxy.blur())
    expect(proxy.value).toBe(fill)
  })

  it('typing reaches the page only while in control', async () => {
    mount({ interactive: false })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    edit(proxy, proxy.value + 'x')
    fireEvent.keyDown(proxy, { key: 'Enter', code: 'Enter', keyCode: 13 })
    await settle()
    expect(writes.input).not.toHaveBeenCalled()
  })

  /** A desktop mouse click on the frame: the proxy now types for a desktop keyboard. */
  const desktopClick = async (x = 160, y = 100) => {
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: x, clientY: y, button: 0, pointerId: 1, pointerType: 'mouse' })
    fireEvent.pointerUp(canvas, { clientX: x, clientY: y, button: 0, pointerId: 1, pointerType: 'mouse' })
    await settle()
    writes.input.mockClear()
    return screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
  }

  it('desktop: a printable key goes to the page as a real key and leaves the proxy alone', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const proxy = await desktopClick()
    const fill = proxy.value
    expect(fireEvent.keyDown(proxy, { key: 'a', code: 'KeyA', keyCode: 65 })).toBe(false)
    await settle()
    // Key repeat: a repeated down goes out too once the one before it has.
    fireEvent.keyDown(proxy, { key: 'a', code: 'KeyA', keyCode: 65, repeat: true })
    fireEvent.keyUp(proxy, { key: 'a', code: 'KeyA', keyCode: 65 })
    expect(fireEvent.keyDown(proxy, { key: 'Backspace', code: 'Backspace', keyCode: 8 })).toBe(false)
    fireEvent.keyUp(proxy, { key: 'Backspace', code: 'Backspace', keyCode: 8 })
    await settle()
    expect(proxy.value).toBe(fill)
    expect(sent().map(e => [e.action, e.key, e.text])).toEqual([
      ['down', 'a', 'a'], ['down', 'a', 'a'], ['up', 'a', undefined], ['down', 'Backspace', undefined], ['up', 'Backspace', undefined],
    ])
    expect(sent().some(e => e.action === 'press')).toBe(false)
  })

  it('a move waiting on its tick is sent before a key pressed meanwhile', async () => {
    vi.useFakeTimers()
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerMove(canvas, { clientX: 30, clientY: 30, buttons: 0 })
    fireEvent.keyDown(canvas, { key: 'Enter', code: 'Enter' })
    await settle()
    act(() => { vi.advanceTimersByTime(16) })
    await settle()
    expect(sent().map(e => [e.type, e.action])).toEqual([['mouse', 'move'], ['key', 'down']])
  })

  it('alternating moves and wheels behind a stalled POST stay one coalesced job', async () => {
    vi.useFakeTimers()
    const release = slowFirstDown()
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    await settle()
    for (let i = 0; i < 10; i += 1) {
      fireEvent.pointerMove(canvas, { clientX: 100 + i, clientY: 100, buttons: 0 })
      act(() => { vi.advanceTimersByTime(16) })
      wheel(canvas, 10)
      await settle()
    }
    expect(writes.input).toHaveBeenCalledTimes(1)
    release()
    await settle()
    expect(sent().slice(1).map(e => [e.type, e.action, e.x, e.dy])).toEqual([['mouse', 'move', 218, undefined], ['wheel', undefined, 320, 100]])
  })

  it('hand back then retake while a POST is stalled: nothing queued before reaches the new takeover', async () => {
    let release!: () => void
    writes.input.mockImplementationOnce(() => new Promise(r => { release = () => r({ sent: 1 }) }))
    const { rerender } = mount({ interactive: true })
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    edit(proxy, proxy.value + 'a')
    edit(proxy, proxy.value + 'b')
    await settle()
    rerender(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive={false} aria-label="frame" />)
    rerender(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive aria-label="frame" />)
    edit(proxy, proxy.value + 'c')
    release()
    await settle()
    expect(sent()).toEqual([text('a'), text('c')])
  })

  it('Esc during a composition cancels the candidate and does not hand back', async () => {
    const onHandBack = vi.fn()
    mount({ interactive: true, onHandBack })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const proxy = await desktopClick()
    proxy.dispatchEvent(new Event('compositionstart'))
    expect(fireEvent.keyDown(proxy, { key: 'Escape', code: 'Escape', keyCode: 27, isComposing: true })).toBe(true)
    await settle()
    expect(onHandBack).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(proxy)
    expect(sent()).toEqual([])
  })

  it('a key held when the proxy blurs, or at hand-back, is released on the page', async () => {
    const { rerender } = mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const proxy = await desktopClick()
    fireEvent.keyDown(proxy, { key: 'a', code: 'KeyA', keyCode: 65 })
    act(() => proxy.blur())
    await settle()
    expect(sent().map(e => [e.action, e.key])).toEqual([['down', 'a'], ['up', 'a']])
    writes.input.mockClear()
    await desktopClick()
    fireEvent.keyDown(proxy, { key: 'w', code: 'KeyW', keyCode: 87 })
    await settle()
    rerender(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive={false} aria-label="frame" />)
    await settle()
    expect(sent().map(e => [e.action, e.key])).toEqual([['down', 'w'], ['up', 'w']])
  })

  it('desktop: a click on a page password field makes the proxy a password box, and a non-field keeps it focused', async () => {
    let focus = { editable: true, kind: 'password', mode: '' }
    writes.input.mockImplementation(async (_k: string, ev: { action?: string }) => (ev.action === 'up' ? { sent: 1, focus } : { sent: 1 }))
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const proxy = await desktopClick()
    expect(proxy.type).toBe('password')
    focus = { editable: false, kind: '', mode: '' }
    await desktopClick(200, 50)
    expect(proxy.type).toBe('text')
    expect(document.activeElement).toBe(proxy)
  })

  it('a drag between press and release ends the multi-click: the next press is a single', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, buttons: 1, pointerId: 1 })
    fireEvent.pointerMove(canvas, { clientX: 200, clientY: 100, buttons: 1 })
    fireEvent.pointerMove(canvas, { clientX: 160, clientY: 100, buttons: 1 })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, buttons: 1, pointerId: 1 })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1 })
    await settle()
    expect(sent().filter(e => e.action !== 'move').map(e => [e.action, e.clicks])).toEqual([['down', 1], ['up', 1], ['down', 1], ['up', 1]])
  })

  const handBack = (rerender: (ui: React.ReactElement) => void) =>
    rerender(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive={false} aria-label="frame" />)
  const retake = (rerender: (ui: React.ReactElement) => void) =>
    rerender(<BrowserLiveCardCanvas sessionKey={S} turnId="t1" visible interactive aria-label="frame" />)
  /** The next POST stays in flight until the returned function resolves it. */
  const stallNext = () => {
    let resolve!: (v: unknown) => void
    writes.input.mockImplementationOnce(() => new Promise(r => { resolve = r }))
    return (v: unknown = { ok: true, sent: 1 }) => resolve(v)
  }

  it('desktop: the letter typed right after an IME commit goes out; the commit Enter does not', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const proxy = await desktopClick()
    proxy.dispatchEvent(new Event('compositionstart'))
    proxy.dispatchEvent(new CompositionEvent('compositionend', { data: '你' }))
    // Inside the 50 ms post-composition window.
    fireEvent.keyDown(proxy, { key: 'Enter', code: 'Enter', keyCode: 13 })
    fireEvent.keyDown(proxy, { key: 'w', code: 'KeyW', keyCode: 87 })
    fireEvent.keyUp(proxy, { key: 'w', code: 'KeyW', keyCode: 87 })
    await settle()
    expect(sent().filter(e => e.type === 'key' && e.action !== 'press').map(e => [e.action, e.key])).toEqual([['down', 'w'], ['up', 'w']])
  })

  it('a key queued behind a stalled POST and dropped by a hand-back is never released either', async () => {
    const { rerender } = mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const proxy = await desktopClick()
    const release = stallNext()
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, buttons: 1, pointerId: 1, pointerType: 'mouse' })
    await settle()
    fireEvent.keyDown(proxy, { key: 'k', code: 'KeyK', keyCode: 75 })
    fireEvent.keyDown(canvas, { key: 'j', code: 'KeyJ' })
    handBack(rerender)
    release()
    await settle()
    // The press was out, so its button is let go; the keys were never sent, so no release.
    expect(sent().map(e => [e.type, e.action])).toEqual([['mouse', 'down'], ['mouse', 'up']])
  })

  it('a release still queued when the card takes control again is dropped', async () => {
    const { rerender } = mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const proxy = await desktopClick()
    fireEvent.keyDown(proxy, { key: 'k', code: 'KeyK', keyCode: 75 })
    await settle()
    const release = stallNext()
    fireEvent.keyDown(proxy, { key: 'j', code: 'KeyJ', keyCode: 74 })
    await settle()
    handBack(rerender)
    retake(rerender)
    release()
    await settle()
    // The k-up was queued behind the stalled j-down for the old takeover; the new
    // takeover may be holding k, so the old release must not lift it.
    const ups = sent().filter(e => e.type === 'key' && e.action === 'up').map(e => e.key)
    expect(ups).toEqual([])
  })

  it('a key down whose POST failed is not held: a blur sends no lone key-up', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const proxy = await desktopClick()
    writes.input.mockImplementationOnce(async () => { throw new Error('net') })
    fireEvent.keyDown(proxy, { key: 'a', code: 'KeyA', keyCode: 65 })
    act(() => proxy.blur())
    await settle()
    expect(sent().map(e => [e.action, e.key])).toEqual([['down', 'a']])
  })

  it('a press the gateway answered with nothing dispatched sends no lone release at hand-back', async () => {
    const { rerender } = mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    writes.input.mockImplementationOnce(async () => ({ sent: 0 }))
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, buttons: 1, pointerId: 1, pointerType: 'mouse' })
    await settle()
    handBack(rerender)
    await settle()
    expect(sent().map(e => [e.type, e.action])).toEqual([['mouse', 'down']])
  })

  it('a pointercancel during a press releases the page button once, at the last position', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, buttons: 1, pointerId: 1 })
    fireEvent.pointerMove(canvas, { clientX: 200, clientY: 100, buttons: 1 })
    fireEvent.pointerCancel(canvas, { pointerId: 1 })
    fireEvent.pointerUp(canvas, { clientX: 200, clientY: 100, button: 0, pointerId: 1 })
    await settle()
    expect(sent().filter(e => e.action !== 'move').map(e => [e.action, e.button, e.x])).toEqual([['down', 'left', 320], ['up', 'left', 400]])
  })

  it('a hand-back during a press releases the page button once', async () => {
    const { rerender } = mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 2, buttons: 2, pointerId: 1 })
    await settle()
    handBack(rerender)
    await settle()
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 2, pointerId: 1 })
    await settle()
    expect(sent().map(e => [e.action, e.button])).toEqual([['down', 'right'], ['up', 'right']])
  })

  it('a press in the letterbox sends no release and handles no reply', async () => {
    writes.input.mockImplementation(async () => ({ sent: 1, focus: { editable: true, kind: 'password', mode: '' } }))
    mount({ interactive: true })
    // 640x200 in a 320x200 box: drawn 320x100, letterbox above y=50.
    act(() => browserLiveStore.applyFrame(frame(1, { w: 640, h: 200 })))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 10, button: 0, pointerId: 1, pointerType: 'mouse' })
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 10, button: 0, pointerId: 1, pointerType: 'mouse' })
    await settle()
    expect(writes.input).not.toHaveBeenCalled()
    expect((screen.getByTestId('browser-live-type-proxy') as HTMLInputElement).type).toBe('text')
  })

  it('a click reply that lands after a hand-back and retake changes nothing', async () => {
    const { rerender } = mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.pointerDown(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    await settle()
    const reply = stallNext()
    fireEvent.pointerUp(canvas, { clientX: 160, clientY: 100, button: 0, pointerId: 1, pointerType: 'mouse' })
    await settle()
    handBack(rerender)
    retake(rerender)
    act(() => canvas.focus())
    const proxy = screen.getByTestId('browser-live-type-proxy') as HTMLInputElement
    expect(document.activeElement).toBe(proxy)
    reply({ sent: 1, focus: { editable: true, kind: 'password', mode: '' } })
    await settle()
    expect(proxy.type).toBe('text')
  })

  it('key repeats behind a stalled POST queue at most one more down per key', async () => {
    mount({ interactive: true })
    act(() => browserLiveStore.applyFrame(frame(1)))
    const proxy = await desktopClick()
    const release = stallNext()
    fireEvent.keyDown(proxy, { key: 'a', code: 'KeyA', keyCode: 65 })
    await settle()
    for (let i = 0; i < 20; i += 1) fireEvent.keyDown(proxy, { key: 'a', code: 'KeyA', keyCode: 65, repeat: true })
    const canvas = screen.getByTestId('browser-live-canvas')
    for (let i = 0; i < 20; i += 1) fireEvent.keyDown(canvas, { key: 'b', code: 'KeyB', repeat: i > 0 })
    release()
    await settle()
    expect(sent().map(e => [e.action, e.key])).toEqual([['down', 'a'], ['down', 'a'], ['down', 'b']])
  })

  it('a key pressed on the canvas fallback is released at hand-back', async () => {
    const { rerender } = mount({ interactive: true })
    const canvas = screen.getByTestId('browser-live-canvas')
    fireEvent.keyDown(canvas, { key: 'ArrowUp', code: 'ArrowUp' })
    fireEvent.keyDown(canvas, { key: 'Process', code: 'KeyN', keyCode: 229 })
    await settle()
    handBack(rerender)
    await settle()
    expect(sent().map(e => [e.action, e.key])).toEqual([['down', 'ArrowUp'], ['up', 'ArrowUp']])
  })

  it('a click on the live (non-interactive) frame claims', () => {
    const onClaim = vi.fn()
    mount({ onClaim })
    fireEvent.click(screen.getByTestId('browser-live-canvas'))
    expect(onClaim).toHaveBeenCalledTimes(1)
  })
})
