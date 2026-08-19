import { describe, it, expect } from 'vitest'
import { jumpAnchorIdx } from '../utils/pinnedPrompt'
import type { DisplayItem } from '../pages/chat/groupDisplayItems'

// Minimal display items: only kind and msg.role are read by the helper.
const user = (): DisplayItem => ({ kind: 'single', msg: { role: 'user', content: 'q' } } as unknown as DisplayItem)
const steer = (): DisplayItem => ({ kind: 'single', msg: { role: 'user', content: 's', meta: { steer: true } } } as unknown as DisplayItem)
const asst = (): DisplayItem => ({ kind: 'single', msg: { role: 'assistant', content: 'a' } } as unknown as DisplayItem)
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
})
