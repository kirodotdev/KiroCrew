import { describe, it, expect } from 'vitest'
import { jumpAnchorIdx } from '../utils/pinnedPrompt'
import type { DisplayItem } from '../pages/chat/groupDisplayItems'

// Minimal display items: only kind and msg.role are read by the helper.
const user = (): DisplayItem => ({ kind: 'single', msg: { role: 'user', content: 'q' } } as unknown as DisplayItem)
const steer = (): DisplayItem => ({ kind: 'single', msg: { role: 'user', content: 's', meta: { steer: true } } } as unknown as DisplayItem)
const asst = (): DisplayItem => ({ kind: 'single', msg: { role: 'assistant', content: 'a' } } as unknown as DisplayItem)
const nudge = (): DisplayItem => ({ kind: 'single', msg: { role: 'nudge', content: 'n' } } as unknown as DisplayItem)
const sub = (): DisplayItem => ({ kind: 'single', msg: { role: 'subagent', content: 'done' } } as unknown as DisplayItem)
const turn = (): DisplayItem => ({ kind: 'turn', items: [] } as unknown as DisplayItem)

describe('jumpAnchorIdx', () => {
  it('returns the target itself when a non-prompt row precedes it', () => {
    const items = [user(), asst(), user()]
    expect(jumpAnchorIdx(items, 2)).toBe(2)
  })

  it('walks to the head of a consecutive prompt run (steer after its prompt)', () => {
    // question(1) + steer(2) back to back: jumping to the steer must anchor at
    // the question, otherwise the question straddles the hand-off line after
    // landing and the banner unmounts (dead jump chain).
    const items = [asst(), user(), steer(), asst()]
    expect(jumpAnchorIdx(items, 2)).toBe(1)
  })

  it('walks across multiple consecutive user rows', () => {
    const items = [turn(), user(), user(), steer()]
    expect(jumpAnchorIdx(items, 3)).toBe(1)
  })

  it('stops at index 0 when the run reaches the top of the list', () => {
    const items = [user(), steer()]
    expect(jumpAnchorIdx(items, 1)).toBe(0)
  })

  it('does not treat a turn group above the target as part of the run', () => {
    const items = [user(), turn(), user()]
    expect(jumpAnchorIdx(items, 2)).toBe(2)
  })

  // Machine turn openers (nudge, subagent) are NOT prompts to the banner: they
  // are never pinned, so a run of them is an ordinary non-prompt gap and the
  // walk stops at the target. (Only user rows are pinnable — see isPrompt.)

  it('does not walk a subagent fan-out run: the target is its own anchor', () => {
    const items = [user(), asst(), sub(), sub(), sub(), sub()]
    expect(jumpAnchorIdx(items, 4)).toBe(4)
  })

  it('does not walk consecutive nudge cycles', () => {
    const items = [user(), asst(), nudge(), nudge(), nudge(), asst()]
    expect(jumpAnchorIdx(items, 3)).toBe(3)
  })

  it('a turn group between machine openers still returns the target unchanged', () => {
    const items = [user(), asst(), nudge(), turn(), nudge()]
    expect(jumpAnchorIdx(items, 4)).toBe(4)
  })

  it('a machine row directly above a user prompt breaks the run', () => {
    // nudge(1) then user(2): the nudge is not a prompt, so the user prompt is
    // the head of its own run and the walk stops on it.
    const items = [asst(), nudge(), user(), asst()]
    expect(jumpAnchorIdx(items, 2)).toBe(2)
  })

  it('a user prompt followed by machine openers is not extended through them', () => {
    // user(1), sub(2), sub(3): jumping to the second completion does not walk
    // up into the user prompt — the subagent rows are not prompts, so the walk
    // never starts.
    const items = [asst(), user(), sub(), sub(), asst()]
    expect(jumpAnchorIdx(items, 3)).toBe(3)
  })
})
