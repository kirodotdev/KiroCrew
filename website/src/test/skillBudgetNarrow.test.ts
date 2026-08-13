import { describe, expect, it } from 'vitest'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

// Comments stripped before matching: the rules are explained in prose that quotes the
// very class names asserted against, and a raw negative match hits the explanation.
const src = async () => {
  const raw = await readFile(
    join(__dirname, '..', 'pages', 'overview', 'SkillContextBudget.tsx'), 'utf8')
  return raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

describe('SkillContextBudget narrow viewport', () => {
  it('does not add a third horizontal inset inside the card', async () => {
    const s = await src()
    // Measured at 390px: 16 (page) + 20 (card) + 16 (row) = 52px before the text,
    // against 32px for the same text in chat. See docs/page-layout.md ->
    // "Horizontal insets below the breakpoint".
    expect(s).toMatch(/gap-y-1 py-2 md:px-4/)
    expect(s, 'an ungated px-4 on the row is the third inset')
      .not.toMatch(/gap-y-1 px-4 py-2/)
  })

  it('gates EVERY row in the card, not just the data row', async () => {
    const s = await src()
    // Gating one row and not its siblings left the data rows 16px to the left of the
    // headers labelling them -- rows escaping their own section.
    expect(s).toMatch(/justify-between md:px-4 py-3 border-b border-border/)   // summary
    expect(s).toMatch(/md:px-4 py-2\.5 text-\[11px\] text-muted border-t/)     // footnote
    expect(s).toMatch(/justify-between md:px-4 py-2 border-t border-b/)        // group header
    expect(s, 'an ungated px-4 on any sibling re-creates the ragged edge')
      .not.toMatch(/justify-between px-4 py-3 border-b|"px-4 py-2\.5 text-\[11px\]|justify-between px-4 py-2 border-t/)
  })

  it('lets the failed-flip message wrap rather than overflow', async () => {
    const s = await src()
    // `shrink-0` sizes at max-content, so a longer locale's recovery text runs past a
    // ~316px row with no scrollable ancestor -- unreachable exactly when it matters.
    expect(s).toMatch(/className="min-w-0 md:shrink-0 text-\[10\.5px\] font-mono md:w-\[140px\]"/)
    expect(s).not.toMatch(/className="shrink-0 text-\[10\.5px\] font-mono md:w-\[140px\]"/)
  })

  it('lets the row wrap below the breakpoint', async () => {
    const s = await src()
    // 280 + 140 + 42 = 462px of fixed columns against a 316px row at 390px, which
    // resolved column 1 -- the skill name -- to 0px with nothing scrollable above it.
    expect(s).toMatch(/flex flex-wrap md:flex-nowrap items-center gap-x-3\.5 gap-y-1/)
    expect(s, 'a non-wrapping row is the defect')
      .not.toMatch(/className=\{`flex items-center gap-3\.5 px-4 py-2 border-b/)
  })

  it('gives the skill name the whole first line', async () => {
    const s = await src()
    expect(s).toMatch(/className="flex-1 min-w-0 basis-full md:basis-auto"/)
  })

  it('stops the bar column reserving 280px below the breakpoint', async () => {
    const s = await src()
    expect(s).toMatch(/className="flex-1 min-w-0 md:flex-none md:w-\[280px\]"/)
    expect(s).not.toMatch(/className="w-\[280px\] shrink-0"/)
  })

  it('stops the deliveries column reserving 140px below the breakpoint', async () => {
    const s = await src()
    // The column also stopped sizing at max-content while narrow, so the invariant is
    // "no 140px reservation AND free to shrink", not the earlier class spelling.
    expect(s).toMatch(/className="min-w-0 md:shrink-0 text-\[10\.5px\] font-mono md:w-\[140px\]"/)
    expect(s).not.toMatch(/className="w-\[140px\] shrink-0 text-\[10\.5px\] font-mono"/)
  })

  it('uses md: rather than sm: for the reflow', async () => {
    const s = await src()
    // 462px of columns plus a readable name needs about 610px, so sm: (640px) would
    // re-create the squeeze; md: (768px) also matches the breakpoint useIsMobile uses.
    expect(s).not.toMatch(/sm:flex-nowrap|sm:basis-auto|sm:w-\[280px\]|sm:w-\[140px\]/)
    expect(s).toMatch(/md:flex-nowrap/)
  })

  it('keeps the toggle column reachable at its fixed size', async () => {
    const s = await src()
    // The control must not be the thing that gives; it is the only affordance in the row.
    expect(s).toMatch(/className="w-\[42px\] shrink-0 flex items-center justify-end"/)
  })
})
