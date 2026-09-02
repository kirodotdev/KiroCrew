import { describe, it, expect } from 'vitest'
import { boardSidebarWidth, SIDEBAR_MIN, SIDEBAR_MAX } from '../pages/ChatSidebar'

/** The board is a horizontal strip inside a sidebar that defaults to 260px, so
 *  four lanes are off-screen unless something widens it. These pin the two ways
 *  that widening could go wrong: swallowing the chat pane, and undoing a width
 *  the user chose. */
describe('boardSidebarWidth', () => {
  it('widens enough for four lanes to fit on a wide window', () => {
    // 4 × 220 + 3 × 8 + 16 = 920, and 1920 can spare it.
    expect(boardSidebarWidth(4, 260, 1920)).toBe(920)
  })

  it('never shrinks a sidebar the user already widened', () => {
    expect(boardSidebarWidth(4, 1100, 1920)).toBe(1100)
  })

  it('leaves room for the nav rail and the chat pane on a narrow window', () => {
    // 1500 − 220 nav − 520 chat = 760: the strip keeps a little horizontal
    // scroll rather than squeezing the conversation to an unreadable column.
    const w = boardSidebarWidth(4, 260, 1500)
    expect(w).toBe(760)
    expect(1500 - 220 - w).toBeGreaterThanOrEqual(520)
  })

  it('never exceeds the sidebar ceiling on a very wide window', () => {
    expect(boardSidebarWidth(12, 260, 6000)).toBeLessThanOrEqual(SIDEBAR_MAX)
  })

  it('never returns less than the sidebar floor', () => {
    expect(boardSidebarWidth(4, SIDEBAR_MIN, 400)).toBeGreaterThanOrEqual(SIDEBAR_MIN)
  })

  it('leaves the width alone when there are no columns', () => {
    expect(boardSidebarWidth(0, 260, 1920)).toBe(260)
  })

  it('scales with the lane count', () => {
    expect(boardSidebarWidth(2, 260, 1920)).toBeLessThan(boardSidebarWidth(4, 260, 1920))
  })
})
