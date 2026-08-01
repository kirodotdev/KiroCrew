import { useEffect, useRef, useState } from 'react'

/**
 * Smoothing buffer for streamed text — Layer 1 of the streaming-feel rework.
 *
 * Raw streaming jitter comes from welding render cadence to network cadence:
 * `chatSlice` appends each WS delta straight into the message content, so the
 * visible text lurches forward by whatever chunk just landed (1 char or a whole
 * sentence) and freezes in the gaps between bursts.
 *
 * This hook sits between the raw growing `content` and what the renderer sees.
 * It is a constant-latency controller (the shape of an audio jitter buffer):
 * the reveal aims to trail the live edge by a fixed time lag, so
 *
 *     rate = backlog / LAG_SECS
 *
 * which self-regulates against every failure mode the previous EMA + catch-up
 * design fought piecemeal:
 *   - a steady model at R chars/sec settles a standing backlog of R·LAG chars
 *     and reveals at exactly R — smooth, with a constant ~LAG_SECS delay;
 *   - a network burst raises the backlog, so the rate ramps up (slew-limited,
 *     below) and the excess drains exponentially with time constant LAG_SECS —
 *     drain time is inherently bounded (and MAX_CPS keeps the cascade at a
 *     speed the eye can follow — see the constants below);
 *   - a gap between bursts is absorbed by the standing backlog: the reveal
 *     keeps flowing (decaying gently, never freezing) for up to ~LAG_SECS
 *     before it can starve. The old design revealed straight to the live edge,
 *     froze there, then surged on the next burst — the freeze→surge cycle the
 *     eye reads as "chunks".
 *
 * The applied rate is additionally slew-limited (low-pass filtered), so burst
 * arrivals read as smooth accelerations rather than step changes, and a MIN
 * floor keeps text flowing even when the model idles. On stream end the same
 * dynamics drain the remainder (exponential tail + MIN floor), so short/fast
 * messages still animate fully.
 *
 * Emission is at char granularity — we re-render only when the floored reveal
 * cursor actually advances, not on every rAF frame.
 *
 * When `enabled` is false the hook is a no-op pass-through (returns `content`
 * unchanged, no rAF loop), so `streamMode: 'immediate'` restores the exact
 * pre-existing behavior.
 */

/** Floor reveal speed (chars/sec) so text still flows when the model idles or
 *  streams very slowly. */
const MIN_CPS = 50
/** The target time lag (seconds) behind the live edge. This single constant is
 *  the smoothness/latency tradeoff: it is simultaneously the standing cushion
 *  that bridges inter-burst gaps, the drain time constant for bursts, and the
 *  perceived delay behind the raw stream. `speed` divides it (higher speed =
 *  lower latency = closer tracking of the raw cadence). */
const LAG_SECS = 0.4
/** Ceiling on the reveal rate (chars/sec) — the smoothness guarantee. Bounds
 *  how many chars can mount per frame (~10 @60fps, roughly 1.7 words), so a
 *  fat burst reads as a fast per-word cascade the eye can follow, never a
 *  blur. Matters most at stream START: the controller has no standing state
 *  yet, and a large first chunk (typical after a long thinking/tool phase)
 *  would otherwise demand backlog/lag = thousands of cps and the slew would
 *  happily ramp there. `speed` multiplies it. */
const MAX_CPS = 600
/** Bounded-drain escape hatch: no backlog may take longer than about this to
 *  drain (seconds). The effective ceiling is max(MAX_CPS, backlog/MAX_DRAIN_SECS),
 *  so a pathological paste-like burst (a whole multi-KB code block in one
 *  chunk) clears as a ~2.5s fast cascade instead of trailing at the cap for
 *  8+ seconds — while ordinary bursts (≤ MAX_CPS × MAX_DRAIN_SECS ≈ 1.5K
 *  chars) never engage it and stay under the smoothness ceiling. This bounded
 *  drain is also what makes a hard cap safe against runaway lag on very fast
 *  models (the failure the old design papered over with a 4x speed override). */
const MAX_DRAIN_SECS = 2.5
/** Slew time constant (seconds) for the APPLIED reveal rate. The desired rate
 *  steps discontinuously when a burst lands; low-pass filtering the applied
 *  rate turns those steps into smooth accelerations, so the reveal speeds up
 *  and coasts down instead of jerking. */
const RATE_SLEW_TAU = 0.15

export function useSmoothStream(content: string, streaming: boolean, enabled: boolean, speed: number = 1): string {
  // Emitted (floored) character count. Initialized to full length so already-
  // complete messages (history, variant switches) render instantly with no
  // animation — only genuine growth while streaming gets buffered.
  const [emitLen, setEmitLen] = useState(content.length)

  const contentRef = useRef(content)
  const streamingRef = useRef(streaming)
  const revRef = useRef(content.length)   // float reveal progress (chars)
  const emitRef = useRef(content.length)  // last committed floored length
  const rateRef = useRef(0)               // slew-limited APPLIED reveal rate (chars/sec)
  // True from the first streamed frame until the post-stream drain has fully
  // caught up. Distinguishes "backlog left over from a live stream" (the rAF
  // loop must finish revealing it smoothly) from "content changed on an idle,
  // fully-revealed message" (variant switch / patch — render instantly).
  const wasStreamingRef = useRef(false)
  contentRef.current = content
  streamingRef.current = streaming
  if (streaming) wasStreamingRef.current = true

  // Pin to full length whenever the buffer is disabled.
  useEffect(() => {
    if (!enabled) {
      revRef.current = content.length
      emitRef.current = content.length
      wasStreamingRef.current = false
      setEmitLen(content.length)
    }
  }, [enabled, content.length])

  // Snap to full length when content GROWS while not streaming. Once a message
  // finishes, the rAF loop stops itself (raf = 0 when !streaming && caughtUp)
  // and never restarts (its deps are [enabled, speed]), so a later content
  // change — a variant switch to a longer answer, or a post-completion patch —
  // was permanently truncated to the old emitLen by the slice at the bottom.
  // A non-streaming content change is not an incremental token reveal; render
  // it instantly (matching the "already-complete messages render instantly"
  // intent of the emitLen initializer). Genuine streaming growth is still
  // handled by the rAF loop below.
  //
  // CRUCIALLY this must NOT fire on the streaming→false transition itself:
  // under the constant-latency controller the reveal deliberately trails the
  // live edge by ~LAG_SECS of text, so at stream end there is ALWAYS unrevealed
  // residue — snapping here would flash the last half-second of every message
  // in as a block (the end-of-stream snap the old speed=4 override existed to
  // hide). While `wasStreamingRef` is up the residue belongs to the drain loop,
  // which reveals it at the slewed rate and lowers the flag when caught up.
  useEffect(() => {
    if (!enabled || streaming) return
    if (wasStreamingRef.current) return
    if (content.length !== emitRef.current) {
      revRef.current = content.length
      emitRef.current = content.length
      setEmitLen(content.length)
    }
  }, [content, streaming, enabled])

  // The rAF drain loop. Restarts whenever `enabled`/`speed` flips; reads the
  // latest content via ref so it doesn't restart on every delta.
  useEffect(() => {
    if (!enabled) return
    // Scale the latency target by the speed preset (slow .5x … turbo 4x):
    // higher speed = smaller lag = tighter tracking of the raw stream.
    const minCps = MIN_CPS * speed
    const maxCps = MAX_CPS * speed
    const lag = LAG_SECS / speed
    let raf = 0
    let last = 0
    const tick = (t: number) => {
      if (!last) last = t
      const dt = Math.min(0.1, (t - last) / 1000)  // clamp (tab refocus jumps)
      last = t
      if (dt <= 0) { raf = requestAnimationFrame(tick); return }

      const target = contentRef.current.length
      if (revRef.current > target) {
        // Content reset (demo loop restart or message switch) — reset buffer state
        revRef.current = target
        emitRef.current = target
        rateRef.current = 0
        setEmitLen(target)
      }

      // Constant-latency controller: reveal fast enough to hold the backlog at
      // ~lag seconds of text — the rate tracks the model's rate with a
      // constant delay. Clamped to the smoothness ceiling, except that no
      // backlog may take longer than ~MAX_DRAIN_SECS to clear (see MAX_CPS /
      // MAX_DRAIN_SECS above for why both halves exist).
      const backlog = target - revRef.current
      let desired = backlog > 0 ? Math.max(minCps, backlog / lag) : 0
      const ceil = Math.max(maxCps, backlog / MAX_DRAIN_SECS)
      if (desired > ceil) desired = ceil
      // Slew-limit the applied rate: bursts become smooth accelerations.
      const s = 1 - Math.exp(-dt / RATE_SLEW_TAU)
      rateRef.current += s * (desired - rateRef.current)
      if (backlog > 0) {
        revRef.current = Math.min(target, revRef.current + rateRef.current * dt)
      }

      // Emit at char granularity (no word snapping) for smooth per-char reveal.
      const caughtUp = revRef.current >= target
      const snapped = caughtUp ? target : Math.floor(revRef.current)
      if (snapped !== emitRef.current) { emitRef.current = snapped; setEmitLen(snapped) }

      // Keep draining after the stream ends so the trailing ~LAG_SECS of text
      // (and short/fast messages) animate fully. Only once fully caught up does
      // the message hand back to "idle" semantics (variant switches snap).
      if (streamingRef.current || !caughtUp) {
        raf = requestAnimationFrame(tick)
      } else {
        wasStreamingRef.current = false
        raf = 0
      }
    }
    raf = requestAnimationFrame(tick)
    return () => { if (raf) cancelAnimationFrame(raf) }
  }, [enabled, speed]) // Note: `streaming` intentionally excluded — the tick reads streamingRef
  // for its continuation condition. Including it would restart the rAF loop
  // (killing the in-flight drain) the moment streaming ends.

  if (!enabled) return content
  return content.slice(0, Math.min(emitLen, content.length))
}
