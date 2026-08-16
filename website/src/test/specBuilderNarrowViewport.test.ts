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

  it('keeps every control that has no other host reachable while narrow', async () => {
    const s = await src('SpecDetail.tsx')
    // Only the document BODY steps aside. The header must stay: `docTabsHeader` is
    // the sole host of Approve → Design, Approve → Tasks, Start building and Pause.
    // The fullscreen overlay builds its own header, never calls docTabsHeader, and
    // gates those actions on `!fullscreen` -- so hiding this header left a phone
    // unable to advance, build or pause a spec at all.
    expect(s, 'the doc header must not be hidden while narrow')
      .toMatch(/sb-doc flex flex-col overflow-hidden \$\{isMobile \? 'shrink-0' : 'flex-1 min-h-0'\}/)
    expect(s, 'only the document body may step aside')
      .toMatch(/flex-1 min-h-0 flex flex-col \$\{isMobile \? 'hidden' : ''\}/)
    // The state panel is the only surface that shows a BLOCKING decision and the
    // only one that can answer it, and the overlay does not render it. Hidden, a
    // blocked spec was indistinguishable from an idle one.
    expect(s, 'the state panel must not be hidden').not.toMatch(/\{isMobile \? 'hidden' : ''\}>\s*\n\s*<SpecStatePanel/)
    // And the column itself must always render while narrow -- gating it on
    // `comments.length` took the phase controls away whenever there were none.
    expect(s, 'the column must always render while narrow')
      .toMatch(/\? 'w-full min-h-0 border-t border-border max-h-/)
    // Presence of the string is not enough -- it survives inside a nested ternary.
    // What must be absent is any comment-count gate on the column itself.
    expect(s, 'the column must not be gated on the comment count')
      .not.toMatch(/comments\.length > 0 \? 'w-full/)
    expect(s, 'the percentage basis must not apply while narrow')
      .toMatch(/style=\{isMobile \? undefined : \{ flexBasis: docPct/)
  })

  it('lets the doc header wrap while narrow, or the phase control is clipped', async () => {
    const s = await src('SpecDetail.tsx')
    // Measured in the real build at 390px: the header row is 414px wide, and the
    // pane's `overflow-hidden` clips `Approve → Tasks` (left 321, right 414) with
    // no way to scroll to it. Exposing the header is not enough on its own.
    expect(s, 'the header must wrap while narrow')
      .toMatch(/isMobile && !fullscreen \? 'flex-wrap min-h-\[52px\] py-1\.5' : 'h-\[52px\]'/)
  })

  it('bounds the stacked column in vh, since a percentage would not resolve', async () => {
    const s = await src('SpecDetail.tsx')
    // No ancestor in this chain has a definite height, so `max-h-[60%]` is inert.
    expect(s, 'expected a vh height bound').toMatch(/max-h-\[60vh\] overflow-y-auto/)
    // Pinned instead of shrinkable, the section would overflow a shell shorter
    // than the cap -- `vh` is viewport-relative, the shell is viewport minus header.
    expect(s, 'the stacked column must be able to shrink').not.toMatch(/'w-full shrink-0 border-t/)
    expect(s, 'a percentage bound would be inert here').not.toMatch(/max-h-\[\d+%\]/)
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
