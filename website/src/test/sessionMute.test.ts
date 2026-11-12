import { describe, it, expect } from 'vitest'
import { appsMutingSessions, isSlotMuted, isSlotMutedByCreator } from '../hooks/sessionMute'
import type { ChatSlot } from '../types'

const NONE: ReadonlySet<string> = new Set()

/** Minimal ChatSlot factory — only the fields the mute walk reads. */
function slot(key: string, createdBy = '', mutesOpened = false): ChatSlot {
  return { key, messages: 0, running: false, created_by: createdBy, mutes_opened: mutesOpened } as ChatSlot
}

describe('isSlotMutedByCreator', () => {
  it("a person's own tab (empty created_by) is never muted", () => {
    const slots = [slot('conductor', '', true), slot('tab', '')]
    expect(isSlotMutedByCreator(slots, 'tab', NONE)).toBe(false)
  })

  it('the creator itself is never muted by its own flag', () => {
    const slots = [slot('conductor', '', true), slot('worker', 'conductor')]
    expect(isSlotMutedByCreator(slots, 'conductor', NONE)).toBe(false)
  })

  it('a worker opened by a flagged conductor is muted', () => {
    const slots = [slot('conductor', '', true), slot('worker', 'conductor')]
    expect(isSlotMutedByCreator(slots, 'worker', NONE)).toBe(true)
  })

  it('a worker whose conductor is NOT flagged is not muted', () => {
    const slots = [slot('conductor', '', false), slot('worker', 'conductor')]
    expect(isSlotMutedByCreator(slots, 'worker', NONE)).toBe(false)
  })

  it('a nested worker is muted when any ANCESTOR carries the flag', () => {
    const slots = [
      slot('top', '', true),
      slot('sub', 'top', false),
      slot('leaf', 'sub', false),
    ]
    expect(isSlotMutedByCreator(slots, 'leaf', NONE)).toBe(true)
  })

  it('a created_by cycle terminates and does not throw', () => {
    const slots = [slot('a', 'b', false), slot('b', 'a', false)]
    expect(isSlotMutedByCreator(slots, 'a', NONE)).toBe(false)
  })

  it('an unknown slot key is not muted', () => {
    expect(isSlotMutedByCreator([slot('x', '')], 'missing', NONE)).toBe(false)
    expect(isSlotMutedByCreator([], null, NONE)).toBe(false)
    expect(isSlotMutedByCreator([], undefined, NONE)).toBe(false)
  })
})

describe('isSlotMuted', () => {
  it('a row carrying its own mute is muted, with no creator involved', () => {
    const slots = [{ ...slot('tab', ''), muted: true }]
    expect(isSlotMuted(slots, 'tab', NONE)).toBe(true)
  })

  it("the row mute is keyed to that row only, never its siblings or creator", () => {
    const slots = [slot('conductor', ''), { ...slot('worker', 'conductor'), muted: true }, slot('other', 'conductor')]
    expect(isSlotMuted(slots, 'conductor', NONE)).toBe(false)
    expect(isSlotMuted(slots, 'other', NONE)).toBe(false)
  })

  it('still honours the creator rule', () => {
    const slots = [slot('conductor', '', true), slot('worker', 'conductor')]
    expect(isSlotMuted(slots, 'worker', NONE)).toBe(true)
  })

  it('an unmuted row with no flagged ancestor is not muted', () => {
    expect(isSlotMuted([slot('tab', '')], 'tab', NONE)).toBe(false)
    expect(isSlotMuted([], null, NONE)).toBe(false)
  })
})

describe('the app mute', () => {
  const opened = (key: string, app: string) => ({ ...slot(key, ''), app }) as ChatSlot

  it("mutes a session the muted app opened and what that session opens, and neither another app's nor the user's own", () => {
    const slots = [opened('review', 'code-reviewer'), slot('spawned', 'review'), opened('research', 'research'), slot('mine', '')]
    const muted = appsMutingSessions({ apps: { 'code-reviewer': { mute_sessions: true }, research: { model: 'x' } } })
    expect(isSlotMuted(slots, 'review', muted)).toBe(true)
    expect(isSlotMuted(slots, 'spawned', muted)).toBe(true)
    expect(isSlotMuted(slots, 'research', muted)).toBe(false)
    expect(isSlotMuted(slots, 'mine', muted)).toBe(false)
  })

  it('reads only the JSON true, and mutes nothing before the config loads', () => {
    expect([...appsMutingSessions({ apps: { a: { mute_sessions: 'true' }, b: null, c: { mute_sessions: true } } })]).toEqual(['c'])
    expect(appsMutingSessions(undefined).size).toBe(0)
  })
})
