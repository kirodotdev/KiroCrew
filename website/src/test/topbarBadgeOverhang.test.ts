import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const read = (f: string) => readFile(join(__dirname, '..', f), 'utf8')
// Strip CSS comments before matching: the rule is explained in prose that quotes
// the very declarations being asserted, and a raw-text match hits the comment.
const css = async () => (await read('index.css')).replace(/\/\*[\s\S]*?\*\//g, '')
// `.tb-right{` also occurs as a substring of the shared `.tb-left,.tb-right{...}`
// rule, whose `overflow:hidden` this gutter exists to live with. An unanchored
// match reads the wrong rule, so anchor on the declaration only this one carries.
const TB_RIGHT_RULE = /\.tb-right\{justify-content:flex-end[^}]*\}/

// The unread badge sits 4px past the bell button's top-right corner, and the bell
// is the last child of a `justify-content:flex-end` group whose box ends at the
// header's padding edge — so the badge overhangs the group's clip box (its padding
// box) by exactly 4px on the right and 4px on the top, and `overflow:hidden`
// clipped both, squaring the badge off and cutting into a two-digit count.
// The gutter reserves that overhang inside the clip box and the equal negative
// margin puts the outer box back, so nothing moves. Not viewport-specific — the
// overhang is 4px at 320, 390 and 1280px alike.
describe('topbar unread badge overhang', () => {
  it('reserves the overhang inside the clip box without moving the group', async () => {
    const m = (await css()).match(TB_RIGHT_RULE)
    expect(m, 'expected the .tb-right rule').not.toBeNull()
    expect(m![0]).toMatch(/padding:4px 4px 0 0/)
    // Equal and opposite: the padding grows the clip box, the margin gives back
    // the space, so the bell stays put and the header keeps its height.
    expect(m![0]).toMatch(/margin:-4px -4px 0 0/)
  })

  it('does not depend on overflow-clip-margin, which WebKit does not implement', async () => {
    // The engine-dependent spelling of this fix (`overflow:clip` +
    // `overflow-clip-margin`) is a no-op on every iOS browser, which is exactly
    // the class of device the defect was reported from. Keep it out.
    const s = await css()
    expect(s).not.toMatch(/overflow-clip-margin/)
    expect(s.match(TB_RIGHT_RULE)![0]).not.toMatch(/overflow/)
  })

  it('keeps the shared clip on the group, so its contents cannot cross the centre track', async () => {
    // The gutter widens the clip box by 4px; it does not remove the clip.
    const s = await css()
    expect(s).toMatch(/\.tb-left,\.tb-right\{container-type:inline-size[^}]*overflow:hidden\}/)
  })

  it('keeps the gutter equal to the badge offset it exists to admit', async () => {
    // The two files carry one number between them. Moving the badge further out
    // without widening the gutter silently re-clips it, which is how this shipped.
    const app = await read('App.tsx')
    const badge = app.match(/className="absolute (-top-\S+) (-right-\S+)[^"]*"/)
    expect(badge, 'expected the bell badge span').not.toBeNull()
    // Tailwind spacing: `-top-1` / `-right-1` are 0.25rem = 4px.
    expect(badge![1]).toBe('-top-1')
    expect(badge![2]).toBe('-right-1')
    const rule = (await css()).match(TB_RIGHT_RULE)![0]
    // `0` is written without a unit, as CSS allows and this file does elsewhere.
    const four = /(-?\d+)(?:px)? (-?\d+)(?:px)? (-?\d+)(?:px)? (-?\d+)(?:px)?/
    const pad = rule.match(new RegExp('padding:' + four.source))
    const mar = rule.match(new RegExp('margin:' + four.source))
    expect(pad, 'expected the gutter padding').not.toBeNull()
    expect(mar, 'expected the compensating margin').not.toBeNull()
    // top and right only — the badge overhangs nowhere else.
    expect([pad![1], pad![2], pad![3], pad![4]]).toEqual(['4', '4', '0', '0'])
    expect([mar![1], mar![2], mar![3], mar![4]]).toEqual(['-4', '-4', '0', '0'])
  })
})
