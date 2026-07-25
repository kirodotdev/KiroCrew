import { describe, it, expect } from 'vitest'
import { shouldChimeOnTurnDone, TURN_DONE_KIND } from '../hooks/notificationEvent'

// Policy: every real turn completion chimes — active chat or background.
// Only slot-less events and reconnect catch-up replays are suppressed.

describe('shouldChimeOnTurnDone', () => {
  it('chimes when a turn finishes (active or background — no attention gating)', () => {
    expect(shouldChimeOnTurnDone({ slot: 's1', reconnecting: false })).toBe(true)
  })

  it('never chimes during reconnect catch-up replay', () => {
    expect(shouldChimeOnTurnDone({ slot: 's1', reconnecting: true })).toBe(false)
  })

  it('never chimes for slot-less events', () => {
    expect(shouldChimeOnTurnDone({ slot: undefined, reconnecting: false })).toBe(false)
    expect(shouldChimeOnTurnDone({ slot: null, reconnecting: false })).toBe(false)
    expect(shouldChimeOnTurnDone({ slot: '', reconnecting: false })).toBe(false)
  })
})

describe('TURN_DONE_KIND', () => {
  it('is a valid sound category key', async () => {
    const { SOUND_CATEGORIES } = await import('../hooks/useNotificationSound')
    expect(SOUND_CATEGORIES).toContain(TURN_DONE_KIND)
  })
})
