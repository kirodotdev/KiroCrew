/**
 * Finds the control the current guide step points at and follows it.
 *
 * The target is the ONE element the registry names: a registered
 * `data-guide-anchor`, or the exact Settings row (`resolveSettingElementStrict`).
 * It must be visibly rendered; nothing near it stands in. While it is absent
 * the tracker waits a bounded time for a panel that mounts late, then reports
 * `target_missing` once. A `reach` step reports `observed` once the UI shows a
 * later registered anchor — the human moved the form on. A `committed` step is
 * never reported by the browser; after its save was submitted, the target
 * leaving (a dialog closing on success) is not "missing" either.
 */
import { useEffect, useRef, useState } from 'react'
import { resolveSettingElementStrict } from '../hooks/useSettingHighlight'
import { findGuideAnchor, type GuideStepPlan, type GuideTarget } from './guideActions'

/** How long a registered target may take to appear before it is missing. */
export const GUIDE_TARGET_WAIT_MS = 10_000
/** Poll cadence while a step is tracked; scroll and resize re-measure at once. */
export const GUIDE_TRACK_TICK_MS = 250

export interface GuideRect {
  top: number
  left: number
  width: number
  height: number
}

export function resolveGuideTarget(target: GuideTarget): HTMLElement | null {
  return target.kind === 'anchor' ? findGuideAnchor(target.anchor) : resolveSettingElementStrict(target.entry)
}

/** Rendered and painted: connected, non-empty box, not hidden or inert. */
export function isGuideTargetVisible(el: HTMLElement | null): el is HTMLElement {
  if (!el || !el.isConnected) return false
  if (el.closest('[hidden], [inert]')) return false
  const style = window.getComputedStyle(el)
  if (style.visibility === 'hidden' || style.display === 'none') return false
  const r = el.getBoundingClientRect()
  return r.width > 0 && r.height > 0
}

const sameRect = (a: GuideRect | null, b: GuideRect | null) =>
  a === b || (!!a && !!b && a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height)

export function useGuideStepTracker({
  stepId,
  step,
  enabled,
  suppressMissing,
  reduceMotion,
  onObserved,
  onMissing,
}: {
  /** Changes whenever the tracked step changes (guide, action, step). */
  stepId: string
  step: GuideStepPlan | null
  enabled: boolean
  /** The committed save was submitted: absence now means "waiting", not missing. */
  suppressMissing: boolean
  reduceMotion: boolean
  onObserved: () => void
  onMissing: () => void
}): GuideRect | null {
  const [rect, setRect] = useState<GuideRect | null>(null)
  const cb = useRef({ onObserved, onMissing, suppressMissing })
  cb.current = { onObserved, onMissing, suppressMissing }

  useEffect(() => {
    setRect(null)
    if (!enabled || !step) return
    let done = false
    let missingSince: number | null = null
    let missingReported = false
    let scrolled = false
    let frame = 0
    const tick = () => {
      if (done) return
      if (step.complete.kind === 'reach' && step.complete.anchors.some(a => isGuideTargetVisible(findGuideAnchor(a)))) {
        done = true
        setRect(null)
        cb.current.onObserved()
        return
      }
      const el = resolveGuideTarget(step.target)
      if (isGuideTargetVisible(el)) {
        missingSince = null
        if (!scrolled) {
          scrolled = true
          el.scrollIntoView?.({ block: 'center', behavior: reduceMotion ? 'auto' : 'smooth' })
        }
        const r = el.getBoundingClientRect()
        const next = { top: r.top, left: r.left, width: r.width, height: r.height }
        setRect(prev => (sameRect(prev, next) ? prev : next))
        return
      }
      setRect(null)
      if (cb.current.suppressMissing) { missingSince = null; return }
      if (missingReported) return
      const now = Date.now()
      if (missingSince === null) missingSince = now
      else if (now - missingSince >= GUIDE_TARGET_WAIT_MS) {
        // Keep ticking: a step reported missing still resumes when the form
        // reaches a later registered control.
        missingReported = true
        cb.current.onMissing()
      }
    }
    const onMove = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(tick)
    }
    tick()
    const id = setInterval(tick, GUIDE_TRACK_TICK_MS)
    window.addEventListener('scroll', onMove, true)
    window.addEventListener('resize', onMove)
    return () => {
      done = true
      clearInterval(id)
      cancelAnimationFrame(frame)
      window.removeEventListener('scroll', onMove, true)
      window.removeEventListener('resize', onMove)
    }
    // `stepId` names the step; `step` is derived from it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stepId, enabled, reduceMotion])

  return rect
}
