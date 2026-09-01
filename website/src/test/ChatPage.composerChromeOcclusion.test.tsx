import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * A phone keyboard used to hide the caret you were typing at.
 *
 * The composer's status bars are flex-flow SIBLINGS of the transcript scroller,
 * not overlays. The scroller is `overflow-y:auto`, so its automatic minimum size
 * is 0 and it collapses under pressure, taking the `h-16` header spacer inside
 * it. An opening keyboard IS that pressure: `interactive-widget=resizes-content`
 * shrinks the layout viewport, the column shortens, and the uncapped stack's top
 * edge rises into the title band at `top-0` — which hosts the rename editor, one
 * z-layer below the bars, so they painted over the input.
 *
 * Two independent guards: the stack is capped and scrolls internally so it
 * yields space instead of climbing (covering all five bars, not just the
 * reported one), and the band outranks the bars while it hosts the editor. The
 * lift is conditional because at rest the band must stay BELOW the mobile
 * sessions scrim so an open drawer dims it; it is safe because opening the
 * drawer blurs the input, which commits and closes the editor.
 *
 * Asserted against SOURCE TEXT, as the transcript-mask geometry guard already
 * does: the cap is a viewport unit jsdom cannot resolve into a height, and the
 * real invariant is the ORDER BETWEEN three z-values living in two files.
 */
const CHAT_PAGE_VIEW = readFileSync(resolve(__dirname, '../pages/chat/ChatPageView.tsx'), 'utf8')
const SUBAGENT_BAR = readFileSync(resolve(__dirname, '../pages/chat/SubagentProgressBar.tsx'), 'utf8')
const QUEUE_STACK = readFileSync(resolve(__dirname, '../components/QueueStack.tsx'), 'utf8')
const INDEX_CSS = readFileSync(resolve(__dirname, '../index.css'), 'utf8')
const INDEX_HTML = readFileSync(resolve(__dirname, '../../index.html'), 'utf8')

/** The title-row overlay: `absolute top-0 … ${editingTitle ? 'z-[47]' : 'z-[45]'}`. */
const TITLE_ROW = /absolute top-0 left-0 right-1\.5 \$\{editingTitle \? 'z-\[(\d+)\]' : 'z-\[(\d+)\]'\}/.exec(CHAT_PAGE_VIEW)
/** The mobile sessions scrim, the one layer the resting band must stay under. */
const SCRIM = /key="sessions-backdrop"[\s\S]{0,240}?z-\[(\d+)\]/.exec(CHAT_PAGE_VIEW)
/** The wave chip's wrapper, lifted to clear ThemeExperienceLayer's ceiling. */
const CHIP = /className="px-4 mx-auto w-full relative z-\[(\d+)\]"/.exec(SUBAGENT_BAR)
/** The wrapper introduced around the bar stack. */
const STACK = /<div ref=\{composerBandRef\} className="([^"]*)" data-testid="composer-status-stack">/.exec(CHAT_PAGE_VIEW)
/** QueueStack's fuse overhang — the seam that pulls its card into the composer. */
const OVERLAP = /const OVERLAP = (\d+)/.exec(QUEUE_STACK)

describe('composer status chrome cannot occlude the header rename editor', () => {
  it('anchors the invariant: every value it compares was actually found', () => {
    // A regex that silently stopped matching would make every ordering assertion
    // below pass on `undefined < undefined` style vacuity, so failing loudly here
    // is what gives the rest of the file its meaning.
    expect(TITLE_ROW, 'title-row overlay className not found in ChatPageView.tsx').not.toBeNull()
    expect(SCRIM, 'sessions-backdrop scrim not found in ChatPageView.tsx').not.toBeNull()
    expect(CHIP, 'wave-chip wrapper not found in SubagentProgressBar.tsx').not.toBeNull()
    expect(STACK, 'composer-status-stack wrapper not found in ChatPageView.tsx').not.toBeNull()
    expect(OVERLAP, 'OVERLAP constant not found in QueueStack.tsx').not.toBeNull()
  })

  it('cancels QueueStack\'s fuse overhang instead of scrolling it', () => {
    // A scroll container turns the -OVERLAP margin into permanent internal
    // overflow: measured scrollHeight-clientHeight == 11 with a collapsed queue
    // at any height, so a thumb showed and the card's bottom 11px clipped.
    const n = Number(OVERLAP![1])
    expect(STACK![1]).toContain(`pb-[${n}px]`)
    expect(STACK![1]).toContain(`mb-[-${n}px]`)
  })

  it('keeps the padding and the negative margin equal, so the column is unchanged', () => {
    // Unequal values would silently move the composer up or down by the
    // difference — layout drift that no other case here would catch.
    const pb = Number(/pb-\[(\d+)px\]/.exec(STACK![1])![1])
    const mb = Number(/mb-\[-(\d+)px\]/.exec(STACK![1])![1])
    expect(pb).toBe(mb)
  })

  it('keeps the thumb out of the way instead of carrying the global always-on bar', () => {
    // Asserted across BOTH files: the wrapper opts in, AND the utility it opts
    // into is still the hover-revealed kind. Redefining the utility to paint a
    // permanent thumb would leave the class assertion passing and this failing.
    expect(STACK![1]).toContain('scrollbar-overlay')
    expect(INDEX_CSS).toMatch(/\.scrollbar-overlay::-webkit-scrollbar-thumb\{background:transparent/)
    // The token is --muted, matching the coarse-pointer rung: WCAG 1.4.11 grants
    // hover no exemption, so the revealed thumb clears the same 3:1 floor as the
    // persistent one. What this line pins is the hover-REVEAL shape (transparent
    // base, painted on :hover); scrollbarThumbContrast.test.ts owns the floor.
    expect(INDEX_CSS).toMatch(/\.scrollbar-overlay:hover::-webkit-scrollbar-thumb\{background:var\(--muted\)/)
  })

  it('restores a persistent thumb on coarse pointers, so a capped box has a touch affordance', () => {
    // A finger never produces the :hover that reveals the thumb, so on touch
    // (.scrollbar-overlay on a capped `max-h-[Nsvh]` stack) the scroller would
    // show no cue that it scrolls. The utility must scope its persistent thumb
    // to @media (pointer: coarse) — a permanent (unscoped) thumb would trip the
    // hover-revealed assertion above on fine pointers. Precedence: the house
    // already carries @media (pointer: coarse) blocks in index.css.
    expect(INDEX_CSS).toMatch(/@media \(pointer: coarse\) \{[\s\S]*?\.scrollbar-overlay\{scrollbar-color:var\(--muted\) transparent\}/)
    expect(INDEX_CSS).toMatch(/@media \(pointer: coarse\) \{[\s\S]*?\.scrollbar-overlay::-webkit-scrollbar-thumb\{background:var\(--muted\)\}/)
  })

  it('caps the bar stack and scrolls it internally, so it never climbs into the band', () => {
    const cls = STACK![1]
    expect(cls).toMatch(/max-h-\[\d+svh\]/)
    expect(cls).toContain('overflow-y-auto')
  })

  it('states the cap as a fraction of the viewport, never a percentage', () => {
    // A percentage max-height resolves against the wrapper's own
    // content-derived height. That is circular, computes to `none`, and the cap
    // would silently do nothing — the failure mode this case exists to catch.
    expect(STACK![1]).not.toMatch(/max-h-\[\d+%\]/)
  })

  it('measures the cap against the SMALL viewport, not the large one', () => {
    // `vh` resolves against the large viewport, so on a phone showing its URL
    // bar a `50vh` cap is more than half the pane the user can actually see —
    // the same over-measure SkillsTab documents. `dvh` would track it but
    // re-resolves while the bar animates.
    expect(STACK![1]).not.toMatch(/max-h-\[\d+(vh|dvh|lvh)\]/)
  })

  it('keeps the cap under half the pane, so the band stays clear with the composer', () => {
    expect(Number(/max-h-\[(\d+)svh\]/.exec(STACK![1])![1])).toBeLessThanOrEqual(50)
  })

  it('lifts the band above the bars while it hosts the editor', () => {
    expect(Number(TITLE_ROW![1])).toBeGreaterThan(Number(CHIP![1]))
  })

  it('leaves the band below the sessions scrim at rest, so the drawer still dims it', () => {
    expect(Number(TITLE_ROW![2])).toBeLessThanOrEqual(Number(SCRIM![1]))
  })

  it('keeps even the lifted band below the drawer, the mute button and the consent modal', () => {
    // Those sit at 50 / 50 / 120; the lift buys one layer over the bars, not a
    // licence to outrank a real modal.
    expect(Number(TITLE_ROW![1])).toBeLessThan(50)
  })

  it('depends on a shrinking layout viewport, so the premise is pinned too', () => {
    // Without resizes-content the column would not shorten, the stack would not
    // rise, and guard 1 would be capping for a pressure that never arrives.
    expect(INDEX_HTML).toContain('interactive-widget=resizes-content')
  })
})
