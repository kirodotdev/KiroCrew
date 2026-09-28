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

function runReporter(scrollHeight: () => number) {
  const posts: Array<{ type: string; height: number }> = []
  const rafQueue: Listener[] = []
  const windowListeners: Record<string, Listener[]> = {}

  const fakeWindow = {
    requestAnimationFrame: (cb: Listener) => { rafQueue.push(cb); return rafQueue.length },
    cancelAnimationFrame: (id: number) => { rafQueue[id - 1] = () => {} },
    addEventListener: (type: string, fn: Listener) => {
      ;(windowListeners[type] ??= []).push(fn)
    },
  }
  const fakeDocument = {
    body: { get scrollHeight() { return scrollHeight() } },
    addEventListener: () => {},
    querySelectorAll: () => [],
  }
  const fakeParent = {
    postMessage: (msg: { type: string; height: number }) => {
      if (msg?.type === 'mc-widget-height') posts.push(msg)
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
    flushRaf() {
      const q = rafQueue.splice(0)
      for (const cb of q) cb()
    },
    fireLoad() {
      for (const fn of windowListeners['load'] ?? []) fn()
    },
    resize() { roCallback() },
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
