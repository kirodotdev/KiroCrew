import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const raw = () => readFile(join(__dirname, '..', 'index.css'), 'utf8')
// Strip CSS comments before matching: the rules below are explained in prose that
// quotes the very selectors being asserted, and a raw-text match hits the comment.
const css = async () => (await raw()).replace(/\/\*[\s\S]*?\*\//g, '')

// The mobile menu button is the ONLY route to the nav on a phone. It rendered
// `display:block`, `visibility:visible`, 36x36 -- and was still invisible: its
// group is an inline-size container, so the narrow `auto` identity track could
// not read a content size and collapsed to the group's 16px padding, leaving
// `overflow:hidden` to clip the button to an 8px sliver. Measured tracks:
// `16px 28px 298px`. With containment off: `52px 28px 262px`, button fully shown.
describe('topbar identity track at phone widths', () => {
  it('does not ask an inline-size container for a content size', async () => {
    const s = await css()
    const m = s.match(/@media \(max-width:767px\)\{\s*\.tb-left\{container-type:normal\}\s*\}/)
    expect(m, 'expected the narrow override that lets the auto track measure the button').not.toBeNull()
  })

  it('keeps the override AFTER the unconditional containment declaration', async () => {
    const s = await css()
    // Same specificity: source order decides. Placed before, the override is dead.
    const decl = s.indexOf('.tb-left,.tb-right{container-type:inline-size')
    const override = s.indexOf('.tb-left{container-type:normal}')
    expect(decl, 'expected the containment declaration').toBeGreaterThan(-1)
    expect(override, 'expected the override').toBeGreaterThan(-1)
    expect(override).toBeGreaterThan(decl)
  })

  it('leaves the actions group contained, since its collapse ladder queries it', async () => {
    const s = await css()
    expect(s).toMatch(/\.tb-left,\.tb-right\{container-type:inline-size/)
    // Every rung targets a descendant of .tb-right; none targets .tb-left.
    const rungs = s.match(/@container[^{]*\{[^}]*\}/g) || []
    expect(rungs.length, 'expected the container-query rungs to still exist').toBeGreaterThan(3)
    for (const r of rungs) expect(r).not.toMatch(/\.tb-left\b/)
  })
})
