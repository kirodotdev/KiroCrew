import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

const src = () => readFile(join(__dirname, '..', 'pages', 'chat', 'ChatPageView.tsx'), 'utf8')

// A long session title pushed the trailing header controls off screen: measured at
// 320px, the external-link button was clipped at the edge and the pin and
// side-panel buttons were fully outside the viewport. `max-w-[50vw]` RESERVES half
// the viewport for the title no matter what sits beside it -- 160px at 320px, plus
// ~128px of leading icons, leaving ~32px for three buttons that need ~110px.
describe('session header at phone widths', () => {
  it('lets the title shrink instead of reserving half the viewport', async () => {
    const s = await src()
    expect(s, 'the display title must shrink, not reserve')
      .toMatch(/session-header-title[^"]*truncate min-w-0/)
    // The reservation is what broke the phone, so it is gone BELOW the breakpoint
    // only. Removing it on a desktop too would change an appearance the root cause
    // never implicated.
    // Anchored to the DISPLAY element specifically: the editing input carries the
    // same class prefix, so an unanchored match is satisfied by the wrong one.
    expect(s, 'the display title must keep its desktop cap')
      .toMatch(/session-header-title[^"]*truncate min-w-0 md:max-w-\[50vw\]/)
    expect(s, 'no unconditional reservation may remain')
      .not.toMatch(/session-header-title[^"]*[^:]max-w-\[50vw\]/)
  })

  it('lets the editing input shrink too', async () => {
    const s = await src()
    // `flex-none` plus a `size` attribute refuses to shrink at all.
    expect(s).toMatch(/session-header-title[^"]*min-w-0 flex-1 outline-none focus-ring md:max-w-\[50vw\]/)
    expect(s, 'flex-none would pin the input at its size attribute')
      .not.toMatch(/session-header-title[^"]*flex-none/)
  })

  it('keeps the trailing controls at full width', async () => {
    const s = await src()
    // These are the buttons that were pushed out; they must not absorb the deficit.
    expect(s).toMatch(/ml-auto flex shrink-0 items-center gap-1\.5 pointer-events-none/)
  })

  it('passes the shrink down every cluster between row and title', async () => {
    const s = await src()
    expect(s, 'the header row').toMatch(/group\/header flex min-w-0 items-stretch/)
    expect(s, 'the display cluster').toMatch(/cursor-text flex min-w-0 items-center/)
    expect(s, 'the clickable wrapper').toMatch(/<Clickable className="flex min-w-0 items-center gap-1"/)
    expect(s, 'the editing cluster').toMatch(/flex min-w-0 flex-1 items-center gap-1 px-1\.5/)
  })
})
