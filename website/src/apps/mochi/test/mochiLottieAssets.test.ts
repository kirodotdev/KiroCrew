/**
 * Lottie clips must be free of EXPRESSIONS to render fully.
 *
 * Mochi loads the LIGHT lottie player (see renderer/LottieRenderer.tsx), which
 * ships no expression support at all — an expression is ignored, so motion that
 * depended on one silently does not play. Historically, when this app still
 * loaded the full player, it was worse: the expression compiler's `eval()`
 * threw under the dashboard CSP (`script-src 'self' 'unsafe-inline'`, no
 * `'unsafe-eval'`) mid-build and the slot painted nothing. Three of the four
 * built-in Kiro Ghost clips carried a redundant `loopOut()` and were therefore
 * invisible, while the one without an expression rendered fine; the pack read
 * as "broken" rather than "one Lottie feature is unavailable". Either way —
 * throw then, ignore now — an expression in a shipped clip is a defect.
 *
 * Two pins here: the SHIPPED assets stay expression-free (so this cannot regress
 * when the art is re-exported from After Effects, which adds `loopOut()` freely),
 * and the strip is lossless for the ghost specifically — its `loopOut()` sat on a
 * track that already spans the whole composition, and the renderer already passes
 * `loop: true`.
 */
import { describe, expect, it } from 'vitest'

import { builtinPackDetail } from '../builtinPacks'

import ghostIdle from '../assets/animations/kiro_idle_mid.json'
import ghostIdleBlink from '../assets/animations/kiro_idle_mid_blink.json'
import ghostFlying from '../assets/animations/kiro_flying.json'
import ghostStaticBlink from '../assets/animations/kiro_static_blink.json'

const CLIPS: [string, unknown][] = [
  ['kiro_idle_mid', ghostIdle],
  ['kiro_idle_mid_blink', ghostIdleBlink],
  ['kiro_flying', ghostFlying],
  ['kiro_static_blink', ghostStaticBlink],
]

/** Every `x` whose value is a STRING — Lottie's expression convention. */
function findExpressions(node: unknown, path = ''): string[] {
  if (Array.isArray(node)) {
    return node.flatMap((item, i) => findExpressions(item, `${path}[${i}]`))
  }
  if (node !== null && typeof node === 'object') {
    const obj = node as Record<string, unknown>
    const here = typeof obj.x === 'string' ? [`${path}.x`] : []
    return [
      ...here,
      ...Object.entries(obj).flatMap(([k, v]) => findExpressions(v, `${path}.${k}`)),
    ]
  }
  return []
}

/** Every `x` whose value is NOT a string — a coordinate or an easing handle. */
function countDataXKeys(node: unknown): number {
  if (Array.isArray(node)) {
    return node.reduce<number>((n, item) => n + countDataXKeys(item), 0)
  }
  if (node !== null && typeof node === 'object') {
    const obj = node as Record<string, unknown>
    const here = 'x' in obj && typeof obj.x !== 'string' ? 1 : 0
    return Object.values(obj).reduce<number>((n, v) => n + countDataXKeys(v), here)
  }
  return 0
}

/** Every node carrying a non-empty `ef` array — a Lottie SVG effect. */
function findEffects(node: unknown, path = ''): string[] {
  if (Array.isArray(node)) {
    return node.flatMap((item, i) => findEffects(item, `${path}[${i}]`))
  }
  if (node !== null && typeof node === 'object') {
    const obj = node as Record<string, unknown>
    const here = Array.isArray(obj.ef) && obj.ef.length > 0 ? [`${path}.ef`] : []
    return [
      ...here,
      ...Object.entries(obj).flatMap(([k, v]) => findEffects(v, `${path}.${k}`)),
    ]
  }
  return []
}

describe('built-in Lottie packs', () => {
  it.each(CLIPS)('%s carries no expressions', (_name, clip) => {
    expect(findExpressions(clip)).toEqual([])
  })

  it.each(CLIPS)('%s carries no SVG effects', (_name, clip) => {
    // The light player ships no SVG effect renderers, so an effect on a
    // shipped clip would render skipped — the one visible regression class the
    // light-player switch knowingly accepts for IMPORTED packs must never
    // apply to the built-in art. After Effects adds effects as freely as it
    // adds `loopOut()`, so pin it against re-exports.
    expect(findEffects(clip)).toEqual([])
  })

  it.each(CLIPS)('%s keeps its non-expression x keys', (_name, clip) => {
    // The strip deletes a string `x` only. A numeric/array/object `x` is a
    // bezier handle or a split-position coordinate — deleting those would
    // corrupt every keyframe in the file, so prove they survived. Asserted
    // structurally: a clip built from hold keyframes has no bezier `x` at all,
    // so grepping for a literal `"x":[` is not a property of a healthy clip.
    expect(countDataXKeys(clip)).toBeGreaterThan(0)
  })

  it.each(CLIPS)('%s loops on its own: the track spans the composition', (_name, clip) => {
    // Why dropping `loopOut()` was lossless rather than a behaviour change.
    const doc = clip as { op: number; layers: { ks?: { p?: { y?: { k?: unknown } } } }[] }
    const tracks = doc.layers
      .map((l) => l.ks?.p?.y)
      .filter((p): p is { k: { t?: number; s?: number[] }[] } => Array.isArray(p?.k))
    for (const track of tracks) {
      const keys = track.k
      const last = keys[keys.length - 1]
      expect(last.t).toBe(doc.op)
      expect(last.s).toEqual(keys[0].s)
    }
  })
})

describe('pack-level flipX', () => {
  it('the ghost declares its baseline facing; the cat does not', () => {
    // The delivered ghost clips are mirrored. Declaring it on the PACK (rather
    // than compensating at each render site) is what lets the pet XOR it against
    // walk direction and edge-peeking instead of fighting them.
    expect(builtinPackDetail('kiro-ghost')?.flipX).toBe(true)
    expect(builtinPackDetail('default-mochi')?.flipX).toBeUndefined()
  })

  it('is absent, not false, when the art faces the normal way', () => {
    // Kept absent so the wire format matches the store's convention of omitting
    // optional keys — and so `flipX ?? sprite?.flipX` still falls through to a
    // sprite sheet's own flag rather than being shadowed by an explicit `false`.
    expect('flipX' in (builtinPackDetail('default-mochi') as object)).toBe(false)
  })
})
