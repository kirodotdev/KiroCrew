import { beforeEach, describe, expect, it } from 'vitest'

import { ApiError } from '../api/client'
import {
  agentSwitchFailureMessage,
  agentSwitchFailureReport,
  isTurnInFlightError,
  isWorkspaceUnavailableError,
} from '../utils/agentSwitchFeedback'
import { __resetErrorJournalForTests, findReport, recordError } from '../utils/errorReport'
import chatReducer, { setAgentSwitchNotice } from '../store/chatSlice'

/** The gateway's real refusal shape for a mid-turn switch (chat_handlers.py). */
const turnInFlight409 = () => new ApiError(
  409,
  'a turn is in flight',
  JSON.stringify({ error: 'a turn is in flight', code: 'turn_in_flight' }),
)

/** The gateway's real refusal shape for an unreadable configured workspace root. */
const workspaceUnavailable503 = () => new ApiError(
  503,
  'the configured workspace directory is unavailable',
  JSON.stringify({
    error: 'the configured workspace directory is unavailable',
    code: 'workspace_unavailable',
  }),
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

  it('preserves the new-conversation instruction for a pinned private chat', () => {
    const message = 'This conversation belongs to its original member. Start a new conversation to choose a different member.'
    const error = new ApiError(409, message, JSON.stringify({ error: message, code: 'member_session_pinned' }))
    expect(agentSwitchFailureMessage(error)).toBe(message)
    expect(isTurnInFlightError(error)).toBe(false)
  })

  it('maps a 409 turn_in_flight to the specific retry-later copy', () => {
    // The keyboard cycles (Alt+Shift model/agent cycling) have no disabled
    // state, so this copy is the only way the user learns the switch was
    // refused because a turn is running — not because anything is broken.
    expect(agentSwitchFailureMessage(turnInFlight409()))
      .toBe('A turn is running — try again when it finishes.')
    expect(isTurnInFlightError(turnInFlight409())).toBe(true)
  })

  it('maps a 503 workspace_unavailable to localized copy, not the backend prose', () => {
    // Without this the generic path prefers the API layer's message, which for this
    // refusal is the gateway's own English -- unlocalized, on a localized page.
    const error = workspaceUnavailable503()
    expect(isWorkspaceUnavailableError(error)).toBe(true)
    expect(agentSwitchFailureMessage(error))
      .toBe("The configured project folder isn't available — check that it exists, then try again.")
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

describe('agent switch failure report', () => {
  beforeEach(() => {
    __resetErrorJournalForTests()
  })

  /** What the API layer journals for the 503 — the gateway's own prose, not the localized copy. */
  const journalWorkspace503 = () => recordError({
    source: 'api',
    message: 'the configured workspace directory is unavailable',
    status: 503,
    code: 'workspace_unavailable',
    endpoint: '/api/chat/slots/chat-1/agent',
  })

  it('recovers the 503 context that a message lookup cannot reach', () => {
    const journaled = journalWorkspace503()
    const shown = agentSwitchFailureMessage(workspaceUnavailable503())

    // The defect, asserted rather than described: the notice carries localized copy while the
    // journal is keyed on the backend's prose, so ErrorNotice's own lookup finds nothing. If this
    // ever starts passing, the two strings have converged and the wire-contract path is moot.
    expect(findReport(shown)).toBeUndefined()

    const recovered = agentSwitchFailureReport(shown)
    expect(recovered?.id).toBe(journaled.id)
    expect(recovered?.status).toBe(503)
    expect(recovered?.code).toBe('workspace_unavailable')
    expect(recovered?.endpoint).toBe('/api/chat/slots/chat-1/agent')
    expect(recovered?.route).toBe(journaled.route)
  })

  it('returns nothing when the refusal never reached the journal', () => {
    // Negative control for the test above: same message, empty journal. Without this, an
    // implementation that returned any arbitrary report would still pass.
    expect(agentSwitchFailureReport(agentSwitchFailureMessage(workspaceUnavailable503())))
      .toBeUndefined()
  })

  it('prefers the live refusal over an identically coded earlier one', () => {
    journalWorkspace503()
    const newer = recordError({
      source: 'api',
      message: 'the configured workspace directory is unavailable',
      status: 503,
      code: 'workspace_unavailable',
      endpoint: '/api/chat/slots/chat-7/agent',
    })
    expect(agentSwitchFailureReport(agentSwitchFailureMessage(workspaceUnavailable503()))?.endpoint)
      .toBe('/api/chat/slots/chat-7/agent')
    expect(agentSwitchFailureReport(agentSwitchFailureMessage(workspaceUnavailable503()))?.id)
      .toBe(newer.id)
  })

  it('resolves the in-flight refusal on its own code, not the workspace one', () => {
    journalWorkspace503()
    const busy = recordError({
      source: 'api',
      message: 'a turn is in flight',
      status: 409,
      code: 'turn_in_flight',
      endpoint: '/api/chat/slots/chat-1/agent',
    })
    // Both localized, both unreachable by message — so a resolver keyed on anything looser than
    // the status+code pair would hand the busy notice the workspace failure's context.
    const recovered = agentSwitchFailureReport(agentSwitchFailureMessage(turnInFlight409()))
    expect(recovered?.id).toBe(busy.id)
    expect(recovered?.code).toBe('turn_in_flight')
  })

  it('keeps the exact message lookup for a failure whose copy was not replaced', () => {
    const journaled = recordError({
      source: 'api',
      message: 'invalid agent name',
      status: 400,
      endpoint: '/api/chat/slots/chat-1/agent',
    })
    const error = new ApiError(400, 'invalid agent name', JSON.stringify({ error: 'invalid agent name' }))
    const shown = agentSwitchFailureMessage(error)
    // This path passes the API layer's message through untouched, so the journal key matches and
    // there is no code to fall back on — the generic branch has to stay.
    expect(shown).toBe('invalid agent name')
    expect(agentSwitchFailureReport(shown)?.id).toBe(journaled.id)
  })

  it('renders nothing to resolve for an absent message', () => {
    expect(agentSwitchFailureReport(null)).toBeUndefined()
    expect(agentSwitchFailureReport('')).toBeUndefined()
  })
})
