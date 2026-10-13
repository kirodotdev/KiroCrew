import { describe, expect, it } from 'vitest'
import { slotListHoldTest } from '../utils/slotListHold'

describe('slotListHoldTest', () => {
  it('holds every key while the list is partial', () => {
    const holds = slotListHoldTest({ partial: true, heldKeys: [], holdChannels: false })
    expect(holds('chat-1-1')).toBe(true)
    expect(holds('slack_C1_1')).toBe(true)
  })

  it('holds only named keys, in both spellings of an unsafe key, once complete', () => {
    const holds = slotListHoldTest({ partial: false, heldKeys: ['chat-held', '__proto__'], holdChannels: false })
    expect(holds('chat-held')).toBe(true)
    expect(holds('__proto__')).toBe(true)
    expect(holds('unsafe-key:__proto__')).toBe(true)
    expect(holds('chat-gone')).toBe(false)
  })

  it('holds channel-origin keys only under holdChannels', () => {
    const channelKey = 'slack_C123_1712345678.000100'
    expect(slotListHoldTest({ partial: false, heldKeys: [], holdChannels: true })(channelKey)).toBe(true)
    expect(slotListHoldTest({ partial: false, heldKeys: [], holdChannels: true })('chat-1-1')).toBe(false)
    expect(slotListHoldTest({ partial: false, heldKeys: [], holdChannels: false })(channelKey)).toBe(false)
  })
})
