import { describe, it, expect } from 'vitest'
import { shouldChimeOnTurnDone, TURN_DONE_KIND } from '../hooks/notificationEvent'

// Policy: chime only when the user isn't watching the reply land.
// Watching = the finishing slot is active AND the tab is visible AND the
// window is focused. Reconnect replays and slot-less events never chime.

const watching = { slot: 's1', activeSlot: 's1', reconnecting: false, hidden: false, focused: true }

describe('shouldChimeOnTurnDone', () => {
  it('does not chime when actively watching the finishing chat', () => {
    expect(shouldChimeOnTurnDone(watching)).toBe(false)
  })

  it('chimes when the turn finished in a background slot', () => {
    expect(shouldChimeOnTurnDone({ ...watching, activeSlot: 's2' })).toBe(true)
  })

  it('chimes when the tab is hidden', () => {
    expect(shouldChimeOnTurnDone({ ...watching, hidden: true })).toBe(true)
  })

  it('chimes when the window is unfocused (user in another app)', () => {
    expect(shouldChimeOnTurnDone({ ...watching, focused: false })).toBe(true)
  })

  it('never chimes during reconnect catch-up replay', () => {
    expect(shouldChimeOnTurnDone({ ...watching, activeSlot: 's2', reconnecting: true })).toBe(false)
    expect(shouldChimeOnTurnDone({ ...watching, hidden: true, reconnecting: true })).toBe(false)
  })

  it('never chimes for slot-less events', () => {
    expect(shouldChimeOnTurnDone({ ...watching, slot: undefined, hidden: true })).toBe(false)
    expect(shouldChimeOnTurnDone({ ...watching, slot: null, focused: false })).toBe(false)
    expect(shouldChimeOnTurnDone({ ...watching, slot: '', activeSlot: 's2' })).toBe(false)
  })

  it('chimes for a background slot even when null activeSlot', () => {
    expect(shouldChimeOnTurnDone({ ...watching, activeSlot: null })).toBe(true)
  })
})

describe('TURN_DONE_KIND', () => {
  it('is a valid sound category key', async () => {
    const { SOUND_CATEGORIES } = await import('../hooks/useNotificationSound')
    expect(SOUND_CATEGORIES).toContain(TURN_DONE_KIND)
  })
})
