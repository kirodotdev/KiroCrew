import { describe, it, expect } from 'vitest'
import { clampSidebarWidth, sidebarMaxWidth, parseStoredSidebarWidth, sidebarPaintWidth, SIDEBAR_MIN, SIDEBAR_MAX } from '../pages/chat/sidebarWidth'

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

describe('sidebarMaxWidth', () => {
  it('is SIDEBAR_MAX on a normal-width window', () => {
    expect(sidebarMaxWidth({ winW: 1440, railW: 236, chatMin: 320 })).toBe(SIDEBAR_MAX)
    expect(sidebarMaxWidth({ winW: 1920, railW: 236, chatMin: 320 })).toBe(SIDEBAR_MAX)
  })
  it('is the window minus rail minus chat minimum on a wide window', () => {
    expect(sidebarMaxWidth({ winW: 3440, railW: 236, chatMin: 320 })).toBe(2884)
    expect(sidebarMaxWidth({ winW: 5120, railW: 74, chatMin: 320 })).toBe(4726)
  })
  it('falls back to SIDEBAR_MAX on a non-finite input', () => {
    expect(sidebarMaxWidth({ winW: 3440, railW: 236, chatMin: undefined as unknown as number })).toBe(SIDEBAR_MAX)
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
  it('paints a width up to SIDEBAR_MAX exactly as stored, whatever the window', () => {
    expect(sidebarPaintWidth({ stored: 900, winW: 1440, railW: 236, chatMin: 320 })).toBe(900)
    expect(sidebarPaintWidth({ stored: SIDEBAR_MAX, winW: 1024, railW: 236, chatMin: 320 })).toBe(SIDEBAR_MAX)
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
