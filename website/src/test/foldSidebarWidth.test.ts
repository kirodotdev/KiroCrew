import { describe, it, expect } from 'vitest'
import { foldHoldsRailCompact, foldSidebarWidth, SIDEBAR_MIN } from '../pages/chat/sidebarWidth'
import { railWidthFor } from '../hooks/useRailWidth'

// The iPhone Duo simulator's book pose in a Safari tab: two 475.5px segments.
const BOOK = [{ width: 475.5 }, { width: 475.5 }]

describe('foldSidebarWidth', () => {
  it('ends the list at the fold: the leading segment less the rail', () => {
    expect(foldSidebarWidth({ segments: BOOK, railW: 74, leading: 'left' })).toBe(401.5)
  })

  it('reads the right-hand segment in a right-to-left document', () => {
    const uneven = [{ width: 475.5 }, { width: 391.5 }]
    expect(foldSidebarWidth({ segments: uneven, railW: 74, leading: 'right' })).toBe(317.5)
  })

  it('never paints narrower than SIDEBAR_MIN', () => {
    expect(foldSidebarWidth({ segments: [{ width: 300 }, { width: 300 }], railW: 236, leading: 'left' })).toBe(SIDEBAR_MIN)
  })
})

describe('foldHoldsRailCompact', () => {
  const RAIL_EXPANDED = railWidthFor({ isMobile: false, collapsed: false })

  it('holds the rail compact when an expanded rail would push the list across the fold', () => {
    // 768px folded into two 384px segments: 236 + 180 > 384.
    const even768 = [{ width: 384 }, { width: 384 }]
    expect(foldHoldsRailCompact({ segments: even768, expandedRailW: RAIL_EXPANDED, leading: 'left' })).toBe(true)
  })

  it('leaves the rail to the user where an expanded rail and the narrowest list fit', () => {
    expect(foldHoldsRailCompact({ segments: BOOK, expandedRailW: RAIL_EXPANDED, leading: 'left' })).toBe(false)
    expect(foldHoldsRailCompact({ segments: [{ width: 416 }, { width: 416 }], expandedRailW: RAIL_EXPANDED, leading: 'left' })).toBe(false)
  })

  it('reads the leading segment in a right-to-left document', () => {
    const uneven = [{ width: 475.5 }, { width: 391.5 }]
    expect(foldHoldsRailCompact({ segments: uneven, expandedRailW: RAIL_EXPANDED, leading: 'left' })).toBe(false)
    expect(foldHoldsRailCompact({ segments: uneven, expandedRailW: RAIL_EXPANDED, leading: 'right' })).toBe(true)
  })
})
