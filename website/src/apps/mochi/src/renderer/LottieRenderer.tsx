/**
 * LottieRenderer — renders Lottie JSON animations via lottie-web.
 * Manages animation lifecycle: destroys old animation and loads new
 * when animationData changes. Notifies parent via onReady callback.
 */
import React, { useEffect, useRef } from 'react'
/*
 * The LIGHT player, deliberately — the same call crew-companion's renderer
 * makes, for the same reason. The package-main build compiles animation
 * expressions with a direct `eval()`, and the JSON reaching `loadAnimation`
 * here includes imported appearance packs — third-party authored — running in
 * the gateway's origin. Nothing mochi draws needs expressions (SVG renderer
 * only, expressions stripped below), so the smaller player removes the sink
 * rather than shipping it disarmed.
 *
 * Known tradeoff, accepted with eyes open: the light build also registers no
 * SVG EFFECT renderers (tint, drop shadow, blur, mattes…). The shipped clips
 * carry none (pinned in mochiLottieAssets.test.ts); an imported pack that
 * uses them still loads and draws, minus those effects, with a console
 * breadcrumb naming the cause (see countEffects). crew-companion accepted the
 * same tradeoff for the same third-party content class. The type-only import
 * stays on the package root: types live there, not under build/player/.
 */
import lottie from 'lottie-web/build/player/lottie_light'
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
 * This file imports the LIGHT player, which has no expression support at all —
 * the expression compiler (an `eval()` in the full build; lottie.js — search
 * `_expression_function`) simply is not shipped. The light player IGNORES an
 * expression rather than throwing, so without this strip a clip that leaned on
 * one would silently animate less, with nothing in the console naming the
 * cause. Stripping keeps the count in hand so the warning below can say
 * exactly what was dropped and why.
 *
 * The concrete history, from when this app still loaded the full player under
 * the dashboard CSP (`script-src 'self' 'unsafe-inline'`, no `'unsafe-eval'`):
 * the compiler's `eval` THREW mid-build and the slot painted nothing, which
 * made three of the four Kiro Ghost clips render as empty boxes while the
 * fourth (the only one with no expression) was fine: `idle`, `walking`,
 * `thinking`, `working` were blank and `error` / `offline` worked, which reads
 * like "the pack is broken" rather than "one feature is unavailable".
 *
 * Stripping loses NOTHING that could have run: the light player evaluates no
 * expression anyway. A clip whose motion depended on one animates less; a clip
 * whose expression was redundant (`loopOut()` over a track that already spans
 * the comp, which is what the shipped ghost used) is unchanged. Both beat
 * silent divergence.
 *
 * Loading the full player (or widening the CSP with `'unsafe-eval'`) to make
 * expressions work is the alternative and is rejected: it would wire an
 * attacker-authored pack's strings to a code-execution primitive in the
 * gateway's origin, to make a companion bob.
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

/**
 * Count effect-bearing nodes (a non-empty `ef` array) in a parsed clip.
 *
 * The light player registers NO SVG effect implementations (tint, fill,
 * stroke, drop shadow, gaussian blur, mattes, transform — nine classes in the
 * full build, zero here), so an effect-bearing pack draws with those effects
 * skipped. The full player did render them, which makes this the one visible
 * difference an imported pack can hit after the light-player switch — the
 * shipped clips carry none (pinned in mochiLottieAssets.test.ts). Nothing is
 * deleted and the clip still loads; this count only feeds the console
 * breadcrumb below, same philosophy as stripExpressions: degrade loudly,
 * never silently.
 */
function countEffects(node: unknown): number {
  if (Array.isArray(node)) {
    return node.reduce<number>((n, item) => n + countEffects(item), 0)
  }
  if (node !== null && typeof node === 'object') {
    const obj = node as Record<string, unknown>
    const here = Array.isArray(obj.ef) && obj.ef.length > 0 ? 1 : 0
    // A counted `ef` holds effect objects whose parameters are themselves `ef`
    // arrays — recursing into it would count one drop shadow as 2+. Prune at
    // the counted key so the breadcrumb's number means "effect-bearing nodes".
    return Object.entries(obj).reduce<number>(
      (n, [key, v]) => (here === 1 && key === 'ef' ? n : n + countEffects(v)),
      here,
    )
  }
  return 0
}

/** Clips (bytes + head, the pair the other logs key on) already warned about. */
const warnedEffectClips = new Set<string>()

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
      // Before loading, not after: `loadAnimation` consumes the parsed object,
      // so the strip must land first to count what was dropped — see
      // stripExpressions.
      const stripped = stripExpressions(parsed)
      if (stripped > 0) {
        // eslint-disable-next-line no-console
        console.warn(
          `[mochi] removed ${stripped} lottie expression(s) — the light lottie ` +
            'player has no expression support, so motion that depended on them ' +
            'will not play',
        )
      }
      const effectNodes = countEffects(parsed)
      if (effectNodes > 0) {
        // Once per distinct clip, not per load: unlike the expression warn
        // (which fires only when something was stripped), this fires for ANY
        // effect-bearing pack — and PetWidget swaps animationData on every pet
        // state change while GalleryPanel mounts one renderer per tile, which
        // would repeat an identical line until it crowds out the three
        // genuinely diagnostic messages this file exists to make findable.
        const clipKey = `${animationData.length}:${animationData.slice(0, 40)}`
        if (!warnedEffectClips.has(clipKey)) {
          warnedEffectClips.add(clipKey)
          // eslint-disable-next-line no-console
          console.warn(
            `[mochi] clip carries ${effectNodes} effect-bearing node(s) — the light ` +
              'lottie player ships no SVG effect renderers, so they will draw ' +
              'without those effects',
          )
        }
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
