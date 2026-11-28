/**
 * A check-ins card's confirm button names the change the press makes, so a
 * card that only turns the Heartbeat off is not confirmed with "Turn on".
 */
import { describe, it, expect } from 'vitest'
import type { Card } from '../api/cards'
import { specFor } from './cardRegistry'

// `changed` names the fields the gateway built a row for: by default every
// param but `member`, as when each one differs from the Bot's current setting.
const primary = (params: Record<string, unknown>, changed = Object.keys(params).filter(k => k !== 'member')) =>
  specFor('crewmate.check_ins')!.primary({
    kind: 'crewmate.check_ins', params, changes: changed.map(field => ({ field, label: field })),
  } as unknown as Card)

describe('crewmate.check_ins primary', () => {
  it('says "Turn off" when the card only turns switches off', () => {
    expect(primary({ member: 'scout', heartbeat: false })).toBe('Turn off these check-ins')
    expect(primary({ member: 'scout', daily: false, weekly: false })).toBe('Turn off these check-ins')
  })

  it('says "Turn on" when the card only turns switches on, with or without timing', () => {
    expect(primary({ member: 'scout', heartbeat: true })).toBe('Turn on these check-ins')
    expect(primary({ member: 'scout', daily: true, time: '09:00' })).toBe('Turn on these check-ins')
  })

  it('says "Save" for a mix of on and off, or a timing-only change', () => {
    expect(primary({ member: 'scout', daily: true, heartbeat: false })).toBe('Save these check-ins')
    expect(primary({ member: 'scout', interval: 60 })).toBe('Save these check-ins')
  })

  it('ignores a switch restated at its current value, which the press does not write', () => {
    expect(primary({ member: 'scout', heartbeat: false, interval: 60 }, ['interval'])).toBe('Save these check-ins')
    expect(primary({ member: 'scout', daily: true, heartbeat: false }, ['heartbeat'])).toBe('Turn off these check-ins')
  })
})
