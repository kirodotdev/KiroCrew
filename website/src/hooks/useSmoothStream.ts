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
 * It advances a "revealed" cursor toward the real content length at a steady,
 * adaptive rate via requestAnimationFrame:
 *   - a constant floor (BASE_CPS) keeps text flowing even when the model pauses;
 *   - a backlog term (CATCHUP) speeds up when the model races ahead, so the
 *     buffer never lags more than a few characters behind.
 * On stream end it flushes to the full content instantly.
 *
 * The emitted length is snapped back to the nearest word boundary so the
 * renderer never receives a half-streamed word (and the per-word reveal in
 * `rehypeStreamingReveal` stays clean). State updates are throttled to word
 * granularity — we only re-render when the snapped output actually changes,
 * not on every sub-word rAF frame.
 *
 * When `enabled` is false the hook is a no-op pass-through (returns `content`
 * unchanged, no rAF loop), so `streamMode: 'immediate'` restores the exact
 * pre-existing behavior.
 *
 * Constants below correspond roughly to the demo's "word reveal @ speed ~48".
 */

/** Floor reveal speed (chars/sec) so text still flows when the model idles or
 *  streams very slowly. */
const MIN_CPS = 50
/** Time constant (seconds) for the low-pass filter estimating the model's
 *  incoming token rate. The reveal rate tracks this, so it speeds up and slows
 *  down with the model automatically. */
const RATE_TAU = 0.35
/** Time constant (seconds) for draining residual backlog so the buffer
 *  converges to the live edge without overshooting. */
const CATCHUP_TAU = 0.35
/** Ceiling on reveal speed = the smoothness guarantee. Bounds how many chars
 *  (≈ words) can mount in one frame, so even a huge network burst fades in as a
 *  per-word wave instead of a single block. ~1.2 words/frame @60fps. Raising it
 *  reduces lag on very fast streams at the cost of burst smoothness. */
const MAX_CPS = 400

/** Largest index <= `idx` that sits on a word boundary, so we never emit a
 *  half-streamed word. Returns `s.length` when the whole string is consumed
 *  and `idx` when no preceding whitespace exists (e.g. one long unbroken run). */
// function snapToWord(s: string, idx: number): number {
//   if (idx >= s.length) return s.length
//   if (idx <= 0) return 0
//   const cut = Math.max(s.lastIndexOf(' ', idx - 1), s.lastIndexOf('\n', idx - 1))
//   return cut >= 0 ? cut + 1 : idx
// }

export function useSmoothStream(content: string, streaming: boolean, enabled: boolean, speed: number = 1): string {
  // Emitted (snapped) character count. Initialized to full length so already-
  // complete messages (history, variant switches) render instantly with no
  // animation — only genuine growth while streaming gets buffered.
  const [emitLen, setEmitLen] = useState(content.length)

  const contentRef = useRef(content)
  const streamingRef = useRef(streaming)
  const revRef = useRef(content.length)   // float reveal progress (chars)
  const emitRef = useRef(content.length)  // last committed snapped length
  const lastTargetRef = useRef(content.length)  // target length seen last frame
  const emaRef = useRef(MIN_CPS)          // smoothed incoming rate (chars/sec)
  contentRef.current = content
  streamingRef.current = streaming

  // Pin to full length whenever the buffer is disabled.
  useEffect(() => {
    if (!enabled) {
      revRef.current = content.length
      emitRef.current = content.length
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
  useEffect(() => {
    if (!enabled || streaming) return
    if (content.length !== emitRef.current) {
      revRef.current = content.length
      emitRef.current = content.length
      lastTargetRef.current = content.length
      setEmitLen(content.length)
    }
  }, [content, streaming, enabled])

  // The rAF drain loop. Restarts whenever `enabled`/`streaming` flips; reads
  // the latest content via ref so it doesn't restart on every delta.
  useEffect(() => {
    if (!enabled) return
    // Scale the reveal bounds by the speed preset (slow .5x … turbo 4x).
    const minCps = MIN_CPS * speed
    const maxCps = MAX_CPS * speed
    let raf = 0
    let last = 0
    lastTargetRef.current = contentRef.current.length  // avoid a spurious first-frame burst
    if (emaRef.current < minCps) emaRef.current = minCps
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
        emaRef.current = minCps
        lastTargetRef.current = target
        setEmitLen(target)
      }

      // 1. Estimate the model's incoming rate — a low-pass filter over the
      //    chars that arrived this frame. Tracks fast/slow output automatically.
      const arrived = target - lastTargetRef.current
      lastTargetRef.current = target
      const inst = arrived > 0 ? arrived / dt : 0
      const a = 1 - Math.exp(-dt / RATE_TAU)
      emaRef.current += a * (inst - emaRef.current)

      // 2. Adaptive reveal rate: track the model (ema) + drain residual backlog,
      //    then clamp to MAX_CPS — the per-frame smoothness guarantee.
      const backlog = target - revRef.current
      let rate = Math.max(minCps, emaRef.current) + backlog / CATCHUP_TAU
      if (rate > maxCps) rate = maxCps
      if (backlog > 0) revRef.current = Math.min(target, revRef.current + rate * dt)

      // 3. Emit at char granularity (no word snapping) for smooth per-char reveal.
      const caughtUp = revRef.current >= target
      const snapped = caughtUp ? target : Math.floor(revRef.current)
      if (snapped !== emitRef.current) { emitRef.current = snapped; setEmitLen(snapped) }

      // Keep draining after the stream ends so short/fast messages animate fully.
      if (streamingRef.current || !caughtUp) {
        raf = requestAnimationFrame(tick)
      } else {
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
