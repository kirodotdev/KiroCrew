/**
 * The drawer settle must reach the COMPOSITOR.
 *
 * The mobile panel and the chat pane behind it share one main thread, and with
 * sessions streaming that thread stalls unpredictably (chunk flushes are held —
 * see lib/streamHold — but tool events, subagent status pushes and the panel's
 * own mount are not). Only an animation the main thread does not drive holds
 * its frame rate through that. `animateDrawer` therefore animates the ELEMENTS
 * (`transform` on the panel, `opacity` on the scrim) via `Element.animate`,
 * falling back to the main-thread tween when no element is registered or under
 * reduced motion.
 *
 * SAFE ONLY because the drawer's sidebar renders `staticRows` — no projection
 * node may live under a compositor-driven transform (see
 * ChatSidebar.staticRows.test.tsx for that half of the pairing).
 *
 * Beyond the mechanism, this pins the ORDER the arrival is published in:
 * `x` is reconciled BEFORE the `fill: 'forwards'` animation is cancelled,
 * because cancelling uncovers the inline style — a stale one is a visible snap
 * back to where the settle began.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { motionValue } from 'framer-motion'
import { animateDrawer, registerDrawerTargets, takeOverDrawer } from '../hooks/useDrawerSwipe'
import { releaseStreamingFlushes, streamingFlushHoldMs } from '../lib/streamHold'

interface FakeAnimation {
  keyframes: Record<string, unknown>[]
  timing: Record<string, unknown>
  cancelled: boolean
  onfinish: (() => void) | null
  oncancel: (() => void) | null
  cancel: () => void
}

/** Replace `animate` on one element with a recorder returning a fake Animation. */
function stubAnimate(el: HTMLElement, log: FakeAnimation[]) {
  ;(el as unknown as { animate: unknown }).animate = (keyframes: Record<string, unknown>[], timing: Record<string, unknown>) => {
    const a: FakeAnimation = {
      keyframes, timing, cancelled: false, onfinish: null, oncancel: null,
      cancel() { this.cancelled = true },
    }
    log.push(a)
    return a
  }
}

const TRAVEL = 390

describe('animateDrawer — compositor settle', () => {
  let panel: HTMLDivElement
  let scrim: HTMLDivElement
  let panelAnims: FakeAnimation[]
  let scrimAnims: FakeAnimation[]
  let x: ReturnType<typeof motionValue<number>>
  let unregister: () => void

  beforeEach(() => {
    document.body.replaceChildren()
    releaseStreamingFlushes()
    panel = document.createElement('div')
    scrim = document.createElement('div')
    document.body.append(panel, scrim)
    panelAnims = []
    scrimAnims = []
    stubAnimate(panel, panelAnims)
    stubAnimate(scrim, scrimAnims)
    x = motionValue(-TRAVEL)
    // No reduced-motion preference: that path is deliberately main-thread.
    Object.defineProperty(window, 'matchMedia', {
      writable: true, configurable: true,
      value: vi.fn().mockImplementation((q: string) => ({ matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
    })
    unregister = registerDrawerTargets(x, {
      panel: () => panel,
      scrim: () => scrim,
      travel: () => TRAVEL,
    })
  })

  it('animates the panel element rather than sampling the value per frame', () => {
    animateDrawer(x, 0)
    expect(panelAnims).toHaveLength(1)
    expect(panelAnims[0].keyframes).toEqual([
      { transform: `translate3d(${-TRAVEL}px, 0, 0)` },
      { transform: 'translate3d(0px, 0, 0)' },
    ])
    // The curve lives in the keyframe timing, which is what makes it expressible
    // to the compositor at all — a spring would have to be sampled here instead.
    // The exact control points and durations are pinned once, in
    // animateDrawer.curve.test.ts; asserting them here too would mean a retune
    // has to edit two files that are testing different things.
    expect(panelAnims[0].timing.easing).toMatch(/^cubic-bezier\(/)
    expect(panelAnims[0].timing.duration).toBeGreaterThan(0)
    expect(panelAnims[0].timing.fill).toBe('forwards')
    // `x` has NOT moved: the compositor owns the offset until it arrives.
    expect(x.get()).toBe(-TRAVEL)
  })

  it('fades the scrim in lockstep, since its own binding reads a value that is not moving', () => {
    animateDrawer(x, 0)
    expect(scrimAnims).toHaveLength(1)
    expect(scrimAnims[0].keyframes).toEqual([{ opacity: 0 }, { opacity: 1 }])
    // Lockstep is the invariant: the scrim rides the panel's own timing object,
    // so the two cannot be given different curves or durations by accident.
    expect(scrimAnims[0].timing).toEqual(panelAnims[0].timing)
  })

  it('holds the streaming flushes for the slide and releases on arrival', () => {
    animateDrawer(x, 0)
    expect(streamingFlushHoldMs()).toBeGreaterThan(300)
    panelAnims[0].onfinish?.()
    expect(streamingFlushHoldMs()).toBe(0)
  })

  it('publishes the arrival into the ELEMENT inline styles before cancelling the fill', () => {
    const order: string[] = []
    const onDone = vi.fn(() => order.push('done'))
    // The panel mounts CLOSED: its last React render serialized the closed
    // offset. Cancelling a fill:'forwards' animation reverts to exactly this,
    // so unless the arrival is written into the element itself first, the
    // just-arrived panel snaps offscreen — recorded on a real device as the
    // drawer opening, vanishing, then flashing back on the next re-render.
    panel.style.transform = `translate3d(${-TRAVEL}px, 0, 0)`
    animateDrawer(x, 0, onDone)
    x.on('change', v => order.push(`x=${v}`))
    panelAnims[0].cancel = function () { this.cancelled = true; order.push('cancel') }
    panelAnims[0].onfinish?.()

    // The element's OWN inline style carries the arrival — jumpTo(x) rewrites
    // it only on framer-bound panels (the sessions drawer), and the nav drawer
    // and right overlay are plain elements.
    expect(panel.style.transform).toBe('translate3d(0px, 0, 0)')
    expect(scrim.style.opacity).toBe('1')
    expect(x.get()).toBe(0)
    expect(panelAnims[0].cancelled).toBe(true)
    expect(scrimAnims[0].cancelled).toBe(true)
    expect(onDone).toHaveBeenCalledTimes(1)
    // Reconcile, THEN uncover the inline style. The other order is a snap.
    expect(order).toEqual(['x=0', 'cancel', 'done'])
  })

  it('a browser-cancelled animation still lands the element inline styles', () => {
    panel.style.transform = `translate3d(${-TRAVEL}px, 0, 0)`
    animateDrawer(x, 0)
    panelAnims[0].oncancel?.()
    expect(panel.style.transform).toBe('translate3d(0px, 0, 0)')
    expect(x.get()).toBe(0)
  })

  it('takeOverDrawer adopts the presented offset into the element inline style', () => {
    animateDrawer(x, 0)
    // The compositor is presenting -100px mid-flight. stubGlobal so the real
    // getComputedStyle comes back for the rest of the file (vi.unstubAllGlobals
    // in this test), not just for whichever test happens to run next.
    vi.stubGlobal('getComputedStyle', () => ({ transform: 'matrix(1, 0, 0, 1, -100, 0)' }))
    try {
      takeOverDrawer(x)
    } finally {
      vi.unstubAllGlobals()
    }
    expect(panel.style.transform).toBe('translate3d(-100px, 0, 0)')
    expect(x.get()).toBe(-100)
  })

  /**
   * Reversing inside the settle window must retire the outgoing animation.
   *
   * Nothing else does: no caller keeps `animateDrawer`'s returned canceller. A
   * survivor breaks the reversal twice — `x` is frozen while the compositor owns
   * the offset, so the replacement would keyframe from the OUTGOING settle's
   * start (a close→open reversal reading 0→0, i.e. a snap, not a slide), and the
   * survivor's `fill: 'forwards'` then re-presents its own end state over the
   * inline style published on arrival, parking the panel offscreen with its
   * phase still `open`.
   */
  it('a mid-settle reversal cancels the outgoing animation and starts from where the panel IS', () => {
    x.jump(0)
    animateDrawer(x, -TRAVEL) // closing
    expect(panelAnims).toHaveLength(1)
    expect(panelAnims[0].cancelled).toBe(false)

    // Reverse while the close is presenting -260px.
    vi.stubGlobal('getComputedStyle', () => ({ transform: 'matrix(1, 0, 0, 1, -260, 0)' }))
    try {
      animateDrawer(x, 0) // re-opening
    } finally {
      vi.unstubAllGlobals()
    }

    // The outgoing close is retired rather than left filling forwards.
    expect(panelAnims[0].cancelled).toBe(true)
    // …and the replacement keyframes from the offset actually on screen, so the
    // reopen slides. A stale `from` of 0 would make this a no-op 0->0 pair.
    expect(panelAnims).toHaveLength(2)
    expect(panelAnims[1].keyframes).toEqual([
      { transform: 'translate3d(-260px, 0, 0)' },
      { transform: 'translate3d(0px, 0, 0)' },
    ])

    // The retired close can no longer publish anything: its finish sees a newer
    // owner and returns.
    panelAnims[0].onfinish?.()
    expect(panel.style.transform).toBe('translate3d(-260px, 0, 0)')

    // The live reopen still arrives normally.
    panelAnims[1].onfinish?.()
    expect(panel.style.transform).toBe('translate3d(0px, 0, 0)')
    expect(x.get()).toBe(0)
  })

  it('still arrives when the browser cancels the animation instead of finishing it', () => {
    const onDone = vi.fn()
    animateDrawer(x, 0, onDone)
    panelAnims[0].oncancel?.()
    expect(x.get()).toBe(0)
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('takeOverDrawer cancels the running settle so the finger is not fought', () => {
    animateDrawer(x, 0)
    expect(panelAnims[0].cancelled).toBe(false)
    takeOverDrawer(x)
    expect(panelAnims[0].cancelled).toBe(true)
    // A finish arriving after the takeover must not retroactively claim the
    // panel — the gesture that took over owns where it ends up.
    panelAnims[0].onfinish?.()
    expect(x.get()).not.toBe(0)
  })

  it('falls back to the main thread when nothing is registered', () => {
    unregister()
    animateDrawer(x, 0)
    expect(panelAnims).toHaveLength(0)
  })

  it('falls back to the main thread under reduced motion', () => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true, configurable: true,
      value: vi.fn().mockImplementation((q: string) => ({ matches: true, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
    })
    animateDrawer(x, 0)
    expect(panelAnims).toHaveLength(0)
  })

  /**
   * The fallback path must still MOVE THE DOM, not just the value.
   *
   * The nav drawer and the right overlay have no drag gesture, so this path is
   * their only one whenever the compositor is unavailable — and neither is bound
   * to `x` (a plain <nav>; a template-string transform serialized once per React
   * render). Tweening the MotionValue alone leaves both stuck at the mounted
   * closed offset: under `prefers-reduced-motion` the panel would open into
   * nowhere and be unreachable.
   */
  it('paints the fallback tween onto the elements, and stops when cancelled', () => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true, configurable: true,
      value: vi.fn().mockImplementation((q: string) => ({ matches: true, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
    })
    // Mounted closed: the inline style the last React render left behind.
    panel.style.transform = `translate3d(${-TRAVEL}px, 0, 0)`
    const stop = animateDrawer(x, 0)
    // Painted once up front, so the element starts from a known offset.
    expect(panel.style.transform).toBe(`translate3d(${-TRAVEL}px, 0, 0)`)

    // …and TRACKS the value for the rest of the tween. This is the load-bearing
    // half: without the subscription the DOM never moves off the closed offset.
    x.set(-100)
    expect(panel.style.transform).toBe('translate3d(-100px, 0, 0)')
    // The scrim moves in lockstep off the same offset (1 - |at|/travel).
    expect(Number(scrim.style.opacity)).toBeCloseTo(1 - 100 / TRAVEL, 5)

    x.set(0)
    expect(panel.style.transform).toBe('translate3d(0px, 0, 0)')

    // Cancelling the settle releases the subscription — a leaked one would keep
    // writing this panel's transform from a later, unrelated owner of `x`.
    stop?.()
    x.set(-42)
    expect(panel.style.transform).toBe('translate3d(0px, 0, 0)')
  })
})
