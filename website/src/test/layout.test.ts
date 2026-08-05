import { describe, it, expect } from 'vitest'
import { LAYOUT } from '../components/layout'

describe('LAYOUT constants', () => {
  it('has expected keys', () => {
    expect(LAYOUT.NAV_WIDTH).toBe(220)
    expect(LAYOUT.NAV_COLLAPSED_WIDTH).toBe(56)
    expect(LAYOUT.CHAT_SIDEBAR_WIDTH).toBe(260)
    expect(LAYOUT.MAX_MESSAGE_WIDTH).toBe(820)
    expect(LAYOUT.LOG_LINE_CAP).toBe(500)
    expect(LAYOUT.TOPBAR_HEIGHT).toBe(52)
  })

  it('is readonly', () => {
    // TypeScript enforces this at compile time via `as const`,
    // but verify the values are numbers at runtime
    for (const v of Object.values(LAYOUT)) {
      expect(typeof v).toBe('number')
    }
  })
})
