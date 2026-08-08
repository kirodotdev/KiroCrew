/**
 * usePlayfulMotion — makes the ghost feel alive/pokeable. A single rAF loop
 * drives three subtle motions on the art wrapper (transform, no React re-renders):
 *   • idle bob   — gentle vertical float while idle
 *   • cursor lean — the body drifts a few px toward the cursor
 *   • click nod  — poke() kicks a small downward nod that eases back (no scale/squish)
 * Gated by `activeRef` (false while dragging/peeking so it stays put).
 *
 * Ported verbatim from the desktop app's src/renderer/hooks/usePlayfulMotion.ts —
 * every timing, amplitude and easing constant is unchanged. The one addition is a
 * `prefers-reduced-motion` guard on the CONTINUOUS idle bob (the source has none):
 * the bob amplitude is forced to 0 when the user asks for reduced motion, while the
 * interaction-driven lean and nod are left intact.
 */
import { useCallback, useEffect, useRef, type RefObject, type MutableRefObject } from 'react'

export function usePlayfulMotion(
  ref: RefObject<HTMLElement>,
  activeRef: MutableRefObject<boolean>,
  enabledRef?: MutableRefObject<boolean>,
  /**
   * True while an ancestor mirrors the art with `scaleX(-1)`.
   *
   * The lean is applied INSIDE that mirror, so a positive translateX moves left on
   * screen. Without this the body would drift away from the cursor while the eyes
   * drift toward it — and the design requires both to move to the same side.
   */
  flipXRef?: MutableRefObject<boolean>,
) {
  const s = useRef({
    t: 0, last: 0,
    bob: 0, amp: 0,
    leanX: 0, leanY: 0, tgX: 0, tgY: 0,
    nod: 0, vnod: 0,
  })

  // Kick a small downward nod that springs back (a gentle "acknowledge").
  const poke = useCallback(() => {
    if (enabledRef && !enabledRef.current) return
    s.current.nod = 5
    s.current.vnod = 0
  }, [enabledRef])

  useEffect(() => {
    // Continuous idle motion honours prefers-reduced-motion (the source did not);
    // the cursor lean and click nod are interaction-driven, so they stay.
    const reduceMq = window.matchMedia('(prefers-reduced-motion: reduce)')
    let reduce = reduceMq.matches
    const onReduceChange = () => { reduce = reduceMq.matches }
    reduceMq.addEventListener('change', onReduceChange)

    let raf = 0
    const step = (now: number) => {
      const st = s.current
      const dt = st.last ? Math.min(0.05, (now - st.last) / 1000) : 0.016
      st.last = now
      st.t += dt

      // Custom packs (enabled=false) get NO idle motion — hold perfectly still.
      if (enabledRef && !enabledRef.current) {
        const el = ref.current
        if (el && el.style.transform !== 'none') el.style.transform = 'none'
        raf = requestAnimationFrame(step)
        return
      }

      const active = activeRef.current
      // Ease the bob amplitude in/out so starting/stopping isn't abrupt. Reduced
      // motion pins the target at 0, so the continuous float never starts.
      const targetAmp = active && !reduce ? 3.5 : 0
      st.amp += (targetAmp - st.amp) * 0.06
      st.bob = Math.sin(st.t * 1.9) * st.amp

      // Ease the lean toward its cursor-driven target.
      st.leanX += (st.tgX - st.leanX) * 0.08
      st.leanY += (st.tgY - st.leanY) * 0.08

      // Nod spring back toward 0 — well-damped, no bounce.
      st.vnod += (0 - st.nod) * 0.14 - st.vnod * 0.5
      st.nod += st.vnod

      const el = ref.current
      if (el) {
        const leanX = flipXRef?.current ? -st.leanX : st.leanX
        el.style.transform =
          `translate(${leanX.toFixed(2)}px, ${(st.bob + st.leanY + st.nod).toFixed(2)}px)`
      }
      raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)

    const onMove = (e: MouseEvent) => {
      if (enabledRef && !enabledRef.current) return
      const el = ref.current
      if (!el) { return }
      const r = el.getBoundingClientRect()
      const cx = r.left + r.width / 2
      const cy = r.top + r.height / 2
      const dx = e.clientX - cx
      const dy = e.clientY - cy
      const n = Math.hypot(dx, dy) || 1
      const mag = Math.min(1, n / 220)
      s.current.tgX = (dx / n) * mag * 3.5
      s.current.tgY = (dy / n) * mag * 2.5
    }
    window.addEventListener('mousemove', onMove)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('mousemove', onMove)
      reduceMq.removeEventListener('change', onReduceChange)
    }
  }, [ref, activeRef, enabledRef])

  return { poke }
}
