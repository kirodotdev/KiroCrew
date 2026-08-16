/**
 * The installed-app card's action cluster must leave the text row at phone
 * width.
 *
 * The cluster is unbounded — Open, Enable/Disable, Update or Sync, Uninstall and
 * the disclosure can all be present — and it does not shrink, so while it shares
 * one row with the text the text column is only the remainder. Measured against
 * a running instance before this guard existed: 34px at 390px and 0px at 320px,
 * which clamps a two-line description to about three characters.
 *
 * These are source contracts rather than jsdom geometry checks on purpose: jsdom
 * does not lay out flexbox, so a rendered-width assertion here would pass no
 * matter which classes the component carries. The real widths are verified
 * against a browser; what this file pins is that the responsive shape cannot be
 * silently removed.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = readFileSync(
  join(__dirname, '..', 'components', 'appstore', 'InstalledAppCard.tsx'),
  'utf8',
)

/** Strip comments so a rule never matches its own explanatory prose. */
const CODE = SRC.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')

describe('InstalledAppCard narrow-viewport shape', () => {
  it('stacks the header column-first and only goes to a row at sm', () => {
    // The outer header row: column below sm so the action cluster drops to its
    // own full-width row, the original single row from sm up.
    expect(CODE).toMatch(/className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-4"/)
  })

  it('does not pin the header to a single row unconditionally', () => {
    // The pre-fix shape. If this reappears the text column goes back to being
    // the remainder of whatever the buttons take.
    expect(CODE).not.toMatch(/className="flex items-start justify-between gap-4"/)
  })

  it('lets the action cluster wrap while narrow and stop shrinking only at sm', () => {
    expect(CODE).toMatch(/className="flex items-center gap-2 flex-wrap sm:flex-nowrap sm:shrink-0"/)
    // An unconditional shrink-0 is what made the cluster win the row.
    expect(CODE).not.toMatch(/className="flex items-center gap-2 shrink-0"/)
  })

  it('keeps the text column able to shrink below its content width', () => {
    // Without min-w-0 a flex item refuses to go below its longest word, so the
    // column would push the card wider instead of wrapping.
    expect(CODE).toMatch(/className="flex items-start gap-3 flex-1 min-w-0"/)
    expect(CODE).toMatch(/className="flex-1 min-w-0"/)
  })

  it('pulls body text out of the icon indent while narrow', () => {
    // Description and meta row both escape the gutter; the name does not.
    expect(CODE).toMatch(/line-clamp-2 -ml-14 sm:ml-0"/)
    expect(CODE).toMatch(/text-\[12px\] text-muted flex-wrap -ml-14 sm:ml-0"/)
  })

  it('keeps the pull-back equal to the icon width plus the row gap', () => {
    // -ml-14 is 56px and must stay equal to the tile (w-11 = 44px) plus the row
    // gap (gap-3 = 12px). Changing the tile size or the gap without changing
    // the offset silently misaligns the body text, which no width assertion
    // would catch -- so pin the three literals together.
    expect(CODE).toMatch(/className="w-11 h-11 mt-0\.5"/)
    expect(CODE).toMatch(/className="flex items-start gap-3 flex-1 min-w-0"/)
    const offsets = CODE.match(/-ml-14 sm:ml-0/g) ?? []
    expect(offsets).toHaveLength(2)
  })

  it('floors the name row at the tile height so pulled body text clears it', () => {
    // The pull is unconditional below sm, so the row above it must be at least
    // as tall as the tile or the description's first line paints over the tile.
    // A one-line header measures ~24px against a 44px tile -- an 18px overlap,
    // with nothing overflowing to reveal it. min-h-11 (44px) must equal h-11.
    expect(CODE).toMatch(/className="flex items-center gap-2 mb-1 flex-wrap min-h-11 sm:min-h-0"/)
    expect(CODE).toMatch(/className="w-11 h-11 mt-0\.5"/)
  })
})
