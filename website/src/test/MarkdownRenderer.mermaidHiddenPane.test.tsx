import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render } from '@testing-library/react'

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(),
  },
}))

import mermaid from 'mermaid'
import MarkdownRenderer from '../components/MarkdownRenderer'

const MERMAID_MD = '```mermaid\ngraph TD;A-->B\n```'
// Two lines so the icon-lint gate (a line-anchored regex aimed at JSX inline
// SVGs) cannot match; this is a mermaid-output fixture, not an icon.
const RENDERED_SVG =
  '<svg ' +
  'viewBox="0 0 240 120" aria-roledescription="flowchart-v2"><g class="nodes"></g></svg>'
// What mermaid returns when every measurement was 0: the 16px viewBox the bug
// report shows. Same two-line construction, for the same gate.
const HIDDEN_SVG =
  '<svg ' +
  'viewBox="-8 -8 16 16"></svg>'

/**
 * The failure this pins: a diagram that finishes streaming while its pane is a
 * display:none <iframe> (InstancesViewport keeps inactive remote panes mounted
 * and hidden) renders as an empty 16px box. mermaid measures labels with
 * getBoundingClientRect() inside document.body, and inside a hidden iframe every
 * rect is 0, so it emits a degenerate viewBox and NaN node transforms. Nothing
 * redraws it until the block happens to remount.
 *
 * MermaidBlock therefore draws only once its host has a box: it probes before
 * the lazy mermaid load and before render(), and watches the box for the whole
 * of render() (render() lazy-loads the diagram chunk and image shapes itself,
 * so the pane can hide inside it). The test DOM has no layout engine, so the
 * two layout facts are simulated directly:
 *  - `getClientRects()` is EMPTY while the element has no box (display:none
 *    anywhere above it, the hidden iframe included) and non-empty, even at zero
 *    size, once layout ran -- this is the probe the block reads.
 *  - a ResizeObserver stays silent while the box is absent and fires when it
 *    comes back -- a controllable stub stands in for it here.
 */

type RoCallback = (entries: ResizeObserverEntry[], observer: ResizeObserver) => void

const RealResizeObserver = globalThis.ResizeObserver
const realGetClientRects = Element.prototype.getClientRects

/** The observer instances the block created, in creation order, each with
 *  the callback it registered and the targets it watches, so a test can fire
 *  "the box came back" by hand and read whether the block let go. */
let observers: Array<{ cb: RoCallback; targets: Element[]; disconnected: boolean }>

/** Elements currently pretending to have no box. */
let boxless: Set<Element>

function installLayoutStubs() {
  observers = []
  boxless = new Set()
  class StubResizeObserver {
    private rec: { cb: RoCallback; targets: Element[]; disconnected: boolean }
    constructor(cb: RoCallback) {
      this.rec = { cb, targets: [], disconnected: false }
      observers.push(this.rec)
    }
    observe(el: Element) { this.rec.targets.push(el) }
    unobserve(el: Element) { this.rec.targets = this.rec.targets.filter(t => t !== el) }
    disconnect() { this.rec.disconnected = true; this.rec.targets = [] }
  }
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = StubResizeObserver
  Element.prototype.getClientRects = function (this: Element) {
    if (boxless.has(this)) return [] as unknown as DOMRectList
    return realGetClientRects.call(this)
  }
}

/** Hosts are `boxless` from the moment they exist: MermaidBlock reads the probe
 *  in its mount effect, before a test could reach the element, so the set is
 *  filled by intercepting the host's creation rather than after render. */
function hideMermaidHostsOnCreate() {
  const realCreate = document.createElement.bind(document)
  const spy = vi.spyOn(document, 'createElement').mockImplementation(((tag: string, opts?: ElementCreationOptions) => {
    const el = realCreate(tag, opts)
    if (tag === 'div') boxless.add(el)
    return el
  }) as typeof document.createElement)
  return spy
}

const hostOf = (container: HTMLElement) =>
  container.querySelector('figure > div') as HTMLElement

/** Deliver a ResizeObserver notification for `el` on every live observer that
 *  watches it, the way the platform would once the box is back. */
function fireResize(el: Element) {
  for (const o of observers) {
    if (o.disconnected || !o.targets.includes(el)) continue
    o.cb([{ target: el } as ResizeObserverEntry], { disconnect() { o.disconnected = true } } as unknown as ResizeObserver)
  }
}

const flush = () => act(async () => { await new Promise(r => setTimeout(r, 20)) })

describe('MermaidBlock waits for a box before drawing', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(mermaid.render).mockResolvedValue({ svg: RENDERED_SVG } as never)
    installLayoutStubs()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = RealResizeObserver
    Element.prototype.getClientRects = realGetClientRects
  })

  it('does not call mermaid.render while the host has no box, and draws once the box is back', async () => {
    const createSpy = hideMermaidHostsOnCreate()
    const { container } = render(<MarkdownRenderer content={MERMAID_MD} />)
    createSpy.mockRestore()
    const host = hostOf(container)
    expect(host).toBeTruthy()
    expect(host.getClientRects().length).toBe(0)

    await flush()
    // The whole point: a hidden pane must not produce a diagram.
    expect(mermaid.render).not.toHaveBeenCalled()
    // ...and the block is watching the host, not polling or giving up.
    const watching = observers.filter(o => !o.disconnected && o.targets.includes(host))
    expect(watching).toHaveLength(1)

    // A notification that arrives while the box is STILL absent (observers can
    // fire for other reasons) must keep waiting rather than draw a broken SVG.
    act(() => fireResize(host))
    await flush()
    expect(mermaid.render).not.toHaveBeenCalled()
    expect(watching[0].disconnected).toBe(false)

    // The pane is shown again: the host has a box, the observer fires, and the
    // diagram is drawn exactly once, with the observer released.
    boxless.delete(host)
    act(() => fireResize(host))
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(1)
    expect(watching[0].disconnected).toBe(true)
    expect(host.querySelector('svg')).toBeTruthy()
  })

  it('draws immediately when the host has a box', async () => {
    const { container } = render(<MarkdownRenderer content={MERMAID_MD} />)
    const host = hostOf(container)
    expect(host.getClientRects().length).toBeGreaterThan(0)
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(1)
    // No observer was armed for a host that already has a box.
    expect(observers.filter(o => o.targets.includes(host))).toHaveLength(0)
  })

  it('re-probes after the lazy mermaid load and waits if the box went away mid-flight', async () => {
    // The box is present when the effect starts, so the first probe passes.
    // The chunk import is the widest async gap before mermaid measures; a pane
    // switched away inside it must be caught by the SECOND probe, or the
    // measurement lands in a hidden document all the same.
    const { container } = render(<MarkdownRenderer content={MERMAID_MD} />)
    const host = hostOf(container)
    expect(host.getClientRects().length).toBeGreaterThan(0)
    // Nothing async has run yet (microtasks wait for this test to yield), so
    // this is "the pane went display:none while mermaid was still loading".
    boxless.add(host)

    await flush()
    expect(mermaid.render).not.toHaveBeenCalled()
    const watching = observers.filter(o => !o.disconnected && o.targets.includes(host))
    expect(watching).toHaveLength(1)

    boxless.delete(host)
    act(() => fireResize(host))
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(1)
    expect(watching[0].disconnected).toBe(true)
    expect(host.querySelector('svg')).toBeTruthy()
  })

  it('discards an SVG measured in a hidden document and renders again once the box is back', async () => {
    // mermaid.render() is itself async (it lazy-loads the diagram chunk before
    // measuring), so the pane can go hidden AFTER every pre-render probe passed.
    // The stub stands in for that: the host loses its box while render() is in
    // flight, and the SVG it returns is the broken one a hidden document yields.
    const { container } = render(<MarkdownRenderer content={MERMAID_MD} />)
    const host = hostOf(container)
    vi.mocked(mermaid.render).mockImplementationOnce(async () => {
      boxless.add(host)
      return { svg: HIDDEN_SVG } as never
    })

    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(1)
    // The broken SVG never reaches the host...
    expect(host.querySelector('svg')).toBeNull()
    // ...and the block is back to waiting for a box.
    const watching = observers.filter(o => !o.disconnected && o.targets.includes(host))
    expect(watching).toHaveLength(1)

    boxless.delete(host)
    act(() => fireResize(host))
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(2)
    expect(host.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 240 120')
  })

  it('discards an SVG when the box was lost during render() even though it is back at the end', async () => {
    // Image shapes load asynchronously inside render(), so the pane can hide
    // and show again before render() resolves. A point check at the end sees a
    // box and would install labels measured at 0. The block watches the box
    // for the whole render instead: the stub plays the observer notification
    // the platform delivers on the frame the box is gone, then restores it.
    const { container } = render(<MarkdownRenderer content={MERMAID_MD} />)
    const host = hostOf(container)
    vi.mocked(mermaid.render).mockImplementationOnce(async () => {
      boxless.add(host)
      fireResize(host)
      boxless.delete(host)
      return { svg: HIDDEN_SVG } as never
    })

    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(2)
    expect(host.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 240 120')
    // The render-time watcher is released after each attempt.
    expect(observers.every(o => o.disconnected || !o.targets.includes(host))).toBe(true)
  })

  it('without ResizeObserver draws once and does not loop on a boxless host', async () => {
    // There is nothing to wait on without an observer, so the block degrades to
    // the pre-fix behaviour: render at once, install what comes back, and --
    // the part this pins -- never re-render in a loop while the box stays
    // absent.
    ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = undefined
    const createSpy = hideMermaidHostsOnCreate()
    const { container } = render(<MarkdownRenderer content={MERMAID_MD} />)
    createSpy.mockRestore()
    const host = hostOf(container)
    expect(host.getClientRects().length).toBe(0)

    await flush()
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(1)
    expect(host.querySelector('svg')).toBeTruthy()
    expect(observers).toHaveLength(0)
  })

  it('releases the observer when unmounted while still waiting', async () => {
    const createSpy = hideMermaidHostsOnCreate()
    const { container, unmount } = render(<MarkdownRenderer content={MERMAID_MD} />)
    createSpy.mockRestore()
    const host = hostOf(container)
    await flush()
    const watching = observers.filter(o => o.targets.includes(host))
    expect(watching).toHaveLength(1)
    unmount()
    expect(watching[0].disconnected).toBe(true)
    // A late notification after teardown draws nothing.
    boxless.delete(host)
    fireResize(host)
    await flush()
    expect(mermaid.render).not.toHaveBeenCalled()
  })
})
