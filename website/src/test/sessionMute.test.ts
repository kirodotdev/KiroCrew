import { describe, it, expect } from 'vitest'
import { isSlotMuted, isSlotMutedByCreator } from '../hooks/sessionMute'
import type { ChatSlot } from '../types'

/** Minimal ChatSlot factory — only the fields the mute walk reads. */
function slot(key: string, createdBy = '', mutesOpened = false): ChatSlot {
  return { key, messages: 0, running: false, created_by: createdBy, mutes_opened: mutesOpened } as ChatSlot
}

describe('isSlotMutedByCreator', () => {
  it("a person's own tab (empty created_by) is never muted", () => {
    const slots = [slot('conductor', '', true), slot('tab', '')]
    expect(isSlotMutedByCreator(slots, 'tab')).toBe(false)
  })

  it('the creator itself is never muted by its own flag', () => {
    const slots = [slot('conductor', '', true), slot('worker', 'conductor')]
    expect(isSlotMutedByCreator(slots, 'conductor')).toBe(false)
  })

  it('a worker opened by a flagged conductor is muted', () => {
    const slots = [slot('conductor', '', true), slot('worker', 'conductor')]
    expect(isSlotMutedByCreator(slots, 'worker')).toBe(true)
  })

  it('a worker whose conductor is NOT flagged is not muted', () => {
    const slots = [slot('conductor', '', false), slot('worker', 'conductor')]
    expect(isSlotMutedByCreator(slots, 'worker')).toBe(false)
  })

  it('a nested worker is muted when any ANCESTOR carries the flag', () => {
    const slots = [
      slot('top', '', true),
      slot('sub', 'top', false),
      slot('leaf', 'sub', false),
    ]
    expect(isSlotMutedByCreator(slots, 'leaf')).toBe(true)
  })

  it('a created_by cycle terminates and does not throw', () => {
    const slots = [slot('a', 'b', false), slot('b', 'a', false)]
    expect(isSlotMutedByCreator(slots, 'a')).toBe(false)
  })

  it('an unknown slot key is not muted', () => {
    expect(isSlotMutedByCreator([slot('x', '')], 'missing')).toBe(false)
    expect(isSlotMutedByCreator([], null)).toBe(false)
    expect(isSlotMutedByCreator([], undefined)).toBe(false)
  })
})

describe('isSlotMuted', () => {
  it('a row carrying its own mute is muted, with no creator involved', () => {
    const slots = [{ ...slot('tab', ''), muted: true }]
    expect(isSlotMuted(slots, 'tab')).toBe(true)
  })

  it("the row mute is keyed to that row only, never its siblings or creator", () => {
    const slots = [slot('conductor', ''), { ...slot('worker', 'conductor'), muted: true }, slot('other', 'conductor')]
    expect(isSlotMuted(slots, 'conductor')).toBe(false)
    expect(isSlotMuted(slots, 'other')).toBe(false)
  })

  it('still honours the creator rule', () => {
    const slots = [slot('conductor', '', true), slot('worker', 'conductor')]
    expect(isSlotMuted(slots, 'worker')).toBe(true)
  })

  it('an unmuted row with no flagged ancestor is not muted', () => {
    expect(isSlotMuted([slot('tab', '')], 'tab')).toBe(false)
    expect(isSlotMuted([], null)).toBe(false)
  })
})
