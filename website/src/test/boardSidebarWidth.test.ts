import { describe, it, expect } from 'vitest'
import { boardSidebarWidth, SIDEBAR_MIN, SIDEBAR_MAX } from '../pages/ChatSidebar'
import { clampSidebarWidth } from '../pages/chat/sidebarWidth'

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

/** #16094: the list/chat clamp reserves CHAT_PANE_MIN_W, but the BOARD paints
 *  through the same clamp with chatReserve 0 -- because boardSidebarWidth has
 *  already reserved the chat pane (BOARD_CHAT_RESERVE) when it chose the width.
 *  So every board width, AND a hand-primed width the board passes through
 *  unwidened (Math.max(current, ...)), reaches the clamp with reserve 0 and is
 *  not cut. This pins the geometry commit 9406a36e7 protects. */
describe('board widths survive the board-view clamp (chatReserve 0)', () => {
  // railWidthFor's expanded track value; the live rail the clamp subtracts.
  const RAIL_W_EXPANDED = 236

  it('does not cut the pinned e2e geometry: stored 1400 @ viewport 1800', () => {
    // The board e2e specs prime stored 1400 at viewport 1800; boardSidebarWidth
    // passes it through unwidened. With reserve 0 the clamp leaves it at 1400,
    // NOT 1800-236-320 = 1244 (the value that broke those specs).
    const clamped = clampSidebarWidth({ stored: 1400, winW: 1800, railW: RAIL_W_EXPANDED, chatReserve: 0 })
    expect(clamped).toBe(1400)
  })

  for (const [count, viewport] of [[4, 1500], [4, 1920], [2, 1440], [12, 6000], [4, 1700]] as const) {
    it(`a ${count}-lane auto-widen at viewport ${viewport} is unchanged by the clamp`, () => {
      const board = boardSidebarWidth(count, 260, viewport)
      const clamped = clampSidebarWidth({
        stored: board, winW: viewport, railW: RAIL_W_EXPANDED, chatReserve: 0,
      })
      expect(clamped).toBe(board)
    })
  }
})
