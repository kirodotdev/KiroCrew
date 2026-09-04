import { describe, it, expect } from 'vitest'
import { clampSidebarWidth, SIDEBAR_MIN } from '../pages/chat/sidebarWidth'

describe('clampSidebarWidth', () => {
  // Pins the board-column e2e geometry (primeBrowser: stored 1400, viewport 1800).
  // Reserving CHAT_PANE_MIN_W here capped it to 1244 and broke two e2e specs.
  it('leaves a legitimately wide board sidebar alone', () => {
    expect(clampSidebarWidth({ stored: 1400, winW: 1800, railW: 236 })).toBe(1400)
  })

  it('leaves the stored width alone whenever it fits beside the rail', () => {
    expect(clampSidebarWidth({ stored: 260, winW: 1440, railW: 236 })).toBe(260)
    expect(clampSidebarWidth({ stored: 900, winW: 1800, railW: 236 })).toBe(900)
  })

  it('narrows a stored width that cannot fit the window', () => {
    // A desktop preference carried onto a portrait phone: 236 + 260 > 412.
    expect(clampSidebarWidth({ stored: 260, winW: 412, railW: 236 })).toBe(SIDEBAR_MIN)
    expect(clampSidebarWidth({ stored: 1400, winW: 900, railW: 236 })).toBe(664)
  })

  it('never returns less than SIDEBAR_MIN, even with no room at all', () => {
    expect(clampSidebarWidth({ stored: 1400, winW: 200, railW: 236 })).toBe(SIDEBAR_MIN)
  })

  it('gives the whole window to the sidebar when the rail is collapsed away', () => {
    // railW is 0 on mobile (railWidthFor returns 0), so nothing is subtracted.
    expect(clampSidebarWidth({ stored: 300, winW: 412, railW: 0 })).toBe(300)
  })
})
