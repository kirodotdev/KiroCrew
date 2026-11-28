/**
 * The guide's viewport store (guideActions.ts `subscribeGuideViewport`) wakes
 * on everything that moves the layout form: the 768px width query, a fold at
 * the same width, and a route change while folded (the fold's two-pane form
 * holds on the chat route only, lib/touchLayoutForm.ts).
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { subscribeGuideViewport } from './guideActions'
import { FOLD_TWO_PANE_QUERY } from '../hooks/useViewportSegments'
import { LAYOUT_ROUTE_CHANGED_EVENT } from '../lib/touchLayoutForm'

const listeners = new Map<string, Set<() => void>>()
function stubMatchMedia() {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false,
    media: q,
    addEventListener: (_t: string, cb: () => void) => {
      if (!listeners.has(q)) listeners.set(q, new Set())
      listeners.get(q)!.add(cb)
    },
    removeEventListener: (_t: string, cb: () => void) => { listeners.get(q)?.delete(cb) },
  }))
}
const fire = (q: string) => { for (const cb of [...(listeners.get(q) ?? [])]) cb() }

afterEach(() => { vi.unstubAllGlobals(); listeners.clear() })

describe('the guide viewport store', () => {
  it('wakes on a fold at the same width and on a route change, and stops after unsubscribe', () => {
    stubMatchMedia()
    const onChange = vi.fn()
    const unsubscribe = subscribeGuideViewport(onChange)
    fire(FOLD_TWO_PANE_QUERY)
    expect(onChange).toHaveBeenCalledTimes(1)
    window.dispatchEvent(new Event(LAYOUT_ROUTE_CHANGED_EVENT))
    expect(onChange).toHaveBeenCalledTimes(2)
    fire('(max-width: 767px)')
    expect(onChange).toHaveBeenCalledTimes(3)
    unsubscribe()
    fire(FOLD_TWO_PANE_QUERY)
    window.dispatchEvent(new Event(LAYOUT_ROUTE_CHANGED_EVENT))
    expect(onChange).toHaveBeenCalledTimes(3)
  })
})
