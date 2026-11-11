import { describe, expect, it } from 'vitest'

import { markChallengeAutoSend, sendAsChallenge, takeChallengeAutoSend, takeChallengeSend } from '../utils/channelChallengeSend'

describe('channelChallengeSend', () => {
  it('marks only the owner that armed it, for the marked prompt, once', () => {
    const page = { current: null }
    const otherPage = { current: null }
    markChallengeAutoSend(page, 'look into this')
    // A page that did not arm it (the one a person moved to) sees no mark.
    expect(takeChallengeAutoSend(otherPage, 'look into this')).toBe(false)
    expect(takeChallengeAutoSend(page, 'look into this')).toBe(true)
    expect(takeChallengeAutoSend(page, 'look into this')).toBe(false)
  })

  it('clears the mark when the auto-send carries other text', () => {
    const page = { current: null }
    markChallengeAutoSend(page, 'look into this')
    expect(takeChallengeAutoSend(page, 'a staged hand-off')).toBe(false)
    expect(takeChallengeAutoSend(page, 'look into this')).toBe(false)
  })

  it('is visible only to the send that starts inside sendAsChallenge', () => {
    expect(takeChallengeSend()).toBe(false)
    expect(sendAsChallenge(() => takeChallengeSend())).toBe(true)
    // Not left armed for the next send, even when the send never read it.
    sendAsChallenge(() => undefined)
    expect(takeChallengeSend()).toBe(false)
  })
})
