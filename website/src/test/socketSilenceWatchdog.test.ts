/**
 * The shared silence watchdog, in isolation from any socket.
 *
 * This is the one implementation the dashboard hook and the two app sockets all
 * drive, so its contract is pinned here directly: a visible OPEN socket silent for
 * the window is replaced, a socket that keeps delivering frames is not, hidden
 * time does not count, and a run of silent replacements widens the window up to
 * the cap. The host's `isOpen`/`onDead` and the injected `now` make it testable
 * without a real socket.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

import {
  SilenceWatchdog,
  WS_SILENCE_MS,
  WS_SILENCE_CHECK_MS,
  WS_SILENCE_MAX_MS,
} from '../utils/socketSilenceWatchdog'

describe('SilenceWatchdog', () => {
  let t = 0
  let open = true
  let dead = 0
  let hidden = false

  function make() {
    const w = new SilenceWatchdog({
      now: () => t,
      isOpen: () => open,
      // A real socket's replacement re-opens and re-seeds the silence clock
      // through the fresh socket's onopen; model that here so a still-silent
      // socket costs exactly one replacement per (widening) window.
      onDead: () => { dead += 1; w.markOpen() },
    })
    return w
  }

  /** Advance the clock AND fire every check interval that elapsed over `ms`. */
  function run(w: SilenceWatchdog, ms: number) {
    for (let elapsed = WS_SILENCE_CHECK_MS; elapsed <= ms; elapsed += WS_SILENCE_CHECK_MS) {
      t += WS_SILENCE_CHECK_MS
      vi.advanceTimersByTime(WS_SILENCE_CHECK_MS)
    }
  }

  beforeEach(() => {
    vi.useFakeTimers()
    t = 1_000_000
    open = true
    dead = 0
    hidden = false
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  })

  afterEach(() => {
    vi.useRealTimers()
    delete (document as { hidden?: boolean }).hidden
  })

  it('replaces a silent open socket once the window passes', () => {
    const w = make()
    w.markOpen()
    w.start()
    run(w, WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    expect(dead).toBe(1)
    w.stop()
  })

  it('does not replace a socket that keeps delivering frames', () => {
    const w = make()
    w.markOpen()
    w.start()
    for (let elapsed = 0; elapsed < WS_SILENCE_MS * 4; elapsed += WS_SILENCE_CHECK_MS) {
      w.markFrame()
      run(w, WS_SILENCE_CHECK_MS)
    }
    expect(dead).toBe(0)
    w.stop()
  })

  it('does not replace a socket that is no longer OPEN', () => {
    const w = make()
    w.markOpen()
    w.start()
    open = false
    run(w, WS_SILENCE_MS * 3)
    expect(dead).toBe(0)
    w.stop()
  })

  it('skips silence checks while the page is hidden', () => {
    const w = make()
    w.markOpen()
    w.start()
    hidden = true
    document.dispatchEvent(new Event('visibilitychange'))
    run(w, WS_SILENCE_MS * 3)
    expect(dead).toBe(0)
    w.stop()
  })

  it('doubles the window after a consecutive silent replacement', () => {
    const w = make()
    w.markOpen()
    w.start()
    // First replacement at the base window (onDead re-seeds the clock).
    run(w, WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    expect(dead).toBe(1)

    // The re-seeded socket is silent too, but the window is now doubled: nothing
    // at the base window, a replacement only once the doubled window passes.
    run(w, WS_SILENCE_MS + WS_SILENCE_CHECK_MS)
    expect(dead).toBe(1)
    run(w, WS_SILENCE_MS)
    expect(dead).toBe(2)
    w.stop()
  })

  it('caps the widened window at WS_SILENCE_MAX_MS', () => {
    const w = make()
    w.markOpen()
    w.start()
    // Drive a long silent streak so the window ramps to its cap (2**4 * 20s =
    // 320s already exceeds the 300s cap, so a handful of cap-length windows is
    // more than enough). Each replacement re-seeds via onDead.
    run(w, WS_SILENCE_MAX_MS * 6)
    const atCap = dead
    expect(atCap).toBeGreaterThan(0)
    // Pinned at the cap now: one full cap window of silence is exactly one more.
    run(w, WS_SILENCE_MAX_MS + WS_SILENCE_CHECK_MS)
    expect(dead).toBe(atCap + 1)
    w.stop()
  })

  it('stop() detaches the interval so no later tick replaces the socket', () => {
    const w = make()
    w.markOpen()
    w.start()
    w.stop()
    run(w, WS_SILENCE_MS * 3)
    expect(dead).toBe(0)
  })
})
