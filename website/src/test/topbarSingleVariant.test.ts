import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const css = async () => (await readFile(join(__dirname, '..', 'index.css'), 'utf8')).replace(/\/\*[\s\S]*?\*\//g, '')
const app = () => readFile(join(__dirname, '..', 'App.tsx'), 'utf8')

// The phone chat page's `topbar-single` header variant gives the CENTRE cell the
// remainder (`auto minmax(0,1fr) auto`) — the chat page's title slot has to take
// every pixel the two side cells leave, the opposite of the window-centred
// search the base grid serves. That inverts topbarMenuButtonNarrow's rule about
// `auto` side tracks, which holds because of ONE property: a side group that is
// an inline-size container has no content size to give an `auto` track and
// collapses to its padding. So the variant is only sound while its side cells
// are NOT `.tb-left` / `.tb-right`. Both halves are pinned here.
describe('topbar-single (phone chat page) header variant', () => {
  it('sizes the centre cell as the remainder inside the mobile media block', async () => {
    const s = await css()
    const rule = s.match(/@media \(max-width:767px\)\{[^{}]*\.topbar\.topbar-single\{([^}]*)\}/)
    expect(rule, 'expected the topbar-single rule inside the 767px media block').toBeTruthy()
    expect(rule![1]).toMatch(/grid-template-columns:auto minmax\(0,1fr\) auto/)
    // No gap: the leading cell is empty in the single-crew case, and a gap after
    // an empty cell would push the sessions toggle off the 16px page gutter.
    expect(rule![1]).toMatch(/gap:0/)
  })

  it('keeps the bell badge overhang reserve on the trailing cell', async () => {
    const s = await css()
    expect(s).toMatch(/\.topbar\.topbar-single \.tb-trail\{padding:6px 6px 0 0;margin:-6px -6px 0 0\}/)
  })

  it('never puts an inline-size-contained group in the variant\'s auto side tracks', async () => {
    const src = await app()
    // The three cells the single variant renders. Each is asserted by the marker
    // the tests and CSS address it by, and none may carry a container class.
    const lead = src.match(/data-testid="topbar-lead" className="([^"]*)"/)
    const slot = src.match(/id="mobile-topbar-slot" data-testid="mobile-topbar-slot" className="([^"]*)"/)
    const trail = src.match(/className="tb-trail ([^"]*)"/)
    expect(lead, 'leading cell').toBeTruthy()
    expect(slot, 'centre slot').toBeTruthy()
    expect(trail, 'trailing cell').toBeTruthy()
    for (const m of [lead!, slot!, trail!]) {
      expect(m[0]).not.toMatch(/tb-left|tb-right/)
    }
    // And `.tb-trail` must not have been turned into a container elsewhere.
    const s = await css()
    expect(s).not.toMatch(/\.tb-trail\{[^}]*container-type/)
  })
})
