/**
 * The composer dock floats over the transcript scroller and the conversation
 * scrolls UNDER it (iOS toolbar layout); there is deliberately no opaque fade
 * band, so the dock's own `--glass-tint` (~40-45% opaque) plus its backdrop
 * blur are the ONLY thing that hides the covered strip -- pinned in
 * ChatPage.dockClearance.test.tsx.
 *
 * At frost 4 that occluder was too weak: a 4px blur leaves ~13px body glyphs
 * legible and the tint passes 55-60% of the backdrop, so a tall message
 * (Autopilot goal/stage injections are the reliable trigger) read straight
 * THROUGH the composer mid-scroll (#15225). The fix is scoped to the COMPOSER
 * alone -- it is the one surface that floats over a scrolling transcript. The
 * heavier blur must NOT reach the other `panel` surfaces (notification cards,
 * banner, the 12px-tall NotificationFeed stubs, the side-panel float, tip and
 * suggestion cards), where a heavy blur reads foggy on a short box -- exactly
 * the regime the chips stay at 4 to avoid. So it lives on its own variant, not
 * on the shared `panel`.
 *
 * Pinned against source text, like the sibling glass suites: RECIPE is
 * module-private and happy-dom has no layout for the primitive's ResizeObserver
 * to fire against, so the recipe the browser paints is read from the file.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const GLASS_SRC = readFileSync(resolve(process.cwd(), 'src/components/Glass.tsx'), 'utf-8')
const CHAT_INPUT_SRC = readFileSync(resolve(process.cwd(), 'src/components/ChatInput.tsx'), 'utf-8')

/** The frost (backdrop-blur px) a variant sets in the RECIPE map. */
function frostOf(variant: 'composer' | 'panel' | 'chip'): number {
  const m = new RegExp(`${variant}:\\s*\\{\\s*frost:\\s*(\\d+)`).exec(GLASS_SRC)
  expect(m, `RECIPE.${variant} frost not found`).not.toBeNull()
  return Number(m![1])
}

describe('composer glass occlusion (#15225)', () => {
  it('blurs the composer variant hard enough to smear body text past reading', () => {
    // The composer band is body-typography (~13px). A blur radius at least the
    // glyph height destroys the letterforms; 4px only softened them, which is
    // exactly what let a tall message read through the dock.
    expect(frostOf('composer')).toBeGreaterThanOrEqual(12)
  })

  it('leaves the other panel surfaces and the chips light, so the heavy blur is composer-only', () => {
    // The heavier blur must not reach a notification card or a 12px feed stub;
    // only the composer floats over a scrolling transcript.
    expect(frostOf('panel')).toBe(4)
    expect(frostOf('chip')).toBe(4)
    expect(frostOf('composer')).toBeGreaterThan(frostOf('panel'))
  })

  it('wires the composer dock to the composer variant', () => {
    // The one call site that gets the heavy blur is the dock, addressed by its
    // testid so this cannot silently point at some other pane.
    expect(CHAT_INPUT_SRC).toMatch(/<Glass\s+variant="composer"\s+radius=\{16\}\s+data-testid="composer-dock"/)
  })

  it('shares the light band across all three variants', () => {
    // The blur is the only difference; the band stays unified at 25.
    for (const v of ['composer', 'panel', 'chip'] as const) {
      const m = new RegExp(`${v}:\\s*\\{[^}]*lightIntensity:\\s*(\\d+)`).exec(GLASS_SRC)
      expect(m, `RECIPE.${v} lightIntensity not found`).not.toBeNull()
      expect(Number(m![1])).toBe(25)
    }
  })
})
