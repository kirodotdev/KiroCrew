import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'

// Reproduces, and then pins the fix for, the `calc(100vh - Npx)` runaway the
// artifact viewer showed: a document that sizes an element to the rest of the
// screen grows the auto-sized frame ~32px per report until the 100,000px clamp.
//
// The loop is iframe -> vh -> content -> iframe. For the issue's body
//
//   header { height: 48px }
//   main   { height: calc(100vh - 48px) }
//
// and buildSrcdoc's injected `body { padding: 16px }`, a frame whose viewport is
// V reports 16 + 48 + (V - 48) + 16 = V + 32. A self-sizing host sets the frame
// to that report, which raises 100vh by 32, which makes the next report 32px
// taller again. The REAL reporter is extracted from buildSrcdoc's output (not a
// copy), so a change to HEIGHT_REPORTER_BODY is what these tests exercise.

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

interface FakeLayout {
  top: () => number
  rectHeight: () => number
  scrollHeight: () => number
  marginBottom: () => string
  clientHeight: () => number
  rootStyle: { overflowY: string }
  onPost: (height: number) => void
  /** Device screen height the fix caps the pin to (window.screen.availHeight).
   * 0 means `window.screen` is absent (the try/catch path). */
  screenAvailHeight: number
}

function runReporter(height: () => number, overrides: Partial<FakeLayout> = {}) {
  const layout: FakeLayout = {
    top: () => 0,
    rectHeight: height,
    scrollHeight: () => Math.round(height()),
    marginBottom: () => '0px',
    clientHeight: () => 100_000,
    rootStyle: { overflowY: '' },
    onPost: () => {},
    screenAvailHeight: 0,
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
    ...(layout.screenAvailHeight > 0 ? { screen: { availHeight: layout.screenAvailHeight } } : {}),
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
    observe() {}
    disconnect() {}
  }

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
    fireLoad() { for (const fn of windowListeners['load'] ?? []) fn() },
    resize() { roCallback() },
    resizeViewport() { for (const fn of windowListeners['resize'] ?? []) fn() },
  }
}

/** A self-sizing host (ArtifactBody / WidgetFrame): it sets the frame's height
 * to each reported height (growth at once, shrink deferred), the frame's
 * viewport follows the frame height, and `content(viewport)` computes the
 * document's reported height from that viewport. One tick is one 16ms frame.
 *
 * `scrollbarDelta` models a space-taking horizontal scrollbar: the frame's CSS
 * height is `frameH`, but `root.clientHeight` (the viewport the reporter reads
 * and that `content` sizes against) is `frameH - scrollbarDelta`. */
function hostedFrame(
  content: (viewport: number) => number,
  opts: {
    startFrameH?: number; screenAvailHeight?: number; scrollbarDelta?: number
    /** Clamp every applied height to this, as the hosts' clampFrameHeight does. */
    maxFrameH?: number
    /** Apply shrinks after this long; 0 applies them at once (ArtifactBody). */
    shrinkMs?: number
  } = {},
) {
  const startFrameH = opts.startFrameH ?? 200
  const screenAvailHeight = opts.screenAvailHeight ?? 0
  const scrollbarDelta = opts.scrollbarDelta ?? 0
  const maxFrameH = opts.maxFrameH ?? Infinity
  const shrinkMs = opts.shrinkMs ?? 250
  const rootStyle = { overflowY: '' }
  let frameH = startFrameH
  const applied: number[] = [startFrameH]
  let shrink: ReturnType<typeof setTimeout> | null = null
  // The viewport the reporter sees via clientHeight is the frame height less a
  // space-taking scrollbar.
  const viewport = () => frameH - scrollbarDelta
  const measured = () => content(viewport())
  const onPost = (reported: number) => {
    const h = Math.min(Math.round(reported), maxFrameH)
    if (shrink) { clearTimeout(shrink); shrink = null }
    if (h === frameH) return
    if (h > frameH || shrinkMs === 0) { frameH = h; applied.push(h) }
    else shrink = setTimeout(() => { shrink = null; frameH = h; applied.push(h) }, shrinkMs)
  }
  const r = runReporter(measured, {
    scrollHeight: () => Math.round(measured()),
    clientHeight: () => Math.floor(viewport()),
    rootStyle,
    onPost,
    screenAvailHeight,
  })
  let laidOutFrameH = frameH
  return {
    posts: r.posts,
    frameH: () => frameH,
    /** True if the host ever applied a height below one it had already applied
     * (a dip, even one undone within the same frame). */
    dipped: () => applied.some((h, i) => i > 0 && h < Math.max(...applied.slice(0, i))),
    /** The lowest height the host ever applied, the start included. */
    lowest: () => Math.min(...applied),
    tick() {
      vi.advanceTimersByTime(16)
      if (frameH !== laidOutFrameH) { laidOutFrameH = frameH; r.resizeViewport() }
      r.resize()
      r.flushRaf()
    },
    fireLoad() { r.fireLoad() },
  }
}

// The issue's body: a 48px header + main:calc(100vh - 48px), under the injected
// 16px body padding top and bottom. Reported height = viewport + 32.
const vhMinusHeader = (viewport: number) => viewport + 32

describe('artifact frame vh feedback (issue repro)', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('(a) calc(100vh - 48px) content settles within a few reports near one viewport', () => {
    // The host gives the frame a one-screen starting height (its cached /
    // estimated detail height). On origin/main the loop climbs ~32px per report
    // to the 100,000px clamp; the fix detects the non-convergent feedback and
    // pins the frame to one screen (the device's availHeight), so it never runs
    // away and never cuts the page off below one screen.
    const ONE_SCREEN = 900
    const f = hostedFrame(vhMinusHeader, { startFrameH: 760, screenAvailHeight: ONE_SCREEN })
    for (let i = 0; i < 400; i++) f.tick()
    const settled = f.frameH()
    // Settled: no further movement over the next 400 frames.
    for (let i = 0; i < 400; i++) f.tick()
    expect(f.frameH()).toBe(settled)
    // Lands at one screen, nowhere near the 100,000px runaway cap.
    expect(settled).toBeGreaterThanOrEqual(ONE_SCREEN - 8)
    expect(settled).toBeLessThanOrEqual(ONE_SCREEN + 8)
    // Bounded, not hundreds: the detector observes ~600px of flat-excess growth
    // (to tell a runaway from a slow converger) then pins and goes quiet.
    expect(f.posts.length).toBeLessThan(40)
  })

  it('(b) a normal tall page still reports its full content height', () => {
    // A document whose content does NOT depend on the viewport (a long article)
    // must still size the frame to its real height.
    const TALL = 6000
    const f = hostedFrame(() => TALL, { startFrameH: 200 })
    for (let i = 0; i < 50; i++) f.tick()
    expect(f.frameH()).toBe(TALL)
  })

  it('(c) min-height:100vh still settles as today (self-consistent fixed point)', () => {
    // min-height:100vh reports exactly the viewport (plus the injected padding),
    // which is a fixed point: content(V) = V means the frame settles at its
    // first stable value, not a runaway.
    const minVh = (viewport: number) => Math.max(viewport, 204)
    const f = hostedFrame(minVh, { startFrameH: 200 })
    for (let i = 0; i < 200; i++) f.tick()
    const settled = f.frameH()
    for (let i = 0; i < 200; i++) f.tick()
    expect(f.frameH()).toBe(settled)
    expect(settled).toBeLessThanOrEqual(10_000)
  })

  it('(d) steady real growth that does NOT track the viewport is never pinned', () => {
    // The false-positive case the excess-trend signal cannot tell from a loop on
    // its own: content that grows a constant 20px per frame for a reason
    // UNRELATED to the viewport (an accordion opening on a CSS height
    // transition, a widget streaming fixed-height rows). Its excess over the
    // viewport looks flat, like a loop. The provisional pin's confirm (shrink
    // the viewport, check the content follows it down) must reject it, so the
    // frame reaches its full 3000px. Runs on the host whose shrink is deferred
    // 250ms, so the bounded-pending timer is not what saves it.
    //
    // The frame must also never drop below a height it already had: a pin that
    // the host applies before the confirm rejects it shows as a visible
    // collapse to one screen and back. Checked on a deferring host and on one
    // that applies shrinks at once (ArtifactBody).
    for (const shrinkMs of [250, 0]) {
      let step = 0
      const grow = () => Math.min(200 + step, 3000)
      const f = hostedFrame(() => grow(), { startFrameH: 200, screenAvailHeight: 900, shrinkMs })
      for (let i = 0; i < 460; i++) {
        if (i < 400) step += 20
        f.tick()
      }
      expect(f.frameH()).toBe(3000)
      expect(f.dipped()).toBe(false)
    }
  })

  it('(d2) self-growth in rows with pauses never dips', () => {
    // Images loading in rows of 60px, with pauses longer than the hold (128ms)
    // and longer than a deferring host's shrink (400ms). Each row grows the
    // content while the viewport stands still, so it must never count toward a
    // loop. Checked on a deferring host and on one that applies shrinks at once.
    for (const shrinkMs of [250, 0]) {
      for (const every of [8, 20, 25]) {
        let step = 0
        const grow = () => Math.min(200 + step, 3000)
        const f = hostedFrame(() => grow(), { startFrameH: 200, screenAvailHeight: 900, shrinkMs })
        for (let i = 0; i < 50 * every; i++) {
          if (i % every === 0) step += 60
          f.tick()
        }
        expect(f.frameH(), `every ${every} frames, shrinkMs ${shrinkMs}`).toBe(3000)
        expect(f.dipped(), `every ${every} frames, shrinkMs ${shrinkMs}`).toBe(false)
      }
    }
  })

  it('(k) a small loop (calc(100vh - 76px), +4px per report) is still pinned', () => {
    // Report = viewport + 4: above the 2px deadband, so every report is posted
    // and the frame climbs; the excess must be allowed to start a streak even
    // though it is within the trend slack.
    const SCREEN = 900
    const f = hostedFrame((v) => v + 4, {
      startFrameH: 760, screenAvailHeight: SCREEN, maxFrameH: 100_000,
    })
    for (let i = 0; i < 1500; i++) f.tick()
    const settled = f.frameH()
    for (let i = 0; i < 400; i++) f.tick()
    expect(f.frameH()).toBe(settled)
    expect(settled).toBeLessThanOrEqual(SCREEN + 64)
  })

  it('(l) a cached height at the 100,000px clamp recovers to one screen', () => {
    // The host restores a runaway height already at the clamp; the content
    // reports 100,032 and the host clamps it back, so the viewport cannot grow
    // and the deadband swallows every report. The guard must still pin it.
    const SCREEN = 900
    const f = hostedFrame(vhMinusHeader, {
      startFrameH: 100_000, screenAvailHeight: SCREEN, maxFrameH: 100_000,
    })
    for (let i = 0; i < 200; i++) f.tick()
    const settled = f.frameH()
    for (let i = 0; i < 400; i++) f.tick()
    expect(f.frameH()).toBe(settled)
    expect(settled).toBeLessThanOrEqual(SCREEN + 64)
    expect(settled).toBeGreaterThanOrEqual(SCREEN - 20)
  })

  it('(e) a stale runaway cache start settles at one screen, not the cached size', () => {
    // getWidgetHeight can hold 27,000+ for anyone who already hit this bug, so
    // the host starts the frame there. Pinning to that cached start would not
    // help the very people who reported it. The pin target is the device screen
    // (availHeight), NOT the start viewport, so the issue's body settles at ~900.
    const SCREEN = 900
    const f = hostedFrame(vhMinusHeader, { startFrameH: 27_000, screenAvailHeight: SCREEN })
    for (let i = 0; i < 600; i++) f.tick()
    const settled = f.frameH()
    for (let i = 0; i < 400; i++) f.tick()
    expect(f.frameH()).toBe(settled)
    expect(settled).toBeLessThanOrEqual(SCREEN + 64)
    expect(settled).toBeGreaterThanOrEqual(80) // never below the MIN clamp
  })

  it('(f) the issue body with a 15px horizontal scrollbar settles and stops posting', () => {
    // GPT/Opus F1: the host sets the frame to the pin P, but clientHeight reads
    // P-15 (a space-taking horizontal scrollbar). A latch-clear that compared
    // clientHeight with the pin would read the 15px gap as an external resize,
    // clear the pin every frame, and ratchet 80<->208 forever. The fix records
    // the clientHeight the pin actually produced and compares against THAT, so
    // the scrollbar gap is not a resize. Must hold both with and without a
    // deferred shrink -- the host here defers 250ms.
    const SCREEN = 900
    const f = hostedFrame(vhMinusHeader, {
      startFrameH: 760, screenAvailHeight: SCREEN, scrollbarDelta: 15,
    })
    for (let i = 0; i < 600; i++) f.tick()
    const settled = f.frameH()
    const postsAtSettle = f.posts.length
    for (let i = 0; i < 400; i++) f.tick()
    // Settled (no oscillation) and quiet (no new posts) -- not cycling 80<->208.
    expect(f.frameH()).toBe(settled)
    expect(f.posts.length).toBe(postsAtSettle)
    expect(settled).toBeGreaterThanOrEqual(SCREEN - 20)
    expect(settled).toBeLessThanOrEqual(SCREEN + 64)
  })

  it('(g) a convergent height:98vh layout follows normally and is never pinned', () => {
    // GPT F2: height:98vh + padding reports ceil(0.98*viewport + 32). The excess
    // over the viewport SHRINKS each step (0.02*viewport falls as the viewport
    // grows), so it converges to a fixed point (~1502px) rather than running
    // away. The excess-trend arm must not fire on it; the frame must settle
    // where main ends, not be pinned at the 200px start.
    const vh98 = (viewport: number) => Math.ceil(0.98 * viewport + 32)
    const f = hostedFrame(vh98, { startFrameH: 200, screenAvailHeight: 900 })
    for (let i = 0; i < 600; i++) f.tick()
    const settled = f.frameH()
    for (let i = 0; i < 200; i++) f.tick()
    expect(f.frameH()).toBe(settled)
    // The fixed point of h = 0.98 h + 32 is 1600; with ceil and padding it lands
    // around there, well above the 200px start and the 900px screen -- proof it
    // was followed, not pinned.
    expect(settled).toBeGreaterThan(1000)
  })

  it('(h) first open from a 200px estimate pins at availHeight, not 200', () => {
    // Opus F3: with no cache the start height is DEFAULT_WIDGET_HEIGHT (200).
    // The old pin target min(base, avail) would pin the full-height body at 200
    // and cache 200 forever. The pin target must come from the real screen
    // measure, so the body settles near availHeight even from a 200px start.
    const SCREEN = 850
    const f = hostedFrame(vhMinusHeader, { startFrameH: 200, screenAvailHeight: SCREEN })
    for (let i = 0; i < 600; i++) f.tick()
    const settled = f.frameH()
    for (let i = 0; i < 200; i++) f.tick()
    expect(f.frameH()).toBe(settled)
    expect(settled).toBeGreaterThanOrEqual(SCREEN - 20)
    expect(settled).toBeLessThanOrEqual(SCREEN + 64)
  })

  it('(i) a host that ignores the pin still receives later real growth', () => {
    // Design note: the pin is provisional. A host that never sizes the frame to
    // reports (its viewport never moves) must not leave the reporter stuck in
    // 'pending'. After the bound (~500ms) the reporter drops back to following
    // and posts real heights again. Here the "host" holds a fixed viewport and
    // the content keeps growing; the reporter must keep posting the growth.
    const FIXED_VIEWPORT = 400
    let step = 0
    // Content tracks a viewport that never actually changes (host ignores pins),
    // but keeps growing on its own so there is always a new height to report.
    const grow = () => 600 + step
    // A host whose frame height (and thus viewport) is frozen: clientHeight is
    // constant regardless of what we post.
    const posts: number[] = []
    const r = runReporter(() => grow(), {
      clientHeight: () => FIXED_VIEWPORT,
      scrollHeight: () => Math.round(grow()),
      onPost: (h) => posts.push(h),
      screenAvailHeight: 900,
    })
    for (let i = 0; i < 200; i++) { step += 20; vi.advanceTimersByTime(16); r.resize(); r.flushRaf() }
    // The reporter kept posting the growing real height (last post is large),
    // rather than freezing at a one-screen pin.
    expect(posts[posts.length - 1]).toBeGreaterThan(2000)
  })

  it('(j) a window load while the pin is pending does not cancel it', () => {
    // GPT F2: a cached-runaway widget (27,000px start) that finishes an async
    // script load mid-pin. The host defers the shrink 250ms, and window `load`
    // fires ~400ms after startup -- while the pin is still pending. The load
    // re-report resets lastSent and runs the first-measurement branch; if that
    // branch posted the full content height it would cancel the host's deferred
    // shrink, the pin would time out, and the runaway would resume. It must
    // re-post the pin instead, so the frame still settles at one screen.
    const SCREEN = 900
    const f = hostedFrame(vhMinusHeader, { startFrameH: 27_000, screenAvailHeight: SCREEN })
    let fired = false
    // ~400ms in at 16ms/tick is ~25 ticks; fire once, around then.
    for (let i = 0; i < 600; i++) {
      f.tick()
      if (!fired && i === 25) { f.fireLoad(); fired = true }
    }
    const settled = f.frameH()
    for (let i = 0; i < 400; i++) f.tick()
    expect(f.frameH()).toBe(settled)
    // Settles at one screen, not stuck near the 27,000px cached runaway.
    expect(settled).toBeLessThanOrEqual(SCREEN + 64)
    expect(settled).toBeGreaterThanOrEqual(SCREEN - 20)
  })

  it('(l2) a window load during the clamp probe does not cancel it', () => {
    // Same clamp-cached start as (l), but window load fires while the host is
    // still deferring the probe shrink. The load re-report must re-post the
    // probe height, not the content height, or the shrink is cancelled and the
    // frame stays at the clamp.
    const SCREEN = 900
    const f = hostedFrame(vhMinusHeader, {
      startFrameH: 100_000, screenAvailHeight: SCREEN, maxFrameH: 100_000,
    })
    for (let i = 0; i < 400; i++) {
      f.tick()
      if (i === 7) f.fireLoad()
    }
    const settled = f.frameH()
    for (let i = 0; i < 400; i++) f.tick()
    expect(f.frameH()).toBe(settled)
    expect(settled).toBeLessThanOrEqual(SCREEN + 64)
  })

  it('(n) min-height:calc(100vh - 48px) over a 1,280px content floor is pinned', () => {
    // Content = max(viewport + 32, 1280). It runs away like the issue's body,
    // but the one-screen pin only shrinks it to its 1,280 floor, part of the
    // way. That partial follow must still confirm the loop, not rule it out.
    for (const shrinkMs of [250, 0]) {
      const SCREEN = 900
      const f = hostedFrame((v) => Math.max(v + 32, 1280), {
        startFrameH: 760, screenAvailHeight: SCREEN, maxFrameH: 100_000, shrinkMs,
      })
      for (let i = 0; i < 1500; i++) f.tick()
      const settled = f.frameH()
      for (let i = 0; i < 400; i++) f.tick()
      expect(f.frameH()).toBe(settled)
      expect(settled).toBeLessThanOrEqual(SCREEN + 64)
    }
  })

  it('(m) a static document taller than the clamp stays at the clamp', () => {
    // A 150,000px log or table: the host clamps it to 100,000px. Its content
    // never follows the viewport, so it must not be pinned to one screen; the
    // clamp check may only shrink it by its small probe, then gives it back.
    for (const shrinkMs of [250, 0]) {
      const f = hostedFrame(() => 150_000, {
        startFrameH: 100_000, screenAvailHeight: 900, maxFrameH: 100_000, shrinkMs,
      })
      for (let i = 0; i < 400; i++) f.tick()
      expect(f.frameH()).toBe(100_000)
      expect(f.lowest()).toBeGreaterThanOrEqual(100_000 - 64)
    }
  })
})
