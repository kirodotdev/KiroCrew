import { describe, it, expect } from 'vitest'
import { clampSidebarWidth, sidebarRoomWidth, parseStoredSidebarWidth, sidebarPaintWidth, SIDEBAR_MIN, SIDEBAR_MAX } from '../pages/chat/sidebarWidth'

describe('clampSidebarWidth', () => {
  // The clamp reserves no chat pane: ChatPage hands it the width the sidebar
  // root paints at, which board view already held beside a minimum chat pane. Reserving CHAT_PANE_MIN_W here a second
  // time would make the drawer narrower than the root it holds.
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

describe('board-column e2e geometry', () => {
  // Pins the geometry the board-column Playwright specs prime
  // (session-tags-folders, session-tags-e2e: stored 1400, viewport 1800, a
  // 236 px rail). The root paints 1800 - 236 - 320 = 1244 beside a minimum
  // chat pane, and the drawer around it takes that same width, so the two
  // agree and the strip's lanes stay inside the drawer that clips them.
  it('paints the root and the drawer at the same 1244 px', () => {
    const painted = sidebarPaintWidth({ stored: 1400, winW: 1800, railW: 236, chatMin: 320 })
    expect(painted).toBe(1244)
    expect(clampSidebarWidth({ stored: painted, winW: 1800, railW: 236 })).toBe(painted)
  })
  it('gives a wide window its room past SIDEBAR_MAX', () => {
    expect(sidebarRoomWidth({ winW: 3440, railW: 236, chatMin: 320 })).toBe(2884)
    expect(sidebarRoomWidth({ winW: 5120, railW: 74, chatMin: 320 })).toBe(4726)
    expect(sidebarRoomWidth({ winW: 1440, railW: 236, chatMin: 320 })).toBeLessThan(SIDEBAR_MAX)
  })
})

describe('parseStoredSidebarWidth', () => {
  it('rejects unreadable or too-narrow values', () => {
    expect(parseStoredSidebarWidth(null)).toBeNull()
    expect(parseStoredSidebarWidth('abc')).toBeNull()
    expect(parseStoredSidebarWidth(String(SIDEBAR_MIN - 1))).toBeNull()
  })
  it('keeps a usable value as saved, including one wider than this window allows', () => {
    // Narrowing to the window is sidebarPaintWidth's job, at render, so a later
    // widening of the same window restores the saved width.
    expect(parseStoredSidebarWidth('900')).toBe(900)
    expect(parseStoredSidebarWidth('2400')).toBe(2400)
  })
})


describe('sidebarPaintWidth', () => {
  it('paints a width that fits the window exactly as stored', () => {
    expect(sidebarPaintWidth({ stored: 900, winW: 1600, railW: 236, chatMin: 320 })).toBe(900)
    expect(sidebarPaintWidth({ stored: SIDEBAR_MAX, winW: 1956, railW: 236, chatMin: 320 })).toBe(SIDEBAR_MAX)
  })
  it('holds a width at or under SIDEBAR_MAX to the same room as a wider one', () => {
    // 1200 - 236 - 320: one rule on both sides of SIDEBAR_MAX, so a saved 1400
    // and a saved 1401 paint the same and the chat pane keeps its minimum.
    const at = sidebarPaintWidth({ stored: SIDEBAR_MAX, winW: 1200, railW: 236, chatMin: 320 })
    const past = sidebarPaintWidth({ stored: SIDEBAR_MAX + 1, winW: 1200, railW: 236, chatMin: 320 })
    expect(at).toBe(644)
    expect(past).toBe(at)
    // A host the sidebar fills reserves no chat pane.
    expect(sidebarPaintWidth({ stored: SIDEBAR_MAX, winW: 1200, railW: 236, chatMin: 0 })).toBe(964)
  })
  it('holds a width past SIDEBAR_MAX to the window ceiling once the window narrows', () => {
    // Dragged to 2884 on a 3440 window, then the window narrows to one too
    // narrow for SIDEBAR_MAX: 1440 - 236 - 320, so the chat pane and the
    // resize handle stay on screen.
    expect(sidebarPaintWidth({ stored: 2884, winW: 1440, railW: 236, chatMin: 320 })).toBe(884)
    // Floored at SIDEBAR_MIN on a window with no room at all.
    expect(sidebarPaintWidth({ stored: 2884, winW: 600, railW: 236, chatMin: 320 })).toBe(SIDEBAR_MIN)
    // 2000 - 236 - 320: the chat pane keeps its minimum.
    expect(sidebarPaintWidth({ stored: 2884, winW: 2000, railW: 236, chatMin: 320 })).toBe(1444)
    expect(sidebarPaintWidth({ stored: 2884, winW: 3440, railW: 236, chatMin: 320 })).toBe(2884)
    // Dragged to 4726 on a 5120 window beside a collapsed rail, then 3440.
    expect(sidebarPaintWidth({ stored: 4726, winW: 3440, railW: 74, chatMin: 320 })).toBe(3046)
  })
  it('feeds a drawer width that keeps the chat pane its minimum', () => {
    const stored = 4726, winW = 3440, railW = 236, chatMin = 320
    const drawer = clampSidebarWidth({ stored: sidebarPaintWidth({ stored, winW, railW, chatMin }), winW, railW })
    expect(winW - railW - drawer).toBe(chatMin)
  })
})
