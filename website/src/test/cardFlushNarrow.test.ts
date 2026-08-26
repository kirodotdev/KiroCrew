import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

// Comments stripped before matching: the rationale below quotes the very class names
// being asserted against, and a raw negative match would hit the prose instead.
const src = async (...seg: string[]) => {
  const raw = await readFile(join(__dirname, '..', 'pages', ...seg), 'utf8')
  return raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

const PANE_FILES = ['SkillsTab.tsx', 'SteeringTab.tsx']

describe('capability panes reach the card edge below the breakpoint', () => {
  // The pane/Card inset pairing and the 'Card keeps SOME inset' assertion moved to
  // cardInsetYield.test.tsx, which READS Card's inset from a render instead of from
  // ui.tsx's source. The source regex here silently stopped matching the moment that
  // inset moved into a variable, so it was pinning nothing.

  it('keeps the pane row its OWN padding, which is load-bearing', async () => {
    // The rows inside these panes are NOT the shape page-layout.md gates: an
    // unpadded bordered pane sits between them and the card, so their px-4 is the
    // only gutter they have. Gating it would put the text against a visible border.
    for (const file of PANE_FILES) {
      const s = await src('overview', file)
      expect(s).toMatch(/px-4 py-2\.5 border-b/)
      expect(s, 'gating this row leaves its text flush against the pane border')
        .not.toMatch(/md:px-4 py-2\.5 border-b/)
    }
  })

  it('gives the skills header buttons their own line while narrow', async () => {
    // Measured at 390px: the three buttons need 343px plus 16px of gaps against a
    // 358px row shared with the title and the info icon, so every label wrapped and
    // the row stood 50px tall. Stacked below md each button is one line (358x30).
    const s = await src('overview', 'SkillsTab.tsx')
    // The header's TOP margin is deliberately not pinned here: the pane that
    // hosts this tab owns the gap under the tab strip, and
    // SidePanelLayout.narrowPaneTopInset.test.tsx asserts this heading carries no
    // margin of its own. Pinning `mt-4` in this row's literal would make the two
    // tests contradict each other over a spacing token that is not this test's
    // subject — which is whether the three buttons get their own line.
    expect(s).toMatch(/mb-2 flex flex-wrap items-center gap-2/)
    expect(s).toMatch(
      /className="w-full md:w-auto md:ml-auto flex flex-col md:flex-row items-stretch md:items-center/)
    expect(s, 'ml-auto without a width switch keeps the buttons on the title line')
      .not.toMatch(/<span className="ml-auto flex items-center gap-2">/)
  })

  it('leaves the desktop header on a single row', async () => {
    // Verified at 1280px: one row, buttons at natural width (137/93/122),
    // right-aligned, and the card keeps its full 20px inset there.
    const s = await src('overview', 'SkillsTab.tsx')
    expect(s).toMatch(/md:flex-row/)
    expect(s).toMatch(/md:ml-auto/)
    expect(s, 'an ungated flex-col would stack the buttons on desktop too')
      .not.toMatch(/className="w-full flex flex-col items-stretch/)
  })
})
