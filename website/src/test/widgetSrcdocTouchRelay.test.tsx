/** Executes the REAL frame touch relay -- extracted from buildSrcdoc's own
 * output, not a copy -- against a live document, and pins the contract:
 *
 *   A frame built with `relayTouches` reports each single-finger touch to its
 *   parent as `{type: 'mc-frame-touch', phase, x, y}`, so a host drawer can
 *   follow a drag that began over the frame. It stays silent for a touch the
 *   frame's own content owns (a horizontal scroller, an editable) and cancels on
 *   a second finger. A frame built without the option carries no relay.
 */
import { describe, it, expect, beforeAll, afterEach } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'

const MARK = 'mc-frame-touch'

function relayScript(out: string): string | undefined {
  const doc = new DOMParser().parseFromString(out, 'text/html')
  return Array.from(doc.querySelectorAll('script'))
    .map((s) => s.textContent ?? '')
    .find((t) => t.includes(MARK))
}

const posted: Array<{ type: string; phase: string; x: number; y: number }> = []

/** A touch event carrying the given live and changed touch points. */
function touch(type: string, target: Element, points: Array<[number, number]>, changed = points) {
  const ev = new Event(type, { bubbles: true })
  const toTouch = ([x, y]: [number, number]) => ({ clientX: x, clientY: y })
  Object.defineProperty(ev, 'touches', { value: points.map(toTouch) })
  Object.defineProperty(ev, 'changedTouches', { value: changed.map(toTouch) })
  target.dispatchEvent(ev)
}

describe('frame touch relay', () => {
  beforeAll(() => {
    const script = relayScript(buildSrcdoc({ html: '<p>probe</p>', themeVars: {}, mode: 'dark', relayTouches: true }))
    if (!script) throw new Error('touch relay not found in srcdoc')
    // `parent` is the relay's only outward channel; stand it in with a recorder.
    // Installed once: its listeners live on this test window for the whole file.
    new Function('parent', script)({ postMessage: (msg: (typeof posted)[number]) => posted.push(msg) })
  })
  afterEach(() => {
    posted.length = 0
    document.querySelectorAll('[data-relay-host]').forEach((n) => n.remove())
  })

  const host = () => {
    const el = document.createElement('div')
    el.setAttribute('data-relay-host', '')
    document.body.appendChild(el)
    return el
  }

  it('is injected only when the host asks for it, ahead of the page body', () => {
    expect(relayScript(buildSrcdoc({ html: '<p>x</p>', themeVars: {}, mode: 'dark' }))).toBeUndefined()
    const out = buildSrcdoc({ html: '<p id="llm">x</p>', themeVars: {}, mode: 'dark', relayTouches: true })
    expect(out.indexOf(MARK)).toBeGreaterThan(-1)
    expect(out.indexOf(MARK)).toBeLessThan(out.indexOf('id="llm"'))
  })

  it('reports a one-finger drag as start, move and end in frame coordinates', () => {
    const el = host()
    touch('touchstart', el, [[40, 300]])
    touch('touchmove', el, [[200, 302]])
    touch('touchend', el, [], [[200, 302]])
    expect(posted).toEqual([
      { type: MARK, phase: 'start', x: 40, y: 300 },
      { type: MARK, phase: 'move', x: 200, y: 302 },
      { type: MARK, phase: 'end', x: 200, y: 302 },
    ])
  })

  it('stays silent for a touch inside a horizontal scroller or an editable', () => {
    const el = host()
    const scroller = document.createElement('div')
    scroller.style.overflowX = 'auto'
    Object.defineProperty(scroller, 'scrollWidth', { value: 800 })
    Object.defineProperty(scroller, 'clientWidth', { value: 300 })
    const cell = document.createElement('span')
    scroller.appendChild(cell)
    const input = document.createElement('input')
    el.append(scroller, input)
    for (const target of [cell, input]) {
      touch('touchstart', target, [[40, 300]])
      touch('touchmove', target, [[200, 300]])
      touch('touchend', target, [], [[200, 300]])
    }
    expect(posted).toEqual([])
  })

  it('cancels the drag when a second finger lands', () => {
    const el = host()
    touch('touchstart', el, [[40, 300]])
    touch('touchstart', el, [[40, 300], [90, 310]])
    touch('touchmove', el, [[200, 300], [250, 310]])
    expect(posted.map((m) => m.phase)).toEqual(['start', 'cancel'])
  })
})
