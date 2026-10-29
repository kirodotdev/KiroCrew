/** Executes the REAL frame wheel relay -- extracted from buildSrcdoc's own
 * output, not a copy -- against a live document, and pins the contract:
 *
 *   A frame built with `relayWheel` posts a horizontal wheel run its own content
 *   does not own as `{type: 'mc-frame-wheel', dx, dy, mode}`, and cancels the
 *   run's rightward-finger events (`deltaX < 0`, the host's close direction) so
 *   the browser's history swipe does not start under it. A run that starts
 *   vertical, a pinch, or a run over a sideways scroller posts nothing. A frame
 *   built without the option carries no relay.
 */
import { describe, it, expect, beforeAll, afterEach } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'
import { hasScrollRoomX, SWIPE_GESTURE_IDLE_MS } from '../lib/trackpadGesture'

const MARK = 'mc-frame-wheel'

function relayScript(out: string): string | undefined {
  const doc = new DOMParser().parseFromString(out, 'text/html')
  return Array.from(doc.querySelectorAll('script'))
    .map((s) => s.textContent ?? '')
    .find((t) => t.includes(MARK))
}

/** The events' clock; `host()` steps it past the relay's run gap. */
let clock = 0

const posted: Array<{ type: string; dx: number; dy: number; mode: number; t?: number }> = []
let hellos = 0
/** The frame's `parent`: records what the relay posts. */
const parentStub = {
  postMessage: (msg: { type: string; dx: number; dy: number; mode: number; t?: number }) => {
    if (msg.type === 'mc-frame-wheel-hello') hellos++
    else posted.push(msg)
  },
}
/** A host's arm/disarm, sent from `source`. */
function armFrom(source: unknown, on: boolean) {
  const ev = new MessageEvent('message', { data: { type: 'mc-frame-wheel-armed', on } })
  Object.defineProperty(ev, 'source', { value: source })
  window.dispatchEvent(ev)
}

/** A run of wheel events; returns how many the relay cancelled. */
function wheel(target: Element, dx: number, dy: number, n: number, init: WheelEventInit = {}) {
  let cancelled = 0
  for (let i = 0; i < n; i++) {
    const ev = new WheelEvent('wheel', { deltaX: dx, deltaY: dy, bubbles: true, cancelable: true, ...init })
    // The test DOM drops modifier keys from a WheelEvent's init.
    if (init.ctrlKey) Object.defineProperty(ev, 'ctrlKey', { value: true })
    // The relay reads the run's timing from the event: 16ms apart within a run.
    clock += 16
    Object.defineProperty(ev, 'timeStamp', { value: clock })
    target.dispatchEvent(ev)
    if (ev.defaultPrevented) cancelled++
  }
  return cancelled
}

describe('frame wheel relay', () => {
  beforeAll(() => {
    const script = relayScript(buildSrcdoc({ html: '<p>probe</p>', themeVars: {}, mode: 'dark', relayWheel: true }))
    if (!script) throw new Error('wheel relay not found in srcdoc')
    new Function('parent', script)(parentStub)
    // The relay waits for its host: arm it, as a listening host answers the hello.
    expect(hellos).toBe(1)
    armFrom(parentStub, true)
  })
  afterEach(() => {
    posted.length = 0
    document.querySelectorAll('[data-relay-host]').forEach((n) => n.remove())
  })

  /** A fresh element, after a gap long enough to end the previous run. */
  const host = () => {
    clock += 1000
    const el = document.createElement('div')
    el.setAttribute('data-relay-host', '')
    document.body.appendChild(el)
    return el
  }

  it('is injected only when the host asks for it, ahead of the page body', () => {
    expect(relayScript(buildSrcdoc({ html: '<p>x</p>', themeVars: {}, mode: 'dark' }))).toBeUndefined()
    const out = buildSrcdoc({ html: '<p id="llm">x</p>', themeVars: {}, mode: 'dark', relayWheel: true })
    expect(out.indexOf(MARK)).toBeGreaterThan(-1)
    expect(out.indexOf(MARK)).toBeLessThan(out.indexOf('id="llm"'))
  })

  it('posts a horizontal run and cancels only its close-direction events', () => {
    const el = host()
    const from = clock
    expect(wheel(el, -12, 1, 3)).toBe(3)
    // `t` is when each wheel happened, on the shared epoch clock, so the host
    // reads the fingers' own timing however late the messages arrive.
    expect(posted).toEqual([1, 2, 3].map((i) => ({ type: MARK, dx: -12, dy: 1, mode: 0, t: performance.timeOrigin + from + i * 16 })))
    posted.length = 0
    const other = host()
    expect(wheel(other, 12, 0, 2)).toBe(0)
    expect(posted).toHaveLength(2)
  })

  it('leaves every wheel alone until the host arms it, and only the parent can arm it', () => {
    armFrom(parentStub, false)
    expect(wheel(host(), -20, 0, 3)).toBe(0)
    // A stranger window posting the same message does not arm the relay.
    armFrom({}, true)
    expect(wheel(host(), -20, 0, 3)).toBe(0)
    expect(posted).toEqual([])
    armFrom(parentStub, true)
    expect(wheel(host(), -20, 0, 3)).toBe(3)
    expect(posted).toHaveLength(3)
  })

  it('stays silent for a run over a scroller inside a shadow root, seen through the composed path', () => {
    const el = host()
    const shadowHost = document.createElement('div')
    el.appendChild(shadowHost)
    const root = shadowHost.attachShadow({ mode: 'open' })
    const scroller = document.createElement('div')
    scroller.style.overflowX = 'auto'
    Object.defineProperty(scroller, 'scrollWidth', { value: 800 })
    Object.defineProperty(scroller, 'clientWidth', { value: 300 })
    Object.defineProperty(scroller, 'scrollLeft', { value: 100 })
    const cell = document.createElement('span')
    scroller.appendChild(cell)
    root.appendChild(scroller)
    let cancelled = 0
    for (let i = 0; i < 3; i++) {
      const ev = new WheelEvent('wheel', { deltaX: -20, deltaY: 0, bubbles: true, cancelable: true, composed: true })
      clock += 16
      Object.defineProperty(ev, 'timeStamp', { value: clock })
      // A window listener sees the target retargeted to the shadow host, as in a
      // browser; this test DOM does not retarget, so it is done here.
      Object.defineProperty(ev, 'target', { get: () => shadowHost })
      cell.dispatchEvent(ev)
      if (ev.defaultPrevented) cancelled++
    }
    expect(cancelled).toBe(0)
    expect(posted).toEqual([])
  })

  it("ends a run after the host's own gap: a swipe soon after a vertical scroll is posted", () => {
    const el = host()
    wheel(el, 0, 30, 3)
    expect(posted).toHaveLength(0)
    // 120ms after the scroll's last event: past the host's gap, so the host
    // reads a new run. The frame must too, or the swipe never reaches it.
    expect(120).toBeGreaterThan(SWIPE_GESTURE_IDLE_MS)
    clock += 120 - 16
    wheel(el, -20, 0, 3)
    expect(posted.map((m) => m.dx)).toEqual([-20, -20, -20])
  })

  /** A right-to-left scroller with 500px to scroll, at `scrollLeft`; returns a
   *  cell inside it. Right to left, `scrollLeft` runs over [-500, 0]. */
  const rtlScroller = (scrollLeft: number) => {
    const scroller = document.createElement('div')
    scroller.style.overflowX = 'auto'
    scroller.style.direction = 'rtl'
    Object.defineProperty(scroller, 'scrollWidth', { value: 800 })
    Object.defineProperty(scroller, 'clientWidth', { value: 300 })
    Object.defineProperty(scroller, 'scrollLeft', { value: scrollLeft })
    const cell = document.createElement('span')
    scroller.appendChild(cell)
    host().appendChild(scroller)
    return cell
  }

  it('reads a right-to-left scroller at its start (scrollLeft 0): a negative deltaX is its own scroll', () => {
    expect(wheel(rtlScroller(0), -20, 0, 3)).toBe(0)
    expect(posted).toEqual([])
  })

  it('reads a right-to-left scroller at its far end (scrollLeft -max): it has no room, so the run is relayed', () => {
    expect(wheel(rtlScroller(-500), -20, 0, 3)).toBe(3)
    expect(posted).toHaveLength(3)
    posted.length = 0
    // Back toward its start it still scrolls itself.
    expect(wheel(rtlScroller(-500), 20, 0, 3)).toBe(0)
    expect(posted).toEqual([])
  })

  it("the frame's scroll-room rule is the host's: same answer on every case", () => {
    const script = relayScript(buildSrcdoc({ html: '<p>probe</p>', themeVars: {}, mode: 'dark', relayWheel: true }))!
    const src = /function room\([^)]*\)\{[\s\S]*?\n {2}\}/.exec(script)?.[0]
    expect(src).toBeDefined()
    const room = new Function(`${src}; return room`)() as typeof hasScrollRoomX
    for (const rtl of [false, true]) {
      for (const max of [0, 1, 500]) {
        for (const scrollLeft of [-500, -499.5, -250, -0.5, 0, 0.5, 250, 498.5, 499, 500]) {
          for (const dx of [-20, 20]) {
            expect(room(scrollLeft, max, rtl, dx), `rtl=${rtl} max=${max} scrollLeft=${scrollLeft} dx=${dx}`).toBe(hasScrollRoomX(scrollLeft, max, rtl, dx))
          }
        }
      }
    }
  })

  it('stays silent for a vertical run, a pinch, and a run over a sideways scroller', () => {
    expect(wheel(host(), 2, 30, 3)).toBe(0)
    expect(wheel(host(), -20, 0, 3, { ctrlKey: true })).toBe(0)
    const el = host()
    const scroller = document.createElement('div')
    scroller.style.overflowX = 'auto'
    Object.defineProperty(scroller, 'scrollWidth', { value: 800 })
    Object.defineProperty(scroller, 'clientWidth', { value: 300 })
    Object.defineProperty(scroller, 'scrollLeft', { value: 100 })
    const cell = document.createElement('span')
    scroller.appendChild(cell)
    el.appendChild(scroller)
    expect(wheel(cell, -20, 0, 3)).toBe(0)
    expect(posted).toEqual([])
  })
})
