/**
 * The single-slot notification rules, ported with the bubble system from the
 * desktop app. Each test pins one rule the companion depends on:
 *
 *  - ONE slot, never a queue: a second completion replaces the first and collapses
 *    into a count instead of stacking.
 *  - Blocked / needs-approval work is sticky: it holds the slot and is shown verbatim.
 *  - That hold is BOUNDED by STICKY_HOLD_MS, so an unclicked approval cannot mute
 *    every later notification forever.
 *  - `collapsedText` is translated (no emoji) and carries the count.
 */
import { describe, it, expect } from 'vitest'
import {
  nextBubble,
  collapsedText,
  STICKY_HOLD_MS,
  type PendingBubble,
} from '../apps/crew-companion/bubbleSlot'

const T0 = 1_000_000

describe('collapsedText', () => {
  it('is translated, count-bearing, and free of emoji or raw keys', () => {
    const two = collapsedText(2)
    expect(two).toContain('2')
    // No emoji glyph carries the status — the words do.
    expect(two).not.toContain('✅')
    // Neither a raw i18n key nor its namespace leaks to the screen.
    expect(two).not.toContain('jobs_finished')
    expect(two).not.toContain('crewCompanion')
  })

  it('uses the singular form for a single job', () => {
    expect(collapsedText(1)).toContain('1')
    expect(collapsedText(1)).not.toBe(collapsedText(2))
  })
})

describe('nextBubble', () => {
  it('shows a fresh routine bubble when the slot is empty', () => {
    const r = nextBubble(null, { text: 'Task finished', kind: 'session-done' }, T0)
    expect(r.show).toBe('Task finished')
    // Matches the ported logic: a fresh routine bubble carries no kind (it is only
    // set on a sticky). pet.tsx falls back to the incoming kind for its reaction.
    expect(r.pending).toMatchObject({ sticky: false, count: 1 })
    expect(r.pending?.kind).toBeUndefined()
  })

  it('collapses a second completion into a count instead of stacking', () => {
    const first = nextBubble(null, { text: 'Task A done', kind: 'session-done' }, T0).pending
    const second = nextBubble(first, { text: 'Task B done', kind: 'session-done' }, T0 + 1_000)

    expect(second.pending?.count).toBe(2)
    expect(second.show).toBe(collapsedText(2))
    // It replaced — it did not queue a second toast.
    expect(second.show).not.toBe('Task B done')
  })

  it('keeps counting up across further completions', () => {
    let slot: PendingBubble | null = null
    let last = ''
    for (let i = 1; i <= 4; i++) {
      const r = nextBubble(slot, { text: `done ${i}`, kind: 'session-done' }, T0 + i)
      slot = r.pending
      last = r.show ?? ''
    }
    expect(slot?.count).toBe(4)
    expect(last).toBe(collapsedText(4))
  })

  it('lets blocked work take the slot and shows it verbatim', () => {
    const r = nextBubble(null, { text: 'Waiting on your approval', sticky: true, kind: 'approval' }, T0)
    expect(r.show).toBe('Waiting on your approval')
    expect(r.pending).toMatchObject({ sticky: true, kind: 'approval' })
  })

  it('holds the slot for a sticky bubble against routine chatter, within the window', () => {
    const sticky = nextBubble(null, { text: 'Needs you', sticky: true, kind: 'session-input' }, T0).pending
    const routine = nextBubble(sticky, { text: 'A task finished', kind: 'session-done' }, T0 + 1_000)

    // Routine is suppressed; the blocked bubble keeps the slot unchanged.
    expect(routine.show).toBeNull()
    expect(routine.pending).toBe(sticky)
  })

  it('releases the slot once the sticky hold has elapsed', () => {
    const sticky = nextBubble(null, { text: 'Needs you', sticky: true, kind: 'session-input' }, T0).pending
    const routine = nextBubble(sticky, { text: 'A task finished', kind: 'session-done' }, T0 + STICKY_HOLD_MS + 1)

    expect(routine.show).toBe('A task finished')
    expect(routine.pending).toMatchObject({ sticky: false, count: 1 })
  })

  it('lets one sticky replace another immediately', () => {
    const first = nextBubble(null, { text: 'Approval A', sticky: true, kind: 'approval' }, T0).pending
    const second = nextBubble(first, { text: 'Error B', sticky: true, kind: 'session-error' }, T0 + 500)

    expect(second.show).toBe('Error B')
    expect(second.pending).toMatchObject({ sticky: true, kind: 'session-error' })
  })
})
