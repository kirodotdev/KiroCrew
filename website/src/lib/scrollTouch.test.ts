/**
 * The finger phases' renderer half: the preload bridge in the app's own window,
 * or the host relay in a remote crew's pane, accepted only from the parent
 * frame at the origin the pane asked, for its own watch.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  subscribeScrollTouch, subscribeScrollTouchNative, asScrollTouchPhase,
  SCROLL_TOUCH_WATCH_TYPE, SCROLL_TOUCH_CANCEL_TYPE, SCROLL_TOUCH_PHASE_TYPE,
} from './scrollTouch'

vi.mock('./embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))
vi.mock('./nativeNotify', () => ({ relayTargetOrigin: vi.fn(() => null) }))
import { isEmbeddedPane } from './embedded'
import { relayTargetOrigin } from './nativeNotify'

const HUB = 'http://127.0.0.1:7777'

afterEach(() => {
  delete (window as Window & { electronAPI?: unknown }).electronAPI
  vi.mocked(isEmbeddedPane).mockReturnValue(false)
  vi.mocked(relayTargetOrigin).mockReturnValue(null)
  vi.restoreAllMocks()
})

describe('asScrollTouchPhase', () => {
  it('admits the two words only', () => {
    expect(asScrollTouchPhase('begin')).toBe('begin')
    expect(asScrollTouchPhase('end')).toBe('end')
    for (const v of ['BEGIN', '', null, 1, { phase: 'end' }]) expect(asScrollTouchPhase(v)).toBeNull()
  })
})

describe('subscribeScrollTouch: native bridge', () => {
  it('passes valid phases through and returns its stop', () => {
    let emit: ((p: unknown) => void) | null = null
    const stop = vi.fn()
    ;(window as Window & { electronAPI?: unknown }).electronAPI = {
      onScrollTouch: (cb: (p: unknown) => void) => { emit = cb; return stop },
    }
    const got: string[] = []
    const off = subscribeScrollTouch((p) => got.push(p))
    expect(off).not.toBeNull()
    emit!('begin')
    emit!('nope')
    emit!('end')
    expect(got).toEqual(['begin', 'end'])
    off!()
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it('is null in a plain browser, top-level', () => {
    expect(subscribeScrollTouchNative(() => {})).toBeNull()
    expect(subscribeScrollTouch(() => {})).toBeNull()
  })
})

describe('subscribeScrollTouch: host relay in a pane', () => {
  function asPane() {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    vi.mocked(relayTargetOrigin).mockReturnValue(HUB)
    const posts: Array<[unknown, string]> = []
    vi.spyOn(window.parent, 'postMessage').mockImplementation(((d: unknown, o: string) => { posts.push([d, o]) }) as typeof window.postMessage)
    return posts
  }
  const deliver = (data: unknown, origin = HUB, source: MessageEventSource | null = window.parent) =>
    window.dispatchEvent(new MessageEvent('message', { data, origin, source }))

  it('asks the hub at its exact origin and hears only its own watch', () => {
    const posts = asPane()
    const got: string[] = []
    const off = subscribeScrollTouch((p) => got.push(p))
    expect(off).not.toBeNull()
    expect(posts).toHaveLength(1)
    const [req, origin] = posts[0] as [{ type: string; v: number; id: string }, string]
    expect(origin).toBe(HUB)
    expect(req.type).toBe(SCROLL_TOUCH_WATCH_TYPE)
    const phase = (p: unknown, extra: Record<string, unknown> = {}) =>
      ({ type: SCROLL_TOUCH_PHASE_TYPE, v: 1, id: req.id, phase: p, ...extra })

    deliver(phase('begin'))
    deliver(phase('end'), 'http://127.0.0.1:9999')
    deliver(phase('end'), HUB, null)
    deliver(phase('end', { id: 'someone-else' }))
    deliver(phase('end', { v: 2 }))
    deliver(phase('sideways'))
    deliver(phase('end'))
    expect(got).toEqual(['begin', 'end'])

    off!()
    expect(posts[1]).toEqual([{ type: SCROLL_TOUCH_CANCEL_TYPE, v: 1, id: req.id }, HUB])
    deliver(phase('begin'))
    expect(got).toEqual(['begin', 'end'])
  })

  it('is null in a pane with no hub origin to address', () => {
    vi.mocked(isEmbeddedPane).mockReturnValue(true)
    expect(subscribeScrollTouch(() => {})).toBeNull()
  })
})
