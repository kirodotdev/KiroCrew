/**
 * A route change re-reads the layout form before the new route paints
 * (lib/touchLayoutForm.ts `useLayoutRouteSignal`, called by the shell). At a
 * 669px fold the fold's two-pane form holds on the chat route only, so leaving
 * /chat for /settings must never paint one frame in the two-pane form. The
 * route is changed with `flushSync` and no `act`; a later sibling's layout
 * effect records whether the re-read had fired by the end of that commit's
 * layout phase, the last point before the browser paints.
 * The guide's viewport reader (guideActions.ts) must read the new form on that
 * same first paint.
 */
import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest'
import { useLayoutEffect, useSyncExternalStore } from 'react'
import { flushSync } from 'react-dom'
import { createRoot, type Root } from 'react-dom/client'
import { FOLD_TWO_PANE_QUERY } from '../hooks/useViewportSegments'

const media = { width: 669, folded: true }
const listeners = new Set<() => void>()

let useIsMobile: () => boolean
let useLayoutRouteSignal: (pathname: string) => void
let LAYOUT_ROUTE_CHANGED_EVENT: string
let subscribeGuideViewport: (cb: () => void) => () => void
let currentGuideViewport: () => 'desktop' | 'mobile'

beforeAll(async () => {
  vi.resetModules()
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (q: string) => ({
      get matches() {
        if (q === FOLD_TWO_PANE_QUERY || q.includes('viewport-segments')) return media.folded && media.width >= 640
        const max = /max-width:\s*(\d+)px/.exec(q)
        return max ? media.width <= Number(max[1]) : false
      },
      media: q,
      addEventListener: (_t: string, cb: () => void) => { listeners.add(cb) },
      removeEventListener: (_t: string, cb: () => void) => { listeners.delete(cb) },
      addListener: () => {}, removeListener: () => {},
    }),
  })
  useIsMobile = (await import('../hooks/useIsMobile')).useIsMobile
  useLayoutRouteSignal = (await import('../lib/touchLayoutForm')).useLayoutRouteSignal
  LAYOUT_ROUTE_CHANGED_EVENT = (await import('../lib/touchLayoutForm')).LAYOUT_ROUTE_CHANGED_EVENT
  const guide = await import('../guide/guideActions')
  subscribeGuideViewport = guide.subscribeGuideViewport
  currentGuideViewport = guide.currentGuideViewport
})

/** Route-signal events seen so far, and how many had fired by the end of each
 *  commit's layout phase (the last moment before the browser paints). */
const signal = { fired: 0, atLayoutEnd: [] as number[] }

function Probe({ path }: { path: string }) {
  useLayoutRouteSignal(path)
  const isMobile = useIsMobile()
  const guide = useSyncExternalStore(subscribeGuideViewport, currentGuideViewport)
  return <div data-testid="probe" data-mobile={String(isMobile)} data-guide={guide} />
}

/** Probe then a later sibling, as the shell is followed by the routed page. */
const Tree = ({ path }: { path: string }) => <><Probe path={path} /><LayoutEnd path={path} /></>

/** A later sibling: its layout effect runs after the route signal's, in the
 *  same layout phase, so it sees the signal only if that fires before paint. */
function LayoutEnd({ path }: { path: string }) {
  useLayoutEffect(() => { signal.atLayoutEnd.push(signal.fired) }, [path])
  return null
}

let root: Root | null = null
let host: HTMLElement | null = null
const actEnv = globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT?: boolean }
let savedActEnv: boolean | undefined

afterEach(() => {
  if (root) flushSync(() => root!.unmount())
  host?.remove()
  root = null
  host = null
  actEnv.IS_REACT_ACT_ENVIRONMENT = savedActEnv
  window.history.replaceState(null, '', '/')
})

/** Moves the URL and renders the new route in one synchronous commit, as the
 *  router does. */
function navigate(path: string) {
  window.history.pushState(null, '', path)
  flushSync(() => root!.render(<Tree path={path} />))
}

describe('a route change at a 669px fold', () => {
  it('paints /settings in the phone form, and the guide reads mobile, on the first paint after leaving /chat', () => {
    savedActEnv = actEnv.IS_REACT_ACT_ENVIRONMENT
    actEnv.IS_REACT_ACT_ENVIRONMENT = false
    window.history.replaceState(null, '', '/chat')
    host = document.createElement('div')
    document.body.appendChild(host)
    root = createRoot(host)
    flushSync(() => root!.render(<Tree path="/chat" />))
    const probe = () => host!.querySelector<HTMLElement>('[data-testid="probe"]')!
    expect(probe().dataset.mobile).toBe('false')
    expect(probe().dataset.guide).toBe('desktop')

    const onSignal = () => { signal.fired++ }
    window.addEventListener(LAYOUT_ROUTE_CHANGED_EVENT, onSignal)
    signal.fired = 0
    signal.atLayoutEnd = []
    navigate('/settings')
    window.removeEventListener(LAYOUT_ROUTE_CHANGED_EVENT, onSignal)
    // The re-read fired inside the route's own layout phase, before paint.
    expect(signal.atLayoutEnd[0]).toBe(1)
    expect(probe().dataset.mobile).toBe('true')
    expect(probe().dataset.guide).toBe('mobile')

    navigate('/chat')
    expect(probe().dataset.mobile).toBe('false')
    expect(probe().dataset.guide).toBe('desktop')
  })
})
