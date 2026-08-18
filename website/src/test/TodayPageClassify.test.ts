import { describe, it, expect } from 'vitest'
import { classify } from '../pages/TodayPage'
import type { ChatSlot } from '../types'

/**
 * Guards the core classification logic of the Today surface. The bug this most
 * directly locks out: keying "Needs You" on `waiting_for_input` (true of EVERY
 * finished turn) instead of `needs_input` (the narrow "agent asked and is
 * blocked" signal). That inversion would light the attention surface on every
 * completed session and defeat the feature's whole purpose, so these assertions
 * pin the signal set to `pending_approval | pending_approval_info | needs_input`.
 */

const slot = (over: Partial<ChatSlot>): ChatSlot => ({ key: 's1', ...over } as ChatSlot)

describe('TodayPage classify', () => {
  it('routes a pending tool approval to Needs You', () => {
    expect(classify(slot({ pending_approval: true })).bucket).toBe('needsYou')
    expect(classify(slot({ pending_approval_info: { request_id: 'r1' } as never })).bucket).toBe('needsYou')
  })

  it('routes a blocked question (needs_input) to Needs You', () => {
    expect(classify(slot({ needs_input: true })).bucket).toBe('needsYou')
  })

  it('does NOT treat an ordinary finished turn (waiting_for_input) as Needs You', () => {
    // waiting_for_input is true of every finished turn; it must not light the
    // attention surface — only needs_input / a pending approval does.
    expect(classify(slot({ waiting_for_input: true })).bucket).toBe('completed')
    expect(classify(slot({ has_options: true })).bucket).toBe('completed')
  })

  it('routes running work to Working, idle to Completed', () => {
    expect(classify(slot({ running: true })).bucket).toBe('working')
    expect(classify(slot({ subagents_running: true })).bucket).toBe('working')
    expect(classify(slot({ stop_state: 'killing' })).bucket).toBe('working')
    expect(classify(slot({})).bucket).toBe('completed')
  })

  it('prioritizes Needs You over a running flag', () => {
    expect(classify(slot({ running: true, needs_input: true })).bucket).toBe('needsYou')
  })
})
