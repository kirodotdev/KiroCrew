import { describe, expect, it } from 'vitest'

import { ApiError } from '../api/client'
import { agentSwitchFailureMessage, isTurnInFlightError } from '../utils/agentSwitchFeedback'
import chatReducer, { setAgentSwitchNotice } from '../store/chatSlice'

/** The gateway's real refusal shape for a mid-turn switch (chat_handlers.py). */
const turnInFlight409 = () => new ApiError(
  409,
  'a turn is in flight',
  JSON.stringify({ error: 'a turn is in flight', code: 'turn_in_flight' }),
)

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

  it('maps a 409 turn_in_flight to the specific retry-later copy', () => {
    // The keyboard cycles (Alt+Shift model/agent cycling) have no disabled
    // state, so this copy is the only way the user learns the switch was
    // refused because a turn is running — not because anything is broken.
    expect(agentSwitchFailureMessage(turnInFlight409()))
      .toBe('A turn is running — try again when it finishes.')
    expect(isTurnInFlightError(turnInFlight409())).toBe(true)
  })

  it('detects the refusal structurally, without the ApiError class', () => {
    // Several switch-surface suites vi.mock('../api/client') with factories
    // that export no ApiError, and their components call this helper inside
    // failure handlers. An instanceof against the (then-undefined) class
    // would throw there, so the check must hold for any error carrying the
    // ApiError SHAPE — this plain object is exactly what those suites see.
    const shapedError = {
      status: 409,
      message: 'a turn is in flight',
      body: JSON.stringify({ error: 'a turn is in flight', code: 'turn_in_flight' }),
    }
    expect(isTurnInFlightError(shapedError)).toBe(true)
    expect(agentSwitchFailureMessage(shapedError))
      .toBe('A turn is running — try again when it finishes.')
  })

  it('matches on the structured code, not the 409 status alone', () => {
    // A different 409 (e.g. slot_orchestrating) keeps its own server message.
    const other409 = new ApiError(
      409,
      'slot is orchestrating',
      JSON.stringify({ error: 'slot is orchestrating', code: 'slot_orchestrating' }),
    )
    expect(agentSwitchFailureMessage(other409)).toBe('slot is orchestrating')
    expect(isTurnInFlightError(other409)).toBe(false)
  })

  it('requires the 409 status alongside the code word', () => {
    // The code travels in the body; a non-409 reuse of the word elsewhere
    // must not trip the busy copy.
    const non409 = new ApiError(
      400,
      'bad request',
      JSON.stringify({ error: 'bad request', code: 'turn_in_flight' }),
    )
    expect(agentSwitchFailureMessage(non409)).toBe('bad request')
    expect(isTurnInFlightError(non409)).toBe(false)
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
