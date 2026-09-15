/** The docked-to-expanded transition is a shared element, not a swap.
 *
 * `website/AGENTS.md` states the rule this pins: a persistent element that
 * changes form or place stays ONE element, never `flag ? <Chip/> : <Card/>`, and
 * the UX Review lane blocks a hard swap. That rule is invisible to a rendering
 * test here, because Framer Motion does not put `layoutId` in the DOM -- so these
 * assertions read the source.
 *
 * Reading source makes them defeatable by WRAPPING rather than deletion: a guard
 * kept as dead code still contains its own text. Each assertion therefore anchors
 * on the whole expression it cares about, and the suite is accompanied by a
 * wrapping mutation in review rather than a deletion one.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = readFileSync(
  join(__dirname, '..', 'pages', 'members', 'CrewWebview.tsx'),
  'utf8',
)

describe('CrewWebview docked-to-expanded transition', () => {
  it('carries one shared id across both surfaces', () => {
    // Declared once, so the two uses cannot drift into different strings.
    expect(SRC).toMatch(/^const SURFACE_LAYOUT_ID = "crew-webview-surface";$/m)
    // The docked surface holds it unconditionally: while docked it is the only
    // one of the two that is visible.
    expect(SRC).toMatch(/^\s*layoutId=\{SURFACE_LAYOUT_ID\}$/m)
    // The expanded surface holds it only WHILE expanded. Unconditional here
    // would put two elements on one id -- that subtree stays mounted through a
    // collapse to keep its single-use document -- and Framer Motion would have
    // no single element to animate.
    expect(SRC).toMatch(/^\s*layoutId=\{expanded \? SURFACE_LAYOUT_ID : undefined\}$/m)
  })

  it('keeps the Contained bar continuous as the landing spot', () => {
    expect(SRC).toMatch(/^const CONTAINED_BAR_LAYOUT_ID = "crew-webview-contained-bar";$/m)
    expect(SRC).toMatch(/^\s*layoutId=\{CONTAINED_BAR_LAYOUT_ID\}$/m)
    expect(SRC).toMatch(/^\s*layoutId=\{expanded \? CONTAINED_BAR_LAYOUT_ID : undefined\}$/m)
  })

  it('drops the motion under prefers-reduced-motion without dropping the continuity', () => {
    expect(SRC).toContain('useReducedMotion')
    // Zero duration, NOT a removed layoutId: taking the ids away under the
    // preference would restore the hard swap the rule forbids.
    expect(SRC).toMatch(/reducedMotion\s*\n?\s*\?\s*\{ duration: 0 \}/)
    // And the ids are outside that branch, which is what makes the sentence
    // above true rather than merely intended.
    const reducedBranch = SRC.slice(
      SRC.indexOf('const surfaceTransition'),
      SRC.indexOf('const { theme'),
    )
    expect(reducedBranch).not.toContain('SURFACE_LAYOUT_ID')
  })

  it('animates through motion elements rather than plain divs', () => {
    expect(SRC).toMatch(/^import \{ motion, useReducedMotion \} from "framer-motion";$/m)
    // The rule also forbids new CSS keyframes for this, so a transition smuggled
    // in as one would satisfy every assertion above while breaking the rule.
    expect(SRC).not.toContain('@keyframes')
  })
})
