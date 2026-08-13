/**
 * LottieRenderer — renders Lottie JSON animations via lottie-web.
 * Manages animation lifecycle: destroys old animation and loads new
 * when animationData changes. Notifies parent via onReady callback.
 */
import React, { useEffect, useRef } from 'react'
import lottie from 'lottie-web'
import type { AnimationItem } from 'lottie-web'

interface LottieRendererProps {
  animationData: string // Lottie JSON string
  width: number
  height: number
  loop?: boolean
  onReady?: () => void // animation loaded callback
}

/**
 * Remove Lottie EXPRESSIONS from a parsed clip, returning how many went.
 *
 * lottie-web compiles an expression with `eval()` (lottie.js — search
 * `_expression_function`), and the dashboard CSP is `script-src 'self'
 * 'unsafe-inline'` with NO `'unsafe-eval'`. So an expression does not merely
 * misbehave under this policy — it THROWS, the clip never finishes building, and
 * the slot paints nothing. That is what made three of the four Kiro Ghost clips
 * render as empty boxes while the fourth (the only one with no expression) was
 * fine: `idle`, `walking`, `thinking`, `working` were blank and `error` /
 * `offline` worked, which reads like "the pack is broken" rather than "one
 * feature is unavailable".
 *
 * Stripping loses NOTHING that could have run: under this CSP no expression can
 * ever evaluate. A clip whose motion depended on one animates less; a clip whose
 * expression was redundant (`loopOut()` over a track that already spans the comp,
 * which is what the shipped ghost used) is unchanged. Both beat invisible.
 *
 * Widening the CSP with `'unsafe-eval'` is the alternative and is rejected: it
 * would hand every script on the page a code-execution primitive to make a
 * companion bob.
 *
 * Only a STRING `x` is an expression. A numeric/array/object `x` is a coordinate
 * or a bezier easing handle and MUST survive — deleting those would corrupt every
 * keyframe in the file.
 */
function stripExpressions(node: unknown): number {
  let removed = 0
  if (Array.isArray(node)) {
    for (const item of node) removed += stripExpressions(item)
    return removed
  }
  if (node !== null && typeof node === 'object') {
    const obj = node as Record<string, unknown>
    if (typeof obj.x === 'string') {
      delete obj.x
      removed += 1
    }
    for (const value of Object.values(obj)) removed += stripExpressions(value)
  }
  return removed
}

const LottieRendererInner: React.FC<LottieRendererProps> = ({
  animationData,
  width,
  height,
  loop = true,
  onReady,
}) => {
  const containerRef = useRef<HTMLDivElement>(null)
  const animRef = useRef<AnimationItem | null>(null)
  // Held in a ref so an inline `onReady={() => ...}` from a caller cannot land in
  // the dependency list and make every parent render destroy and rebuild the
  // animation — a rebuild loop shows as a clip that never settles or never paints.
  const onReadyRef = useRef(onReady)
  onReadyRef.current = onReady

  useEffect(() => {
    // Destroy any previous animation
    if (animRef.current) {
      animRef.current.destroy()
      animRef.current = null
    }

    const container = containerRef.current
    if (!container || !animationData) return

    // Empty the container before building. `destroy()` is supposed to do this,
    // but it only removes what it knows about: an instance torn down BEFORE its
    // SVG finished building (React runs mount effects twice in development, so
    // every clip gets a load/destroy/load cycle) can leave an orphan node behind,
    // and lottie then draws into a container that already has stale children.
    // Starting from an empty node makes the outcome independent of that race.
    container.replaceChildren()

    let parsed: unknown
    try {
      parsed = JSON.parse(animationData)
    } catch (e) {
      // A bad clip used to render as an EMPTY BOX with nothing anywhere -- no
      // throw, no log -- which is indistinguishable from a pack that simply has
      // no art for that slot. Leave a breadcrumb so the next such report is
      // diagnosable from the window's console instead of by elimination.
      // eslint-disable-next-line no-console
      console.error(
        '[mochi] lottie JSON parse failed',
        { bytes: animationData.length, head: animationData.slice(0, 40) },
        e,
      )
      return
    }

    let anim: AnimationItem
    try {
      // Before loading, not after: an expression throws during the build, so
      // there is no post-hoc recovery — see stripExpressions.
      const stripped = stripExpressions(parsed)
      if (stripped > 0) {
        // eslint-disable-next-line no-console
        console.warn(
          `[mochi] removed ${stripped} lottie expression(s) — they cannot run under ` +
            'the dashboard CSP (no unsafe-eval) and would render the clip blank',
        )
      }
      anim = lottie.loadAnimation({
        container,
        renderer: 'svg',
        loop,
        autoplay: true,
        animationData: parsed,
      })
    } catch (e) {
      // Same reasoning as above: lottie throwing here (unsupported feature,
      // malformed layer) must not be an invisible blank.
      // eslint-disable-next-line no-console
      console.error('[mochi] lottie loadAnimation failed', { bytes: animationData.length }, e)
      return
    }

    animRef.current = anim

    const handleReady = () => {
      // Loading can SUCCEED and still paint nothing — the clip is fine, the
      // container ends up empty. That combination has no error anywhere, so it
      // was previously indistinguishable from "this pack has no art for this
      // slot". Report the DOM outcome, not just the load outcome.
      const svg = container.querySelector('svg')
      const drawable = svg ? svg.querySelectorAll('path, image, rect, ellipse').length : 0
      if (drawable === 0) {
        // eslint-disable-next-line no-console
        console.error('[mochi] lottie loaded but painted nothing', {
          bytes: animationData.length,
          hasSvg: Boolean(svg),
          childNodes: container.childNodes.length,
          head: animationData.slice(0, 60),
        })
      }
      onReadyRef.current?.()
    }
    anim.addEventListener('DOMLoaded', handleReady)

    return () => {
      anim.removeEventListener('DOMLoaded', handleReady)
      anim.destroy()
      animRef.current = null
    }
  }, [animationData, loop])

  return (
    <div
      ref={containerRef}
      style={{ width, height }}
    />
  )
}

export const LottieRenderer = React.memo(LottieRendererInner)
