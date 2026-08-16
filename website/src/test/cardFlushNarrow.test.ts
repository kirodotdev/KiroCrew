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
  it('pulls the pane out by EXACTLY the card inset it has to cancel', async () => {
    // The two numbers are one number. The negative margin exists only to undo the
    // card's own horizontal inset, so changing the inset without changing the margin
    // pushes the pane past the border (or leaves it short of it) -- which is why this
    // asserts the relationship, not two independent literals.
    for (const file of PANE_FILES) {
      const s = await src('overview', file)
      const pane = s.match(/const PANE_SHELL_CLASS = 'flex gap-3 max-md:-mx-([\d.]+)/)
      expect(pane, `${file} should full-bleed the pane shell below md`).toBeTruthy()
      const cards = [...s.matchAll(/<Card className="max-md:px-([\d.]+)">/g)].map(m => m[1])
      expect(cards.length, `${file} should have at least one inset-halved card`)
        .toBeGreaterThan(0)
      for (const inset of cards) {
        expect(inset, `${file}: pane -mx-${pane![1]} must match card px-${inset}`)
          .toBe(pane![1])
      }
    }
  })

  it('keeps the card SOME horizontal inset -- the toolbar shares it', async () => {
    // Measured at 390px: removing it entirely put the search field's rounded border
    // flush against the card's border (0px gap). Halved reads as 10px, which is the
    // shipped value; zero is the defect.
    for (const file of PANE_FILES) {
      const s = await src('overview', file)
      expect(s, 'a flushed card takes the filter row gutter with it')
        .not.toMatch(/<Card className="max-md:px-0">/)
    }
  })

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
    expect(s).toMatch(/mt-4 mb-2 flex flex-wrap items-center gap-2/)
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
