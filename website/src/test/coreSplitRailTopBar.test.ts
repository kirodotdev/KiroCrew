/**
 * The two CORE pages carrying a desktop left/right split — /projects (Task
 * Runner) and /webhooks — must hand the information pane the FULL viewport width
 * on a phone. Both already drill down (the rail opens full-width, picking an
 * entry collapses it), so what was left was the AXIS of the collapsed state: a
 * strip down the side keeps spending the one dimension a phone has none of.
 *
 * This is a body-text rule in every language, not a CJK one — the measure is how
 * much width the prose column gets. Script changes only the symptom: Latin
 * refuses to break below its longest word and overflows, while scripts that break
 * per character collapse into a ribbon at the same width and report NO overflow,
 * which is why `scrollWidth` cannot be the test.
 *
 * Measured at 390px: /projects 48x802 strip -> 390x46 bar, content 342 -> 390px;
 * /webhooks 44x750 strip -> 390x47 bar, content 346 -> 390px. Desktop unchanged
 * at 260px / 300px rails.
 *
 * Asserted over source: these are declaration-level facts (which element flips
 * direction, whether the reserved width is released) and jsdom performs no
 * layout, so a render could not measure the widths this is really about.
 */
import { describe, it, expect } from 'vitest'

async function src(path: string): Promise<string> {
  return (await import(`../pages/${path}?raw`)).default as string
}

const PAGES = ['ProjectsPage.tsx', 'WebhooksPage.tsx'] as const

describe('core left/right splits at narrow widths', () => {
  it('derives the bar from narrow AND collapsed, on both pages', async () => {
    for (const page of PAGES) {
      const s = await src(page)
      // Both conditions in ONE expression. Narrow alone would put a bar on an
      // expanded rail; collapsed alone would leak it onto a desktop, spending a
      // whole row to recover width that was never scarce there.
      const decl = s.match(/const railBar = [^\n]*/)
      expect(decl, `${page}: expected a railBar declaration`).not.toBeNull()
      expect(decl![0], `${page}: railBar must be narrow-gated`).toContain('isMobile')
      expect(decl![0], `${page}: railBar must require the collapsed state`)
        .toContain('rail.collapsed')
    }
  })

  it('flips the shell to a column so the bar sits ABOVE the pane', async () => {
    for (const page of PAGES) {
      const s = await src(page)
      // Without the direction flip the bar is just a wide strip still sitting in
      // a row — it would take the whole width AND leave the pane beside it.
      expect(s, `${page}: expected the shell to stack while the bar is up`)
        .toMatch(/\$\{railBar \? 'flex-col' : ''\}/)
    }
  })

  it('releases the reserved rail width while the bar is up', async () => {
    // The collapsed rail's `width` is what reserved the column. A bar that keeps
    // it would still be holding the strip's space, defeating the whole change.
    const projects = await src('ProjectsPage.tsx')
    expect(projects).toMatch(/<CollapsedRail[^>]*horizontal=\{railBar\}/s)
    const webhooks = await src('WebhooksPage.tsx')
    expect(webhooks).toMatch(/width: railBar \? undefined :/)
  })

  it('keeps the vertical form for the desktop', async () => {
    // The side-by-side layout is correct where width is abundant; this change is
    // additive, so the vertical branch must still exist on both pages.
    const projects = await src('ProjectsPage.tsx')
    expect(projects, 'ProjectsPage: expected the rotated label to survive')
      .toContain("writingMode: 'vertical-rl'")
    const webhooks = await src('WebhooksPage.tsx')
    expect(webhooks, 'WebhooksPage: expected the side border on the vertical form')
      .toMatch(/flex-col border-r border-border/)
  })

  it('makes the whole bar the control, not a lone icon in a wide strip', async () => {
    // A single icon adrift in a full-width bar does not read as tappable. Both
    // bars carry the panel glyph trailing the row, and the row itself is the
    // button — reusing the existing expand label, so no new string.
    //
    // The branch marker differs by page: ProjectsPage passes `horizontal` into its
    // own CollapsedRail, WebhooksPage switches inline on `railBar`.
    const cases: [string, string, RegExp][] = [
      ['ProjectsPage', await src('ProjectsPage.tsx'), /if \(horizontal\) \{[\s\S]*?\n  \}/],
      ['WebhooksPage', await src('WebhooksPage.tsx'), /\{railBar \? \([\s\S]*?\) : \(/],
    ]
    for (const [page, s, re] of cases) {
      const bar = s.match(re)
      expect(bar, `${page}: expected a horizontal bar branch`).not.toBeNull()
      expect(bar![0], `${page}: the bar needs a panel affordance`).toContain('PanelLeftOpen')
      // The shared primitive, never a raw element: AUTOSDE's page-layout-pattern
      // (blocking) requires `Btn` on dashboard pages. The vertical branch predates
      // the rule and is tolerated; a NEW raw button would be that row growing.
      expect(bar![0], `${page}: the bar must use the Btn primitive`).toContain('<Btn')
      expect(bar![0], `${page}: no raw button element in the bar`).not.toMatch(/<button\b/)
    }
  })

  it('names the pane in the Webhooks bar, not just an icon', async () => {
    // The page title above the bar already says "Webhooks", so a lone webhook
    // icon carries no new meaning — and this bar is the ONLY route back to the
    // section list once selecting a row auto-collapses the rail. `leaf` is the
    // page's existing human label for the open pane, so this costs no new string.
    const s = await src('WebhooksPage.tsx')
    const bar = s.match(/\{railBar \? \([\s\S]*?\) : \(/)
    expect(bar, 'expected a railBar branch').not.toBeNull()
    expect(bar![0], 'the bar must name the open pane').toContain('{leaf}')
  })
})
