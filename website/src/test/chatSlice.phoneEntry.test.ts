/**
 * On a phone the side panel is a sheet over the whole chat, so entering a chat
 * lands on the chat: a chat left with its sheet up (a worker opened from the
 * Board, the Sessions drawer) does not come back covered. A desktop keeps each
 * chat's saved panel state.
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import reducer, { openActivityPanel, setActiveSlot, toggleActivity } from '../store/chatSlice'
import './mockApiClient'

const initial = () => reducer(undefined, { type: '@@INIT' })
const switchTo = (arg: string) => ({
  type: 'chat/switchSlot/pending' as const,
  meta: { arg, requestId: `switch-${arg}`, requestStatus: 'pending' as const },
})
/** Open chat A's panel, leave A for B, then come back to A. */
const leaveOpenAndReturn = () => {
  let s = reducer(initial(), setActiveSlot('A'))
  s = reducer(s, toggleActivity())
  expect(s.activityOpen).toBe(true)
  s = reducer(s, switchTo('B'))
  return reducer(s, switchTo('A'))
}

beforeEach(() => { localStorage.clear() })
afterEach(() => { delete document.documentElement.dataset.layoutForm })

describe('entering a chat on a phone', () => {
  it('lands on the chat even though the chat was left with its sheet open', () => {
    document.documentElement.dataset.layoutForm = 'phone'
    const s = leaveOpenAndReturn()
    expect(s.activeSlot).toBe('A')
    expect(s.activityOpen).toBe(false)
  })

  it('still opens the sheet on a press after the switch', () => {
    document.documentElement.dataset.layoutForm = 'phone'
    let s = reducer(initial(), setActiveSlot('A'))
    s = reducer(s, switchTo('B'))
    s = reducer(s, openActivityPanel())
    expect(s.activityOpen).toBe(true)
  })

  it("restores a chat's open panel on a desktop", () => {
    document.documentElement.dataset.layoutForm = 'desktop'
    expect(leaveOpenAndReturn().activityOpen).toBe(true)
  })
})
