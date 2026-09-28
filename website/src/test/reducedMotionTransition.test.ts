import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * The reduced-motion transition rule must not MANUFACTURE transitions.
 *
 * `transition-property` defaults to `all`, so a non-zero `transition-duration`
 * on `*` gives a transition to every animatable property of every element —
 * including elements that declare none. A transition samples the OLD value on
 * its first frame, so from then on an element's USED value trails its SPECIFIED
 * value by one frame.
 *
 * The chat transcript paid for that. The virtualizer writes a spacer height in
 * the SAME commit that mounts a row, so with a manufactured height transition
 * the new row and the space still reserved for it both counted for one frame:
 * `scrollHeight` jumped by that row's own height and Chromium's scroll
 * anchoring shoved `scrollTop` by up to 602px before releasing it the next
 * frame. A frame-resolution browser probe measured 0 of those events with
 * motion enabled and 4-6 per scroll gesture under `reduce`.
 *
 * The widely-copied snippet this rule came from uses `0.01ms` so that
 * `transitionend` still fires for JS that waits on it. Nothing in this app
 * waits on it under reduced motion: PinnedPrompt owns the only `transitionend`
 * listener and returns before installing one when the preference is set. That
 * is asserted here too, because it is the single fact that makes `0s` safe.
 */
/**
 * index.css with every comment removed.
 *
 * Load-bearing: these rules carry long comments that quote the very declarations
 * asserted below ("transition-duration: 0s !important", ".vc-spacer-skeleton"),
 * so a scan of the raw file matches prose and reports whatever the comment says
 * rather than what the stylesheet does. Comments are stripped first so every
 * assertion reads real declarations only.
 */
const CSS = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf-8')
  .replace(/\/\*[\s\S]*?\*\//g, ' ')

/** The `prefers-reduced-motion` block that targets the universal selector. */
function universalReducedMotionBlocks(): string[] {
  const out: string[] = []
  const re = /@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{/g
  for (let m = re.exec(CSS); m; m = re.exec(CSS)) {
    // Walk braces so a nested rule cannot truncate the block early.
    let depth = 1
    let i = re.lastIndex
    for (; i < CSS.length && depth > 0; i++) {
      if (CSS[i] === '{') depth++
      else if (CSS[i] === '}') depth--
    }
    const body = CSS.slice(re.lastIndex, i - 1)
    if (/(^|[,\s{])\*[\s,{]/.test(body)) out.push(body)
  }
  return out
}

describe('prefers-reduced-motion does not manufacture transitions', () => {
  it('has a universal reduced-motion block to assert against', () => {
    // Guards the rest of this file: a rename or a refactor that moved the rule
    // would otherwise make every assertion below vacuously true.
    expect(universalReducedMotionBlocks().length).toBeGreaterThan(0)
  })

  it('zeroes transition-duration outright rather than clamping it to a tiny value', () => {
    for (const body of universalReducedMotionBlocks()) {
      const decls = body.match(/transition-duration\s*:\s*[^;}]+/g) ?? []
      for (const decl of decls) {
        const value = decl.split(':')[1].trim()
        // `0`, `0s` and `0ms` all mean no transition starts. Anything else —
        // `0.01ms`, `1ms`, `0.001s` — is a real transition and reintroduces the
        // one-frame used-value lag this rule exists to avoid.
        expect(value.replace(/\s*!important$/, '')).toMatch(/^0(s|ms)?$/)
      }
    }
  })

  it('never gives the virtualizer spacers a transition', () => {
    // The spacer's height IS layout, not decoration: it is the reserved space
    // for unmounted rows, written in the same commit that mounts one. Any
    // transition on it re-opens the double-count regardless of the rule above.
    const spacerRules = CSS.match(/\.vc-spacer-skeleton[^{]*\{[^}]*\}/g) ?? []
    for (const rule of spacerRules) {
      const decls = rule.match(/transition[^:]*:\s*([^;}]+)/g) ?? []
      for (const decl of decls) {
        expect(decl).toMatch(/:\s*none|:\s*0(s|ms)?\s*(!important)?$/)
      }
    }
  })

  it('keeps the animation-duration clamp, which is NOT the same hazard', () => {
    // `animation-name` defaults to `none`, so a duration alone cannot create an
    // animation where none was declared. Documented here so a future
    // "consistency" pass does not read the two rules as one and change both.
    expect(CSS).toMatch(/prefers-reduced-motion:reduce\)\{[^}]*animation-duration:0\.01ms !important/)
  })

  it('leaves no transitionend listener depending on a transition firing under reduced motion', () => {
    // `0s` means no transition starts, so no `transitionend` is dispatched. That
    // is safe only while the sole listener is unreachable under the preference.
    const pinned = readFileSync(resolve(process.cwd(), 'src/pages/chat/PinnedPrompt.tsx'), 'utf-8')
    const guardAt = pinned.indexOf('if (reducedMotion) return')
    const listenerAt = pinned.indexOf("addEventListener('transitionend'")
    expect(guardAt).toBeGreaterThan(-1)
    expect(listenerAt).toBeGreaterThan(-1)
    // The guard must come FIRST: the listener is installed only on the path the
    // preference short-circuits.
    expect(guardAt).toBeLessThan(listenerAt)
  })
})
