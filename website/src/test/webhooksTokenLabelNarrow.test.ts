/**
 * The Webhooks token row: an <Input> with a max-width and NO floor inside a
 * `flex-wrap` row. A flex item like that shrinks toward zero BEFORE the row
 * wraps, so the field crushed while the full-size "Generate token" button and
 * the signing checkbox kept their size. Naming a token is the gate on creating
 * one (the button stays disabled until the label is non-empty), so the field you
 * must type into was the one squeezed out.
 *
 * Measured widths, same probe:
 *
 *            before   after
 *   390px     166px    314px (own line)
 *   430px      40px    354px (own line)
 *   1280px    260px    260px (unchanged)
 *
 * Note the 430px row: WIDER is worse. The extra width lets the checkbox stay on
 * line 1, so three items share the row and the only one that can shrink does.
 * That also means the defect gets worse, not better, as the pane is given more
 * width.
 *
 * `basis-full` while narrow is deterministic where a min-width is not: a floor
 * only forces the wrap if THIS locale's content happens to exceed the container,
 * and the placeholder runs from 17 characters (Korean) to 29 (German).
 *
 * Asserted over source: jsdom performs no layout, so a render could not measure
 * the widths this is about.
 */
import { describe, it, expect } from 'vitest'

async function src(): Promise<string> {
  return (await import('../pages/WebhooksPage.tsx?raw')).default as string
}

describe('Webhooks token label field at narrow widths', () => {
  it('gives the label input its own line while narrow', async () => {
    const s = await src()
    const input = s.match(/<Input\n[\s\S]*?aria-label=\{i18nT\('pages\.webhooksPage\.new_token_label'\)\}/)
    expect(input, 'expected the new-token label Input').not.toBeNull()
    // A whole line, not a floor: the wrap must not depend on the active locale.
    expect(input![0], 'the input must take its own line while narrow')
      .toContain('basis-full')
    expect(input![0], 'and must sit inline again on a desktop')
      .toContain('sm:basis-auto')
  })

  it('does not cap the input below the line it now owns', async () => {
    const s = await src()
    const input = s.match(/<Input\n[\s\S]*?aria-label=\{i18nT\('pages\.webhooksPage\.new_token_label'\)\}/)
    // The old ungated `max-w-[260px]` would cap the full-width line back to 260px,
    // leaving a short field beside dead space — the cap belongs to the desktop
    // row, where the input shares the line with the button.
    expect(input![0], 'the 260px cap must be desktop-only')
      .toContain('sm:max-w-[260px]')
    // Lookbehind, because `sm:max-w-[260px]` CONTAINS `max-w-[260px]` — a plain
    // substring check flags the correct form.
    expect(input![0], 'no ungated 260px cap').not.toMatch(/(?<!sm:)max-w-\[260px\]/)
  })

  it('keeps the row wrapping, which is what makes the line available', async () => {
    const s = await src()
    // `basis-full` only produces a new line because the row wraps. Without this
    // the input would be a full-width item forced onto a non-wrapping row and
    // would overflow instead.
    expect(s).toMatch(/<div className="flex items-center gap-2 flex-wrap">/)
  })
})
