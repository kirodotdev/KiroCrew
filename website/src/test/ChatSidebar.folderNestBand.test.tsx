/**
 * Reorder-vs-nest band math (isFolderNestBand).
 *
 * When a root folder is dragged over another folder's header, the MIDDLE of the
 * header re-parents INTO it (nest) and the top/bottom edges fall through to
 * sortable REORDER. The band is a fraction of the MEASURED header height, not a
 * px constant — that is what keeps it correct for both the taller list header
 * and the shorter board header. This regressed once: a hardcoded 34px band
 * applied to the shorter board header made almost the whole board row a nest
 * zone, so an intended reorder became a re-parent. These cases lock the
 * boundary at both header sizes.
 */
import { describe, it, expect } from 'vitest'
import { isFolderNestBand } from '../pages/ChatSidebar'

describe('isFolderNestBand — reorder vs nest boundary', () => {
  // Middle 60% nests; top/bottom 20% reorder. Fractions are 0.2 .. 0.8.
  const LIST_H = 34 // text-sm py-1.5 list header
  const BOARD_H = 26 // text-[12px] py-1 board header (shorter)

  it('nests in the middle of a list header', () => {
    expect(isFolderNestBand(LIST_H * 0.5, LIST_H)).toBe(true)
  })

  it('reorders at the top and bottom edges of a list header', () => {
    expect(isFolderNestBand(LIST_H * 0.1, LIST_H)).toBe(false) // top edge
    expect(isFolderNestBand(LIST_H * 0.95, LIST_H)).toBe(false) // bottom edge
  })

  it('nests in the middle of the SHORTER board header (regression guard)', () => {
    // The board over-nest bug: with a 34px constant, a pointer ~15px down a 26px
    // board header sat at 0.15..0.85 of 34 and wrongly nested. Measured against
    // the real 26px header, the SAME middle-of-header point still nests...
    expect(isFolderNestBand(BOARD_H * 0.5, BOARD_H)).toBe(true)
  })

  it('reorders at the edges of the SHORTER board header (regression guard)', () => {
    // ...but the edges of the board header stay REORDER — which the 34px
    // constant broke, because 0.15*34 = 5.1px still fell inside a 26px header's
    // upper edge that should reorder. Measured, the top/bottom 20% reorder.
    expect(isFolderNestBand(BOARD_H * 0.1, BOARD_H)).toBe(false)
    expect(isFolderNestBand(BOARD_H * 0.9, BOARD_H)).toBe(false)
  })

  it('is proportional: the same fractional point nests at either header size', () => {
    // Proportionality is the whole point of measuring: identical fractions land
    // identically regardless of the absolute px height.
    expect(isFolderNestBand(LIST_H * 0.5, LIST_H)).toBe(isFolderNestBand(BOARD_H * 0.5, BOARD_H))
    expect(isFolderNestBand(LIST_H * 0.05, LIST_H)).toBe(isFolderNestBand(BOARD_H * 0.05, BOARD_H))
  })

  it('treats the exact 0.2 / 0.8 boundaries as nest (inclusive)', () => {
    expect(isFolderNestBand(LIST_H * 0.2, LIST_H)).toBe(true)
    expect(isFolderNestBand(LIST_H * 0.8, LIST_H)).toBe(true)
    // Just outside the boundary reorders.
    expect(isFolderNestBand(LIST_H * 0.19, LIST_H)).toBe(false)
    expect(isFolderNestBand(LIST_H * 0.81, LIST_H)).toBe(false)
  })
})
