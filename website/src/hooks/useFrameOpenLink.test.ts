import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { OPEN_MESSAGE_TYPE, useFrameOpenLink } from './useFrameOpenLink'

const URL_A = 'https://github.com/kirodotdev/KiroCrew/pull/18518'

/** The URL rule is the caller's (`webUrlOf`, tested with the bridge); this one
 *  takes any URL on the open type, so these tests pin only frame and gesture. */
const accept = (data: unknown) => {
  const msg = data as { type?: unknown; url?: unknown }
  return msg?.type === OPEN_MESSAGE_TYPE && typeof msg.url === 'string' ? msg.url : null
}

/** A frame stand-in whose window is a unique object: the hook compares identity only. */
function mountFrame() {
  return { contentWindow: {} as Window } as HTMLIFrameElement
}

function post(data: unknown, source: unknown) {
  const event = new MessageEvent('message', { data })
  Object.defineProperty(event, 'source', { value: source })
  window.dispatchEvent(event)
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useFrameOpenLink', () => {
  it('opens what its own frame asks for, exactly once', () => {
    const frame = mountFrame()
    const open = vi.fn()
    renderHook(() => useFrameOpenLink([{ current: frame }], accept, open, () => true))
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(open).toHaveBeenCalledTimes(1)
    expect(open).toHaveBeenCalledWith(URL_A, frame)
  })

  it('opens nothing the accept rule refuses', () => {
    const frame = mountFrame()
    const open = vi.fn()
    renderHook(() => useFrameOpenLink([{ current: frame }], () => null, open, () => true))
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(open).not.toHaveBeenCalled()
  })

  it('ignores a valid message from a window that is not its frame', () => {
    const mine = mountFrame()
    const other = mountFrame()
    const open = vi.fn()
    renderHook(() => useFrameOpenLink([{ current: mine }], accept, open, () => true))
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, other.contentWindow)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, window)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, null)
    expect(open).not.toHaveBeenCalled()
  })

  it('opens through window.open with noopener and noreferrer by default', () => {
    const frame = mountFrame()
    const spy = vi.spyOn(window, 'open').mockImplementation(() => null)
    renderHook(() => useFrameOpenLink([{ current: frame }], accept, undefined, () => true))
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith(URL_A, '_blank', 'noopener,noreferrer')
    spy.mockRestore()
  })

  it('opens nothing when the frame posts without a user gesture', () => {
    const frame = mountFrame()
    const open = vi.fn()
    renderHook(() => useFrameOpenLink([{ current: frame }], accept, open, () => false))
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(open).not.toHaveBeenCalled()
  })

  it('needs an activation AND focus on the asking frame by default', () => {
    const frame = mountFrame()
    const box = document.createElement('textarea')
    const open = vi.fn()
    const nav = navigator as Navigator & { userActivation?: { isActive: boolean } }
    const hadActivation = Object.getOwnPropertyDescriptor(nav, 'userActivation')
    let focused: Element = box
    const focus = vi.spyOn(document, 'activeElement', 'get').mockImplementation(() => focused)
    const activate = (isActive: boolean) =>
      Object.defineProperty(nav, 'userActivation', { configurable: true, value: { isActive } })
    renderHook(() => useFrameOpenLink([{ current: frame }], accept, open))
    // No activation, focus on the frame: nothing.
    focused = frame
    activate(false)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    // Typing in the composer: an activation, but the focus is not the frame.
    focused = box
    activate(true)
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(open).not.toHaveBeenCalled()
    // A click in the frame: activation, and the frame holds the host's focus.
    focused = frame
    post({ type: OPEN_MESSAGE_TYPE, url: URL_A }, frame.contentWindow)
    expect(open).toHaveBeenCalledTimes(1)
    focus.mockRestore()
    if (hadActivation) Object.defineProperty(nav, 'userActivation', hadActivation)
    else delete (nav as { userActivation?: unknown }).userActivation
  })
})
