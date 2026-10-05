import { describe, it, expect } from 'vitest'
import { clampSidebarWidth, SIDEBAR_MIN } from '../pages/chat/sidebarWidth'

// The chat pane's own minimum (SidePanel's CHAT_PANE_MIN_W), restated here so
// the test reads as the geometry it is asserting rather than importing the
// whole SidePanel module (which pulls the editor stack into this unit test).
const CHAT_PANE_MIN_W = 320

describe('clampSidebarWidth', () => {
  // Board view passes chatReserve 0: the board is a column strip meant to occupy
  // the width, and boardSidebarWidth already reserved the chat pane when it chose
  // the width. A second subtraction here would cap a legitimately wide board
  // sidebar -- the regression commit 9406a36e7 reverted.
  describe('board view (chatReserve 0)', () => {
    const reserve = 0

    it('leaves a legitimately wide board sidebar alone -- the pinned e2e geometry', () => {
      // session-tags-e2e.spec.ts / session-tags-folders.spec.ts prime stored 1400
      // at viewport 1800 with the board on. boardSidebarWidth is Math.max(current,
      // ...), so the primed 1400 passes through unwidened and reaches this clamp;
      // it MUST NOT be cut to 1800-236-320 = 1244, which broke those two specs.
      expect(clampSidebarWidth({ stored: 1400, winW: 1800, railW: 236, chatReserve: reserve })).toBe(1400)
    })

    it('leaves the stored width alone whenever it fits beside the rail', () => {
      expect(clampSidebarWidth({ stored: 260, winW: 1440, railW: 236, chatReserve: reserve })).toBe(260)
      expect(clampSidebarWidth({ stored: 900, winW: 1800, railW: 236, chatReserve: reserve })).toBe(900)
    })

    it('narrows a stored width that cannot fit the window', () => {
      // A desktop preference carried onto a portrait phone: 236 + 260 > 412.
      expect(clampSidebarWidth({ stored: 260, winW: 412, railW: 236, chatReserve: reserve })).toBe(SIDEBAR_MIN)
      expect(clampSidebarWidth({ stored: 1400, winW: 900, railW: 236, chatReserve: reserve })).toBe(664)
    })

    it('never returns less than SIDEBAR_MIN, even with no room at all', () => {
      expect(clampSidebarWidth({ stored: 1400, winW: 200, railW: 236, chatReserve: reserve })).toBe(SIDEBAR_MIN)
    })

    it('gives the whole window to the sidebar when the rail is collapsed away', () => {
      // railW is 0 on mobile (railWidthFor returns 0), so nothing is subtracted.
      expect(clampSidebarWidth({ stored: 300, winW: 412, railW: 0, chatReserve: reserve })).toBe(300)
    })
  })

  // The list / chat view passes CHAT_PANE_MIN_W so a stored width always leaves
  // the chat pane usable and the resize handle inside the window (#16094). The
  // hold is applied to EVERY stored width, so a saved 1400 is held exactly as a
  // saved 2884 is -- adjacent preferences can no longer paint differently.
  describe('list / chat view (chatReserve = CHAT_PANE_MIN_W)', () => {
    const reserve = CHAT_PANE_MIN_W

    it('clamps a stored 1400 on a 1440 window so the chat pane fits and the handle is grabbable', () => {
      // The reproduce case: 1440 window, rail expanded (236). Painted as stored,
      // 1400 would leave 1440-236-1400 = -196 for the chat pane and put the handle
      // at 1636, past the 1440 edge. Held: 1440-236-320 = 884.
      const w = clampSidebarWidth({ stored: 1400, winW: 1440, railW: 236, chatReserve: reserve })
      expect(w).toBe(884)
      // Chat pane keeps at least its minimum, and the handle (railW + w) is inside
      // the window, so the user can still grab it to narrow the sidebar.
      expect(1440 - 236 - w).toBeGreaterThanOrEqual(CHAT_PANE_MIN_W)
      expect(236 + w).toBeLessThanOrEqual(1440)
    })

    it('holds a saved 1400 and a saved SIDEBAR_MAX+ identically -- no adjacent-preference cliff', () => {
      // Before #16094 a width <= 1400 was left as stored while a wider one was
      // held, so a saved 1400 overhung a 1440 window where a saved 2884 did not.
      const held = clampSidebarWidth({ stored: 1400, winW: 1440, railW: 236, chatReserve: reserve })
      const heldWider = clampSidebarWidth({ stored: 2884, winW: 1440, railW: 236, chatReserve: reserve })
      expect(held).toBe(heldWider)
    })

    it('leaves a stored width that already fits beside the rail and chat pane', () => {
      // 1800-236-320 = 1244, so 900 fits untouched -- wider windows keep their
      // graceful behavior and nothing a user set is narrowed needlessly.
      expect(clampSidebarWidth({ stored: 900, winW: 1800, railW: 236, chatReserve: reserve })).toBe(900)
      expect(clampSidebarWidth({ stored: 260, winW: 1440, railW: 236, chatReserve: reserve })).toBe(260)
    })

    it('floors at SIDEBAR_MIN when the window cannot seat the reserve and the floor', () => {
      // The chat pane yields the rest rather than the sidebar collapsing below
      // where it is usable: 600-236-320 = 44 < SIDEBAR_MIN.
      expect(clampSidebarWidth({ stored: 1400, winW: 600, railW: 236, chatReserve: reserve })).toBe(SIDEBAR_MIN)
    })
  })
})
