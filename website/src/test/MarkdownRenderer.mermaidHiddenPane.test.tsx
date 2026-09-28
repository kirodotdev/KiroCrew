import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render } from '@testing-library/react'

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(),
  },
}))

import mermaid from 'mermaid'
import MarkdownRenderer, { MERMAID_FONTS_READY_CAP_MS } from '../components/MarkdownRenderer'

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
 *
 * The second describe pins the OTHER precondition of a trustworthy measurement:
 * the web fonts. See `installFakeFonts` below.
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

// What mermaid returns when it measured in the FALLBACK face: a narrower
// viewBox than the loaded face needs. Same two-line construction as above.
const NARROW_SVG =
  '<svg ' +
  'viewBox="0 0 200 120" aria-roledescription="flowchart-v2"><g class="nodes"></g></svg>'

type FontsEvent = 'loading' | 'loadingdone' | 'loadingerror'

/** Mirrors `MERMAID_FONTS_READY_CAP_MS`, and is pinned equal to it in the case
 *  that advances the clock by it, so the two cannot drift apart silently. */
const FONTS_READY_CAP_MS = 2500

/** A controllable stand-in for `document.fonts`, which happy-dom does not
 *  implement (every case above therefore ran with it ABSENT and pins that the
 *  block draws at once when there is nothing to wait for). `ready` is a fresh
 *  pending promise for as long as a load is in flight and a settled one
 *  otherwise, and the three status events fire on the listeners the block
 *  registers -- the parts of the FontFaceSet contract the block reads. */
function installFakeFonts(initial: 'loading' | 'loaded') {
  const listeners: Record<FontsEvent, Set<() => void>> = {
    loading: new Set(), loadingdone: new Set(), loadingerror: new Set(),
  }
  let resolveReady: () => void = () => {}
  const fire = (type: FontsEvent) => { for (const cb of Array.from(listeners[type])) cb() }
  const fake = {
    status: 'loaded' as 'loading' | 'loaded',
    ready: Promise.resolve(),
    addEventListener(type: FontsEvent, cb: () => void) { listeners[type].add(cb) },
    removeEventListener(type: FontsEvent, cb: () => void) { listeners[type].delete(cb) },
    /** A face begins to load: `ready` goes pending, as the platform's does. */
    startLoading() {
      fake.status = 'loading'
      fake.ready = new Promise<void>(resolve => { resolveReady = resolve })
      fire('loading')
    },
    /** The pending load lands: `ready` settles and `loadingdone` fires. */
    finishLoading() {
      fake.status = 'loaded'
      resolveReady()
      fire('loadingdone')
    },
    listenerCount(type: FontsEvent) { return listeners[type].size },
  }
  if (initial === 'loading') fake.startLoading()
  Object.defineProperty(document, 'fonts', { configurable: true, value: fake })
  return fake
}

describe('MermaidBlock waits for the web fonts before measuring', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(mermaid.render).mockResolvedValue({ svg: RENDERED_SVG } as never)
    installLayoutStubs()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = RealResizeObserver
    Element.prototype.getClientRects = realGetClientRects
    // Back to the environment's own shape: no `document.fonts` at all.
    Reflect.deleteProperty(document, 'fonts')
  })

  it('does not call mermaid.render while a web font is still loading, and draws once the fonts are ready', async () => {
    // The failure this pins (#12480): the body face is swap-loaded, so a
    // diagram drawn while it is still in flight has every label MEASURED in the
    // fallback face and then PAINTED in the loaded one -- boxes sized for the
    // narrower glyphs, text clipped at the right edge. The block must hold the
    // measurement until `document.fonts.ready` settles.
    const fonts = installFakeFonts('loading')
    const { container } = render(<MarkdownRenderer content={MERMAID_MD} />)
    const host = hostOf(container)
    expect(host.getClientRects().length).toBeGreaterThan(0)

    await flush()
    expect(mermaid.render).not.toHaveBeenCalled()
    expect(host.querySelector('svg')).toBeNull()

    act(() => fonts.finishLoading())
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(1)
    expect(host.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 240 120')
    // The watch outlives the draw: a face can still be declared late.
    expect(fonts.listenerCount('loadingdone')).toBe(1)
  })

  it('draws in the fallback face once the wait on `ready` reaches the cap, and redraws once when the face lands late', async () => {
    // A font file whose packets are DROPPED (not refused: a refusal fails fast
    // and settles `ready`) keeps its FontFace pending for the browser's network
    // timeout, tens of seconds or more. Uncapped, every diagram on that cold
    // load shows nothing for the whole window, where drawing at once showed
    // clipped but readable labels. So the wait is capped: past it the diagram is
    // drawn in the fallback face, and the late `loadingdone` buys the one
    // redraw that the stylesheet-late case above already proves yields the
    // correct final frame. The fake's `ready` never settles on its own here.
    vi.useFakeTimers()
    try {
      const fonts = installFakeFonts('loading')
      const { container } = render(<MarkdownRenderer content={MERMAID_MD} />)
      const host = hostOf(container)
      vi.mocked(mermaid.render).mockResolvedValueOnce({ svg: NARROW_SVG } as never)

      // Just short of the cap the measurement is still held.
      await act(async () => { await vi.advanceTimersByTimeAsync(FONTS_READY_CAP_MS - 1) })
      expect(mermaid.render).not.toHaveBeenCalled()
      expect(host.querySelector('svg')).toBeNull()

      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(
        mermaid.render,
        `a font load that never settles must not hold the diagram past the ${FONTS_READY_CAP_MS} ms cap: mermaid.render was still not called`,
      ).toHaveBeenCalledTimes(1)
      expect(host.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 200 120')
      // The load left pending is the one the gate gave up on: it buys no
      // immediate second attempt, and the watch stays armed for its arrival.
      expect(fonts.listenerCount('loadingdone')).toBe(1)
      expect(MERMAID_FONTS_READY_CAP_MS).toBe(FONTS_READY_CAP_MS)

      // The face lands, late: one redraw in the loaded face, and the watch is spent.
      await act(async () => { fonts.finishLoading(); await vi.advanceTimersByTimeAsync(0) })
      expect(mermaid.render).toHaveBeenCalledTimes(2)
      expect(host.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 240 120')
      expect(fonts.listenerCount('loadingdone')).toBe(0)

      // Neither another face nor another cap's worth of waiting buys a third draw.
      await act(async () => {
        fonts.startLoading()
        fonts.finishLoading()
        await vi.advanceTimersByTimeAsync(FONTS_READY_CAP_MS * 2)
      })
      expect(mermaid.render).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
      // A failure here leaves the NARROW once-value unconsumed, and
      // `vi.clearAllMocks()` keeps once-queues; drop it so the case fails alone.
      vi.mocked(mermaid.render).mockReset()
    }
  })

  it('redraws once when a face lands after the diagram was drawn, then lets the watch go', async () => {
    // The swap-loaded stylesheet is itself the late arrival: the dashboard is
    // served from a local gateway, the font origin is the slow one. When the
    // diagram renders no face is declared, so `ready` is settled and nothing is
    // pending -- the measurement is honest for the fallback face -- and the
    // stylesheet then lands, the face loads, and `swap` repaints the labels in
    // a face the boxes were never measured in. The first `loadingdone` after
    // the draw is the signal, and it buys exactly one redraw.
    const fonts = installFakeFonts('loaded')
    const { container } = render(<MarkdownRenderer content={MERMAID_MD} />)
    const host = hostOf(container)
    vi.mocked(mermaid.render).mockResolvedValueOnce({ svg: NARROW_SVG } as never)

    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(1)
    expect(host.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 200 120')

    act(() => { fonts.startLoading(); fonts.finishLoading() })
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(2)
    expect(host.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 240 120')
    expect(fonts.listenerCount('loadingdone')).toBe(0)

    // A second late face is not a second redraw.
    act(() => { fonts.startLoading(); fonts.finishLoading() })
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(2)
  })

  it('releases the font watch when unmounted after a draw, and a late face redraws nothing', async () => {
    const fonts = installFakeFonts('loaded')
    const { unmount } = render(<MarkdownRenderer content={MERMAID_MD} />)
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(1)
    expect(fonts.listenerCount('loadingdone')).toBe(1)
    unmount()
    expect(fonts.listenerCount('loadingdone')).toBe(0)
    act(() => { fonts.startLoading(); fonts.finishLoading() })
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(1)
  })

  it('discards an SVG measured while a face landed mid-render and draws once more', async () => {
    // `ready` settles when no load is PENDING, and a face whose unicode-range is
    // first exercised by the diagram's own glyphs starts loading only once
    // mermaid lays the label out -- inside render(). Its arrival mid-render
    // means the measurement may predate it, so that SVG is discarded and the
    // diagram drawn again, exactly as a box lost mid-render is handled.
    const fonts = installFakeFonts('loaded')
    const { container } = render(<MarkdownRenderer content={MERMAID_MD} />)
    const host = hostOf(container)
    vi.mocked(mermaid.render).mockImplementationOnce(async () => {
      fonts.startLoading()
      fonts.finishLoading()
      return { svg: NARROW_SVG } as never
    })

    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(2)
    expect(host.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 240 120')
    // The mid-render listener is released after each attempt.
    expect(fonts.listenerCount('loadingdone')).toBe(0)
  })

  it('waits for a load still pending when render finishes, draws once more, and never a third time', async () => {
    const fonts = installFakeFonts('loaded')
    const { container } = render(<MarkdownRenderer content={MERMAID_MD} />)
    const host = hostOf(container)
    vi.mocked(mermaid.render)
      // First attempt: a load STARTS during render and is still in flight when
      // render() resolves, so the SVG it returns was measured too early.
      .mockImplementationOnce(async () => {
        fonts.startLoading()
        return { svg: NARROW_SVG } as never
      })
      // Second attempt: yet another load starts. The result must STAND -- the
      // fonts re-render is bounded to one, or a face that keeps loading (or a
      // set that never settles) would redraw the diagram forever.
      .mockImplementationOnce(async () => {
        fonts.startLoading()
        return { svg: RENDERED_SVG } as never
      })

    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(1)
    // Nothing is installed while the second attempt waits on `ready`...
    expect(host.querySelector('svg')).toBeNull()

    act(() => fonts.finishLoading())
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(2)
    expect(host.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 240 120')

    // ...and the load the second attempt left pending does not buy a third.
    act(() => fonts.finishLoading())
    await flush()
    await flush()
    expect(mermaid.render).toHaveBeenCalledTimes(2)
    expect(fonts.listenerCount('loadingdone')).toBe(0)
  })

  it('draws nothing when unmounted while waiting for the fonts', async () => {
    const fonts = installFakeFonts('loading')
    const { unmount } = render(<MarkdownRenderer content={MERMAID_MD} />)
    await flush()
    expect(mermaid.render).not.toHaveBeenCalled()
    unmount()
    act(() => fonts.finishLoading())
    await flush()
    expect(mermaid.render).not.toHaveBeenCalled()
  })
})
