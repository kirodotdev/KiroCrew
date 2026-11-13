/** Executes the REAL fragment-scroll script -- extracted from buildSrcdoc's own
 * output, not a copy -- against the test document, and pins the contract:
 *
 *   A frame sized to its own document cannot scroll to an in-page link target,
 *   so a plain click on a same-document `#id` link (its own target, `_self`, or
 *   no target under `<base target="_blank">`) posts
 *   `{type: 'mc-widget-scroll-to', top}` with the target's offset in the frame,
 *   for the host to scroll to. A link that opens a window, points at another
 *   document, or that the page cancels posts nothing.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'
import { FRAGMENT_SCROLL_MESSAGE } from '../lib/widgetFragmentScroll'

function extractScript(): string {
  const out = buildSrcdoc({ html: '<p>probe</p>', themeVars: {}, mode: 'dark', includeHeightReporter: true })
  const doc = new DOMParser().parseFromString(out, 'text/html')
  const script = Array.from(doc.querySelectorAll('script'))
    .map((s) => s.textContent ?? '')
    .find((t) => t.includes(FRAGMENT_SCROLL_MESSAGE))
  if (!script) throw new Error('fragment-scroll script not found in srcdoc')
  return script
}

const removers: Array<() => void> = []

/** Run the script as a framed document would: `window` is a stand-in whose
 * listeners land on the real test window, and `parent` records what was posted.
 * `topLevel` makes `window.parent === window`. */
function install(topLevel = false): unknown[] {
  const posted: unknown[] = []
  const parentStub = { postMessage: (data: unknown) => { posted.push(data) } }
  const win: { parent: unknown; addEventListener: typeof window.addEventListener } = {
    parent: parentStub,
    addEventListener: ((type: string, fn: EventListener, opts?: boolean | AddEventListenerOptions) => {
      window.addEventListener(type, fn, opts)
      removers.push(() => window.removeEventListener(type, fn, opts))
    }) as typeof window.addEventListener,
  }
  if (topLevel) win.parent = win
  new Function('window', 'parent', extractScript())(win, parentStub)
  return posted
}

function mount(html: string): HTMLElement {
  const host = document.createElement('div')
  host.setAttribute('data-fragment-host', '')
  const parsed = new DOMParser().parseFromString(html, 'text/html')
  Array.from(parsed.body.childNodes).forEach((n) => host.appendChild(document.importNode(n, true)))
  document.body.appendChild(host)
  return host
}

/** Pin where `id` sits in the frame's viewport; jsdom lays nothing out. */
function placeAt(id: string, top: number): void {
  const el = document.getElementById(id)
  if (!el) throw new Error(`no #${id}`)
  el.getBoundingClientRect = () => ({ top, left: 0, right: 0, bottom: top, width: 0, height: 0, x: 0, y: top, toJSON: () => ({}) })
}

/** Click `selector`. The test document must not navigate, so a bubbling listener
 * added AFTER the script's capture listener would hide the case under test;
 * instead the default is left alone and only the posted messages are read. */
function click(host: HTMLElement, selector: string, init: MouseEventInit = {}): void {
  const el = host.querySelector<HTMLElement>(selector)
  if (!el) throw new Error(`nothing matches ${selector}`)
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, ...init }))
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
})

afterEach(() => {
  vi.useRealTimers()
  removers.splice(0).forEach((r) => r())
  document.querySelectorAll('[data-fragment-host], base').forEach((n) => n.remove())
  history.replaceState(null, '', location.pathname)
})

describe('fragment-scroll script', () => {
  it('ships only with the height reporter, the mark of a self-sizing frame', () => {
    const plain = buildSrcdoc({ html: '<p>x</p>', themeVars: {}, mode: 'dark' })
    expect(plain).not.toContain(FRAGMENT_SCROLL_MESSAGE)
    expect(extractScript()).toContain(FRAGMENT_SCROLL_MESSAGE)
  })

  it('posts the target offset after a click on a #id link', () => {
    const posted = install()
    const host = mount('<a id="go" href="#far">Jump</a><h2 id="far">Far</h2>')
    placeAt('far', 1840)
    click(host, '#go')
    vi.runOnlyPendingTimers()
    expect(posted).toContainEqual({ type: FRAGMENT_SCROLL_MESSAGE, top: 1840 })
  })

  it('handles target="_self", and a link with no target under <base target="_blank">', () => {
    const base = document.createElement('base')
    base.setAttribute('target', '_blank')
    document.head.appendChild(base)
    const posted = install()
    const host = mount('<a id="self" href="#far" target="_self">A</a><a id="bare" href="#far">B</a><h2 id="far">Far</h2>')
    placeAt('far', 900)
    click(host, '#self')
    vi.runOnlyPendingTimers()
    expect(posted).toHaveLength(1)
    click(host, '#bare')
    // Set during dispatch, so the browser navigates this frame rather than
    // asking for a window the sandbox refuses.
    expect(host.querySelector('#bare')?.getAttribute('target')).toBe('_self')
    vi.runOnlyPendingTimers()
    expect(posted).toEqual([
      { type: FRAGMENT_SCROLL_MESSAGE, top: 900 },
      { type: FRAGMENT_SCROLL_MESSAGE, top: 900 },
    ])
  })

  it('treats #top as the top of the document', () => {
    const posted = install()
    const host = mount('<a id="up" href="#top">Top</a>')
    document.documentElement.getBoundingClientRect = () => ({ top: -50, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: -50, toJSON: () => ({}) })
    click(host, '#up')
    vi.runOnlyPendingTimers()
    expect(posted).toContainEqual({ type: FRAGMENT_SCROLL_MESSAGE, top: -50 })
    delete (document.documentElement as { getBoundingClientRect?: unknown }).getBoundingClientRect
  })

  it('leaves a window-opening link, another document and modified clicks alone', () => {
    const posted = install()
    const host = mount(
      '<a id="blank" href="#far" target="_blank">A</a>' +
      '<a id="other" href="https://other.example.test/doc#far">B</a>' +
      '<a id="mod" href="#far">C</a><h2 id="far">Far</h2>',
    )
    placeAt('far', 700)
    for (const [sel, init] of [['#blank', {}], ['#other', {}], ['#mod', { ctrlKey: true }]] as const) {
      host.querySelector(sel)?.addEventListener('click', (e) => e.preventDefault())
      click(host, sel, init)
    }
    expect(host.querySelector('#blank')?.getAttribute('target')).toBe('_blank')
    expect(vi.getTimerCount()).toBe(0)
    expect(posted).toEqual([])
  })

  it('posts nothing for a click the page cancels (in-page tabs on #links)', () => {
    const posted = install()
    const host = mount('<a id="tab" href="#far">Tab</a><h2 id="far">Far</h2>')
    placeAt('far', 300)
    host.querySelector('#tab')?.addEventListener('click', (e) => e.preventDefault())
    click(host, '#tab')
    vi.runOnlyPendingTimers()
    expect(posted).toEqual([])
  })

  it('reports a hash set from script', () => {
    const posted = install()
    mount('<h2 id="far">Far</h2>')
    placeAt('far', 1200)
    history.replaceState(null, '', '#far')
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    expect(posted).toEqual([{ type: FRAGMENT_SCROLL_MESSAGE, top: 1200 }])
  })

  it('does nothing in a document with no parent', () => {
    const posted = install(true)
    const host = mount('<a id="go" href="#far">Jump</a><h2 id="far">Far</h2>')
    placeAt('far', 100)
    click(host, '#go')
    vi.runOnlyPendingTimers()
    expect(posted).toEqual([])
  })
})
