import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const raw = () => readFile(join(__dirname, '..', 'index.css'), 'utf8')
// Strip CSS comments before matching: the rules below are explained in prose that
// quotes the very selectors being asserted, and a raw-text match hits the comment.
const css = async () => (await raw()).replace(/\/\*[\s\S]*?\*\//g, '')

// The mobile menu button is the ONLY route to the nav on a phone. It rendered
// `display:block`, `visibility:visible`, 36x36 -- and was still invisible: its
// group is an inline-size container, so an `auto` identity track could not read a
// content size and collapsed to the group's 16px padding, leaving
// `overflow:hidden` to clip the button to an 8px sliver. Measured tracks:
// `16px 28px 298px`; with the track sized by the window instead, `52px 28px 262px`
// and the button is fully shown. The guard is therefore on the TRACK, not on a
// containment override: a side track may never be `auto` while its group is a
// size container.
describe('topbar identity track at phone widths', () => {
  it('never asks an inline-size container for a content size', async () => {
    const s = await css()
    // Every `.topbar` track list in the sheet, narrow rung included.
    const lists = [...s.matchAll(/\.topbar\{[^}]*grid-template-columns:([^;}]+)/g)].map(m => m[1].trim())
    expect(lists.length, 'expected the base track list plus the narrow rung').toBeGreaterThan(1)
    for (const cols of lists) {
      const parts: string[] = []
      let depth = 0
      let cur = ''
      for (const ch of cols) {
        if (ch === '(') depth++
        if (ch === ')') depth--
        if (ch === ' ' && depth === 0) { if (cur) parts.push(cur); cur = '' } else cur += ch
      }
      if (cur) parts.push(cur)
      expect(parts, `expected three tracks in "${cols}"`).toHaveLength(3)
      // Sides only. The centre track's item is a plain button, so `auto` is fine
      // there and is what keeps the icon-only trigger from reserving 240px.
      expect([parts[0], parts[2]], `side track sized by content in "${cols}"`)
        .toEqual(['minmax(0,1fr)', 'minmax(0,1fr)'])
    }
  })

  it('keeps both side groups contained, since the actions collapse ladder queries one', async () => {
    const s = await css()
    expect(s).toMatch(/\.tb-left,\.tb-right\{container-type:inline-size/)
    // No rung may re-introduce a content-sized side track by turning containment
    // off for one group instead.
    expect(s).not.toMatch(/container-type:\s*normal/)
    // A rung targets a DESCENDANT of a group, never the group's own box: a
    // container cannot query itself, so such a rule would silently never apply.
    const rungs = s.match(/@container[^{]*\{[^}]*\}/g) || []
    expect(rungs.length, 'expected the container-query rungs to still exist').toBeGreaterThan(3)
    for (const r of rungs) {
      expect(r, `rung targets a group's own box: ${r}`).not.toMatch(/\{\s*\.tb-(left|right)\s*\{/)
    }
  })
})
