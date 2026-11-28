import { afterEach, describe, expect, it } from 'vitest'
import { isPaneSlotKey, readPaneSlot, rememberPaneSlot } from './paneLastSlot'

afterEach(() => localStorage.clear())

describe('paneLastSlot', () => {
  it('remembers and restores a key as long as the longest stored slot key plus the dashboard: prefix', () => {
    // 249 is MAX_SLOT_KEY_CHARS in src/kiro_crew/dashboard/slot_buffers.py.
    const key = `dashboard:${'a'.repeat(249)}`
    rememberPaneSlot('crew-1', key)
    expect(readPaneSlot('crew-1')).toBe(key)
  })

  it('remembers a key between 129 and 249 characters', () => {
    const key = 'k'.repeat(200)
    rememberPaneSlot('crew-1', key)
    expect(readPaneSlot('crew-1')).toBe(key)
  })

  it('refuses a key over the cap or outside the character set', () => {
    for (const bad of ['', 'a'.repeat(260), 'a b', 'a/b', 7, null]) {
      expect(isPaneSlotKey(bad)).toBe(false)
      rememberPaneSlot('crew-1', bad)
      expect(readPaneSlot('crew-1')).toBeNull()
    }
  })
})
