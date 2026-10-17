import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'

// Executes the REAL height-reporter script — extracted from buildSrcdoc's own
// output, not a copy — inside a stub document, and pins the one cross-boundary
// contract the parent surfaces depend on:
//
//   every window `load` event is followed by a height report, even when the
//   height has not changed since the last post.
//
// Why this matters: ArtifactBody re-arms its DOC_REPORT_GRACE_MS silence window
// on EVERY iframe `load` and discards any report received before it (an engine
// renavigation onto a spent single-use url fires `load` with no reporter behind
// it — post-load silence is the only signal). A document whose layout settles
// before its images finish posts its first measurement BEFORE `load`; if the
// reporter's deadband then swallowed the load-time re-check (same height, no
// post), that healthy, rendering document would be flagged "no longer showing"
// three seconds after every open — the false positive this test locks out.

type Listener = (ev?: unknown) => void

function extractReporterScript(): string {
  const out = buildSrcdoc({
    html: '<p>probe</p>',
    themeVars: {},
    mode: 'dark',
    includeHeightReporter: true,
  })
  const doc = new DOMParser().parseFromString(out, 'text/html')
  const script = Array.from(doc.querySelectorAll('script'))
    .map((s) => s.textContent ?? '')
    .find((t) => t.includes('mc-widget-height'))
  if (!script) throw new Error('height reporter script not found in srcdoc')
  return script
}

/** The layout a test hands the reporter: body's box (offset from the document
 * top, fractional height, bottom margin), body's integer scrollHeight, and the
 * root element's integer clientHeight (the frame's viewport). */
interface FakeLayout {
  top: () => number
  rectHeight: () => number
  scrollHeight: () => number
  marginBottom: () => string
  clientHeight: () => number
  /** The root element's inline style, so a test can read and log overflow-y. */
  rootStyle: { overflowY: string }
  onPost: (height: number) => void
}

function runReporter(height: () => number, overrides: Partial<FakeLayout> = {}) {
  const layout: FakeLayout = {
    top: () => 0,
    rectHeight: height,
    scrollHeight: () => Math.round(height()),
    marginBottom: () => '0px',
    // A viewport far taller than any test document unless a test sizes it.
    clientHeight: () => 100_000,
    rootStyle: { overflowY: '' },
    onPost: () => {},
    ...overrides,
  }
  const posts: Array<{ type: string; height: number }> = []
  const rafQueue: Listener[] = []
  const windowListeners: Record<string, Listener[]> = {}

  const fakeWindow = {
    requestAnimationFrame: (cb: Listener) => { rafQueue.push(cb); return rafQueue.length },
    cancelAnimationFrame: (id: number) => { rafQueue[id - 1] = () => {} },
    addEventListener: (type: string, fn: Listener) => {
      ;(windowListeners[type] ??= []).push(fn)
    },
    scrollY: 0,
    getComputedStyle: () => ({ marginBottom: layout.marginBottom() }),
  }
  const fakeDocument = {
    documentElement: {
      style: layout.rootStyle,
      get clientHeight() { return layout.clientHeight() },
    },
    body: {
      get scrollHeight() { return layout.scrollHeight() },
      getBoundingClientRect: () => ({ top: layout.top(), height: layout.rectHeight() }),
    },
    addEventListener: () => {},
    querySelectorAll: () => [],
  }
  const fakeParent = {
    postMessage: (msg: { type: string; height: number }) => {
      if (msg?.type !== 'mc-widget-height') return
      posts.push(msg)
      layout.onPost(msg.height)
    },
  }
  let roCallback: Listener = () => {}
  class FakeResizeObserver {
    constructor(cb: Listener) { roCallback = cb }
    observe() { /* initial layout is driven by the script's own schedule() */ }
    disconnect() {}
  }

  // Shadow the globals the reporter body reaches for. It uses window.*,
  // document.*, parent.postMessage, bare ResizeObserver, and bare
  // setTimeout/clearTimeout (left as the real — fake-timer-patched — globals).
  new Function('window', 'document', 'parent', 'ResizeObserver', extractReporterScript())(
    fakeWindow, fakeDocument, fakeParent, FakeResizeObserver,
  )

  return {
    posts,
    rootStyle: layout.rootStyle,
    flushRaf() {
      const q = rafQueue.splice(0)
      for (const cb of q) cb()
    },
    fireLoad() {
      for (const fn of windowListeners['load'] ?? []) fn()
    },
    /** body's box changed size (the ResizeObserver the reporter installs). */
    resize() { roCallback() },
    /** The frame's viewport changed size (the window `resize` event). */
    resizeViewport() {
      for (const fn of windowListeners['resize'] ?? []) fn()
    },
  }
}

describe('height reporter load re-report contract', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('posts an initial measurement on execution', () => {
    const r = runReporter(() => 400)
    r.flushRaf()
    expect(r.posts).toEqual([{ type: 'mc-widget-height', height: 400 }])
  })

  it('re-posts after window load even when the height is unchanged', () => {
    // The regression case: first measurement raced ahead of `load` (images
    // still fetching), height never changes again. Without the unconditional
    // load re-report the deadband keeps the reporter quiet, the parent — which
    // reset its record at `load` — sees silence, and a rendering artifact is
    // flagged "no longer showing".
    const r = runReporter(() => 400)
    r.flushRaf()
    expect(r.posts).toHaveLength(1)

    r.fireLoad()
    vi.advanceTimersByTime(150) // the reporter defers its load re-check ~100ms
    r.flushRaf()

    expect(r.posts).toHaveLength(2)
    expect(r.posts[1]).toEqual({ type: 'mc-widget-height', height: 400 })
  })

  it('stays quiet on jitter within the deadband outside of load', () => {
    // The re-report must not weaken the quietness contract everywhere else: an
    // animating widget still locks to its height and stops posting.
    let h = 400
    const r = runReporter(() => h)
    r.flushRaf()
    expect(r.posts).toHaveLength(1)

    h = 401 // within HEIGHT_REPORT_EPSILON_PX of the last post
    r.resize()
    r.flushRaf()
    vi.advanceTimersByTime(1000)
    r.flushRaf()
    expect(r.posts).toHaveLength(1)
  })
})

describe('height reporter measurement', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('rounds a fractional content height up to a whole pixel', () => {
    // body.scrollHeight is an integer that can round DOWN; a frame sized to it
    // is a fraction of a pixel short of the content and overflows.
    const r = runReporter(() => 400.3, { scrollHeight: () => 400 })
    r.flushRaf()
    expect(r.posts).toEqual([{ type: 'mc-widget-height', height: 401 }])
  })

  it("counts body's own top offset and bottom margin", () => {
    const r = runReporter(() => 400, { top: () => 8, marginBottom: () => '8px' })
    r.flushRaf()
    expect(r.posts[0].height).toBe(416)
  })

  it("counts content that overflows body's box", () => {
    const r = runReporter(() => 300, { scrollHeight: () => 600 })
    r.flushRaf()
    expect(r.posts[0].height).toBe(600)
  })
})

describe('height reporter vertical overflow', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('hides the vertical overflow while the frame holds the whole document', () => {
    const r = runReporter(() => 400, { clientHeight: () => 400 })
    r.flushRaf()
    expect(r.rootStyle.overflowY).toBe('hidden')
    vi.advanceTimersByTime(5000)
    r.flushRaf()
    expect(r.rootStyle.overflowY).toBe('hidden')
  })

  it('gives a fixed-height frame its scroll back once the grace window passes', () => {
    // An expanded view, a popout or a thumbnail holds the frame at a height
    // that never follows the report, so the document must stay scrollable.
    const r = runReporter(() => 1000, { clientHeight: () => 600 })
    r.flushRaf()
    vi.advanceTimersByTime(200)
    expect(r.rootStyle.overflowY).toBe('hidden')
    vi.advanceTimersByTime(200)
    expect(r.rootStyle.overflowY).toBe('')
  })

  it('counts only a pixel of overhang as fitting', () => {
    const r = runReporter(() => 602, { clientHeight: () => 600 })
    r.flushRaf()
    vi.advanceTimersByTime(1000)
    expect(r.rootStyle.overflowY).toBe('')
  })

  it("restores the document's own inline overflow-y when it scrolls", () => {
    const rootStyle = { overflowY: 'scroll' }
    const r = runReporter(() => 1000, { clientHeight: () => 600, rootStyle })
    r.flushRaf()
    expect(rootStyle.overflowY).toBe('hidden')
    vi.advanceTimersByTime(1000)
    expect(rootStyle.overflowY).toBe('scroll')
  })

  it('keeps the scrollbar hidden while a slow host is still growing the frame', () => {
    // Each viewport resize restarts the grace window: a host that takes longer
    // than the window to apply a growth is still sizing the frame.
    const seen: string[] = []
    let value = ''
    const rootStyle = {
      get overflowY() { return value },
      set overflowY(v: string) { value = v; seen.push(v) },
    }
    let viewport = 200
    const r = runReporter(() => 1000, { clientHeight: () => viewport, rootStyle })
    r.flushRaf()
    vi.advanceTimersByTime(250)
    viewport = 600 // the host is partway there
    r.resizeViewport()
    r.flushRaf()
    vi.advanceTimersByTime(250)
    viewport = 1000
    r.resizeViewport()
    r.flushRaf()
    vi.advanceTimersByTime(5000)
    expect(seen).toEqual(['hidden'])
  })

  it('keeps the scroll once given back while the viewport holds its height', () => {
    // Content that keeps its aspect ratio (a full-width image or chart) gets
    // SHORTER when a classic scrollbar narrows it. Just over the viewport at
    // full width and inside it once narrowed, it fits only while the scrollbar
    // shows, so re-clipping on that fit would toggle the scrollbar forever.
    const rootStyle = { overflowY: '' }
    const seen: string[] = []
    const style = {
      get overflowY() { return rootStyle.overflowY },
      set overflowY(v: string) { rootStyle.overflowY = v; seen.push(v) },
    }
    const scrollbar = () => rootStyle.overflowY !== 'hidden'
    const content = () => (scrollbar() ? 590 : 610)
    const r = runReporter(content, { clientHeight: () => 600, rootStyle: style })
    let laidOut = scrollbar()
    for (let tick = 0; tick < 300; tick++) {
      r.flushRaf()
      vi.advanceTimersByTime(16)
      if (scrollbar() !== laidOut) { laidOut = scrollbar(); r.resize() }
    }
    expect(seen).toEqual(['hidden', ''])
  })

  it('hides the overflow again when the frame grows to fit', () => {
    // Collapsing an expanded view returns the frame to the content's height.
    let viewport = 600
    const r = runReporter(() => 1000, { clientHeight: () => viewport })
    r.flushRaf()
    vi.advanceTimersByTime(1000)
    expect(r.rootStyle.overflowY).toBe('')
    viewport = 1000
    r.resizeViewport()
    r.flushRaf()
    expect(r.rootStyle.overflowY).toBe('hidden')
  })
})

describe('height reporter in a self-sizing frame with a classic scrollbar', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  // Layout model of the reported loop. The frame shows a classic (space-taking)
  // vertical scrollbar whenever its overflow is not hidden and the content is
  // taller than the viewport. The text lays out at 1000.7px at full width and
  // re-wraps to 1040px when the scrollbar narrows the viewport, and the
  // viewport is half a pixel short of the frame's CSS height, as device-pixel
  // snapping leaves it at a fractional display scale. The host mirrors
  // WidgetFrame: it rounds the report, applies a growth at once and a shrink
  // 250ms later. One tick is one 16ms frame.
  function hostedFrame() {
    const WIDE = 1000.7
    const NARROW = 1040
    const SNAP = 0.5
    const rootStyle = { overflowY: '' }
    let frameH = 200
    let shrink: ReturnType<typeof setTimeout> | null = null
    const viewport = () => frameH - SNAP
    const scrollbar = () => rootStyle.overflowY !== 'hidden' && WIDE > viewport()
    const content = () => (scrollbar() ? NARROW : WIDE)
    const onPost = (reported: number) => {
      const h = Math.round(reported)
      if (shrink) { clearTimeout(shrink); shrink = null }
      if (h === frameH) return
      if (h > frameH) frameH = h
      else shrink = setTimeout(() => { shrink = null; frameH = h }, 250)
    }
    const r = runReporter(content, {
      scrollHeight: () => Math.round(content()),
      // Rounded down, the less forgiving of the two ways an engine rounds it.
      clientHeight: () => Math.floor(viewport()),
      rootStyle,
      onPost,
    })
    let laidOutFrameH = frameH
    let laidOutScrollbar = scrollbar()
    return {
      posts: r.posts,
      frameH: () => frameH,
      scrollbar,
      tick() {
        vi.advanceTimersByTime(16)
        if (frameH !== laidOutFrameH) { laidOutFrameH = frameH; r.resizeViewport() }
        const sb = scrollbar()
        // The scrollbar changes body's width, which is what the reporter's
        // ResizeObserver sees.
        if (sb !== laidOutScrollbar) { laidOutScrollbar = sb; r.resize() }
        r.flushRaf()
      },
    }
  }

  it('settles on one height instead of toggling its scrollbar forever', () => {
    const f = hostedFrame()
    for (let i = 0; i < 120; i++) f.tick()
    const settledPosts = f.posts.length
    const settledHeight = f.frameH()
    for (let i = 0; i < 300; i++) f.tick()
    expect(f.posts).toHaveLength(settledPosts)
    expect(f.frameH()).toBe(settledHeight)
    expect(f.scrollbar()).toBe(false)
  })

  it('reports once: no post follows the first growth', () => {
    const f = hostedFrame()
    for (let i = 0; i < 420; i++) f.tick()
    expect(f.posts).toEqual([{ type: 'mc-widget-height', height: 1001 }])
    expect(f.frameH()).toBe(1001)
  })
})
