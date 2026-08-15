/**
 * Spec Builder had NO narrow-viewport awareness at all — no `useIsMobile`, no
 * `whenNarrow`, no `sm:` anywhere — so on a phone its rail kept the full desktop
 * width and the detail split what was left. Measured at 390px before this: rail
 * 250px, chat column 69px, document column 59px, every word of the requirements
 * on its own line with the right edge cut off.
 *
 * Two levels, because fixing only the shell would leave the reading column
 * squeezed by the detail's OWN split — the same defect one layer down:
 *
 *   1. shell — the collapsed rail lies across the TOP (bar), the expanded rail
 *      takes the whole viewport, and picking a spec collapses it (drill-down);
 *   2. detail — the document column steps aside so the chat owns the full width,
 *      and the document stays reachable through the fullscreen review overlay
 *      the app already had, opened from the chat header.
 *
 * This is a body-text rule in every language, not a CJK one: the measure is how
 * much width the prose column gets. Script only changes the symptom — Latin
 * overflows and clips, per-character-breaking scripts collapse into a ribbon with
 * no overflow at all, which is why `scrollWidth` cannot be the test. It read
 * 390px both before and after.
 *
 * Asserted over source: declaration-level facts, and jsdom performs no layout.
 */
import { describe, it, expect } from 'vitest'

async function src(path: string): Promise<string> {
  return (await import(`../apps/spec-builder/components/${path}?raw`)).default as string
}

describe('Spec Builder at narrow widths', () => {
  it('opts the rail into its collapsed form while narrow', async () => {
    const s = await src('Workspace.tsx')
    const cfg = s.match(/const RAIL_COLLAPSE: CollapseConfig = \{[\s\S]*?\n\}/)
    expect(cfg, 'expected a module-level RAIL_COLLAPSE').not.toBeNull()
    // Without this the rail simply keeps its desktop width on a phone, which is
    // the state this whole change is about: 250px of 390px spent on navigation.
    expect(cfg![0]).toContain('whenNarrow: true')
  })

  it('lays the collapsed rail ACROSS THE TOP, not down the side', async () => {
    const s = await src('Workspace.tsx')
    expect(s, 'expected railBar from narrow AND collapsed')
      .toMatch(/const railBar = isMobile && rail\.collapsed/)
    // The direction flip is what puts the bar above the pane; without it the bar
    // is a wide strip still sitting in a row, taking the width twice over.
    expect(s, 'expected the shell to stack while the bar is up')
      .toMatch(/\$\{railBar \? 'flex-col' : ''\}/)
    expect(s, 'expected the bar mode to reach the rail').toMatch(/horizontal=\{railBar\}/)
    const rail = await src('SpecRail.tsx')
    const bar = rail.match(/if \(horizontal\) \{[\s\S]*?\n    \}/)
    expect(bar, 'expected a horizontal branch in SpecRail').not.toBeNull()
    expect(bar![0], 'the bar must lay out as a row').toMatch(/flex items-center/)
  })

  it('gives the expanded rail the whole viewport and steps the detail aside', async () => {
    const s = await src('Workspace.tsx')
    // `whenNarrow`'s own contract: a rail that expands back to its minimum beside
    // the detail hands the user the squeeze they just escaped.
    expect(s).toMatch(/const railFull = isMobile && !rail\.collapsed/)
    expect(s).toMatch(/width=\{railFull \? '100%' : rail\.width\}/)
    // HIDDEN, not unmounted. `{railFull ? null : ...}` discarded a typed chat
    // message and any staged review comments the moment the user opened the rail
    // to look for another spec, because SpecDetail owns both in local state.
    // Every other narrow shell in the repo uses `hidden` for exactly this reason.
    expect(s, 'the detail pane must be hidden, never unmounted, behind the rail')
      .toMatch(/flex-1 min-w-0 min-h-0 \$\{railFull \? 'hidden' : 'flex'\}/)
    expect(s, 'the detail pane must not be unmounted while the rail is up')
      .not.toMatch(/railFull \? null/)
  })

  it('points the empty state at where the list actually is', async () => {
    const s = await src('Workspace.tsx')
    // The list is beside this pane on a desktop and ABOVE it while narrow, where
    // the rail is a bar. A left arrow there pointed at the screen edge.
    expect(s, 'expected an up arrow while narrow').toMatch(/isMobile\s*\n?\s*\?\s*<ArrowUp/)
    expect(s, 'expected the left arrow to survive on a desktop').toContain('<ArrowLeft')
  })

  it('collapses the rail on select, so the full-width rail is not a one-way door', async () => {
    const s = await src('Workspace.tsx')
    const sel = s.match(/const selectSpec = [\s\S]*?\n  \}/)
    expect(sel, 'expected a selectSpec wrapper').not.toBeNull()
    expect(sel![0]).toContain('if (isMobile) rail.collapse()')
    // Wired in place of the raw setter, or the drill-down never fires.
    expect(s).toMatch(/setSel=\{selectSpec\}/)
  })

  it('drops both pointer-only drag handles on touch', async () => {
    // Each costs width a phone has none of and does nothing without a pointer:
    // the shell's rail splitter and the detail's own document divider.
    expect(await src('Workspace.tsx')).toMatch(/\{!isMobile && \(\s*<ColumnSplitter/)
    expect(await src('SpecDetail.tsx')).toMatch(/cursor-col-resize[^`]*\$\{isMobile \? 'hidden' : ''\}/)
  })

  it('hands the chat the full width and keeps the document reachable', async () => {
    const s = await src('SpecDetail.tsx')
    // The document column is a PERCENTAGE of the row, so at 390px it did not
    // overflow — it just took 59px and left the chat 69px. Releasing that basis
    // is what gives the chat the width; the review overlay keeps the document.
    expect(s, 'the percentage basis must not apply while narrow')
      .toMatch(/style=\{isMobile \? undefined : \{ flexBasis: docPct/)
    const gate = s.match(/\{isMobile && \(\s*<Btn[\s\S]*?\/>\s*\)\}/)
    expect(gate, 'expected a narrow-only control in the chat header').not.toBeNull()
    // Reuses the overlay AND its existing label, so no new string in any locale.
    expect(gate![0]).toContain('setExpanded(true)')
    expect(gate![0]).toContain('expand_document_for_review')
  })

  it('keeps unsent review comments reachable while narrow', async () => {
    const s = await src('SpecDetail.tsx')
    // The pending-comment tray lives in the document column. Hiding that column
    // outright made comments the user WROTE unreachable — no Send, no Clear — and
    // `key={sel}` unmounts this component on the next spec, so they were then
    // discarded silently. While narrow the column survives as a full-width row
    // under the chat whenever it has comments, carrying only the tray.
    expect(s, 'the column must not be hidden while it holds comments')
      .toMatch(/comments\.length > 0 \? 'w-full shrink-0 border-t border-border' : 'hidden'/)
    // And the document body itself must still step aside, or the 44% split — and
    // the 69px chat column — come straight back.
    expect(s, 'the document body must still step aside')
      .toMatch(/sb-doc flex-1 min-h-0 flex flex-col overflow-hidden \$\{isMobile \? 'hidden' : ''\}/)
    // Same rule for the state panel: its `sent` map guards against answering one
    // decision twice while the agent persists the first answer, and it is local
    // state. Unmounting resets the guard, so rotating a phone across the 768px
    // breakpoint and back would let a conflicting answer through.
    expect(s, 'the state panel must be hidden, never unmounted')
      .toMatch(/<div className=\{isMobile \? 'hidden' : ''\}>\s*\n\s*<SpecStatePanel/)
    expect(s, 'the state panel must not be conditionally rendered away')
      .not.toMatch(/\{!isMobile && \(\s*\n?\s*<SpecStatePanel/)
    expect(s, 'the detail must stack so the tray lands under the chat')
      .toMatch(/flex flex-1 min-w-0 min-h-0 \$\{isMobile \? 'flex-col' : ''\}/)
  })

  it('spends the bar on at most two actions', async () => {
    const rail = await src('SpecRail.tsx')
    const bar = rail.match(/if \(horizontal\) \{[\s\S]*?\n    \}/)
    expect(bar, 'expected a horizontal branch').not.toBeNull()
    // AUTOSDE's max-two-buttons-per-row is blocking, and this bar is a NEW row:
    // expand and new spend its two. The vertical strip stacks the same three,
    // which the rule does not govern. Settings stays in the expanded rail footer.
    expect(bar![0], 'settings must not ride in the bar').not.toContain('spec_builder_settings')
    expect((bar![0].match(/<Btn\b/g) ?? []).length, 'at most one Btn beside the expand control')
      .toBeLessThanOrEqual(1)
    // Still reachable, just one tap further — through expand.
    expect(rail, 'settings must remain in the expanded rail').toContain('spec_builder_settings')
  })
})
