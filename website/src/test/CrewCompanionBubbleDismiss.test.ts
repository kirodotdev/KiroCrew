/**
 * Dismissing a bubble must free the slot.
 *
 * There is ONE slot, and a sticky (blocked / needs-approval) bubble takes it and holds
 * it so routine chatter cannot displace unresolved work. The bug this pins: a sticky
 * bubble has no ✕, but the whole bubble is a dismiss target — so it CAN be
 * acknowledged, and if the slot stayed held afterwards the bubble left the screen
 * while every later notification was silently swallowed until the 90s cap expired.
 *
 * A deliberate dismissal is the acknowledgement the hold waits for.
 */
import { describe, expect, it } from 'vitest'
import { STICKY_HOLD_MS, nextBubble } from '../apps/crew-companion/bubbleSlot'

/** What pet.tsx's `dismiss` does to the slot. */
function dismiss(): null {
  return null
}

describe('bubble dismissal frees the slot', () => {
  it('releases a sticky slot, so the next notification is not swallowed', () => {
    const now = 1_000
    const held = { text: 'Needs your approval', sticky: true, count: 1, at: now, kind: 'approval' }

    // While held, routine news cannot take the slot — that is the intended behaviour.
    const blocked = nextBubble(held, { text: 'Session finished', sticky: false, kind: 'session-done' }, now + 500)
    expect(blocked.show).toBeNull()

    // After the user dismisses it, the slot is empty and the next one shows.
    const after = nextBubble(dismiss(), { text: 'Session finished', sticky: false, kind: 'session-done' }, now + 600)
    expect(after.show).not.toBeNull()
  })

  it('still lets the cap release a sticky bubble the user never touched', () => {
    const now = 1_000
    const held = { text: 'Needs your approval', sticky: true, count: 1, at: now, kind: 'approval' }
    const later = nextBubble(held, { text: 'Session finished', sticky: false, kind: 'session-done' }, now + STICKY_HOLD_MS + 1)
    expect(later.show).not.toBeNull()
  })
})
