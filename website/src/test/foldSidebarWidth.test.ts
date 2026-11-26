import { describe, it, expect } from 'vitest'
import { foldHoldsRailCompact, foldSidebarWidth, SIDEBAR_MIN } from '../pages/chat/sidebarWidth'
import { railWidthFor } from '../hooks/useRailWidth'

// The iPhone Duo simulator's book pose in a Safari tab: two 475.5px segments.
const BOOK = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]

describe('foldSidebarWidth', () => {
  it('ends the list at the fold: the leading segment less the rail', () => {
    expect(foldSidebarWidth({ segments: BOOK, railW: 74, leading: 'left' })).toBe(401.5)
  })

  it('reads the right-hand segment in a right-to-left document', () => {
    const uneven = [{ x: 0, width: 475.5 }, { x: 475.5, width: 391.5 }]
    expect(foldSidebarWidth({ segments: uneven, railW: 74, leading: 'right' })).toBe(317.5)
  })

  it("ends at the fold inside the shell's safe-area box: a leading inset narrows the list, not the fold", () => {
    // 900px, segments 0-440 and 460-900, a 42px left inset: the shell starts at
    // 42 and the rail takes 74, so the list must end at 440, not 482.
    const hinge = [{ x: 0, width: 440 }, { x: 460, width: 440 }]
    const insets = { left: 42, right: 0 }
    expect(42 + 74 + foldSidebarWidth({ segments: hinge, railW: 74, leading: 'left', insets })).toBeLessThanOrEqual(440)
    expect(foldSidebarWidth({ segments: hinge, railW: 74, leading: 'left', insets })).toBe(324)
    // Right to left the leading segment is the right-hand one, and the right inset narrows it.
    expect(foldSidebarWidth({ segments: hinge, railW: 74, leading: 'right', insets: { left: 0, right: 42 } })).toBe(324)
  })

  it('never paints narrower than SIDEBAR_MIN', () => {
    expect(foldSidebarWidth({ segments: [{ x: 0, width: 300 }, { x: 300, width: 300 }], railW: 236, leading: 'left' })).toBe(SIDEBAR_MIN)
  })
})

describe('foldHoldsRailCompact', () => {
  const RAIL_EXPANDED = railWidthFor({ isMobile: false, collapsed: false })

  it('holds the rail compact when an expanded rail would push the list across the fold', () => {
    // 768px folded into two 384px segments: 236 + 180 > 384.
    const even768 = [{ x: 0, width: 384 }, { x: 384, width: 384 }]
    expect(foldHoldsRailCompact({ segments: even768, expandedRailW: RAIL_EXPANDED, leading: 'left' })).toBe(true)
  })

  it('leaves the rail to the user where an expanded rail and the narrowest list fit', () => {
    expect(foldHoldsRailCompact({ segments: BOOK, expandedRailW: RAIL_EXPANDED, leading: 'left' })).toBe(false)
    expect(foldHoldsRailCompact({ segments: [{ x: 0, width: 416 }, { x: 416, width: 416 }], expandedRailW: RAIL_EXPANDED, leading: 'left' })).toBe(false)
  })

  it('counts only the leading segment inside the safe-area box', () => {
    // 416px fits an expanded rail and the narrowest list flat; a 42px leading
    // inset leaves 374px, so the rail must stay compact.
    const even832 = [{ x: 0, width: 416 }, { x: 416, width: 416 }]
    expect(foldHoldsRailCompact({ segments: even832, expandedRailW: RAIL_EXPANDED, leading: 'left', insets: { left: 42, right: 0 } })).toBe(true)
  })

  it('reads the leading segment in a right-to-left document', () => {
    const uneven = [{ x: 0, width: 475.5 }, { x: 475.5, width: 391.5 }]
    expect(foldHoldsRailCompact({ segments: uneven, expandedRailW: RAIL_EXPANDED, leading: 'left' })).toBe(false)
    expect(foldHoldsRailCompact({ segments: uneven, expandedRailW: RAIL_EXPANDED, leading: 'right' })).toBe(true)
  })
})
