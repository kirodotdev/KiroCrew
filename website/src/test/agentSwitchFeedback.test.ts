import { describe, expect, it } from 'vitest'

import { ApiError } from '../api/client'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import chatReducer, { setAgentSwitchNotice } from '../store/chatSlice'

describe('agent switch failure feedback', () => {
  it('surfaces the message a real ApiError carries', () => {
    // The production error shape, not a hand-rolled stand-in: this is what
    // `api.chatSlotAgent` actually rejects with, so the test proves the real
    // plumbing supplies something useful rather than that the helper reads a
    // field the app never sets.
    const error = new ApiError(400, 'invalid agent name', JSON.stringify({ error: 'invalid agent name' }))
    expect(agentSwitchFailureMessage(error)).toBe('invalid agent name')
  })

  it('surfaces a slot that no longer exists', () => {
    const error = new ApiError(404, 'not found', JSON.stringify({ error: 'not found' }))
    expect(agentSwitchFailureMessage(error)).toBe('not found')
  })

  it('falls back to generic copy when the rejection carries no message', () => {
    // A network-layer rejection is not an ApiError and may carry no usable
    // text; the user still has to be told something happened.
    expect(agentSwitchFailureMessage(new Error(''))).toBe('Something went wrong')
    expect(agentSwitchFailureMessage('offline')).toBe('Something went wrong')
    expect(agentSwitchFailureMessage(null)).toBe('Something went wrong')
  })

  it('stores and clears the shared chat notice', () => {
    const initial = chatReducer(undefined, { type: 'test/init' })
    const failed = chatReducer(initial, setAgentSwitchNotice('invalid agent name'))
    expect(failed.agentSwitchNotice?.message).toBe('invalid agent name')
    // A repeat of the same message must be a fresh value, or the App shell's
    // expiry effect keeps the first notice's timer instead of restarting it.
    const repeated = chatReducer(failed, setAgentSwitchNotice('invalid agent name'))
    expect(repeated.agentSwitchNotice).not.toBe(failed.agentSwitchNotice)
    expect(chatReducer(failed, setAgentSwitchNotice(null)).agentSwitchNotice).toBeNull()
  })
})
