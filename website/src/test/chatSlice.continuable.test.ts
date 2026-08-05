import { describe, expect, it } from 'vitest'

import { selectContinuable, selectTurnInterrupted } from '../store/chatSlice'
import type { ChatMessage } from '../types'

/**
 * Two predicates, two jobs.
 *
 * `selectContinuable` decides whether the UI OFFERS Continue on an empty
 * composer — mirroring `_has_conversation` in
 * `src/kiro_crew/dashboard/chat_handlers.py`, which authorizes the press under
 * the slot lock. `selectTurnInterrupted` only decides what the button SAYS,
 * mirroring `_is_interrupted`, which makes the same split to pick the
 * continuation body handed to the model.
 *
 * These tests pin both so the pairs cannot drift apart silently — a drift on the
 * first pair means the button appears where the server refuses it, and on the
 * second it means the button promises one thing while the agent is told another.
 */
const msg = (role: string, content = 'x', meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', ...(meta ? { meta } : {}) }) as ChatMessage

const state = (over: Partial<{ messages: ChatMessage[]; slotRunning: boolean; slotStopping: boolean; pendingTurnSlot: string | null }> = {}, slots: Array<{ key: string; orchestrating?: boolean; subagents_running?: boolean }> = []) =>
  ({
    chat: {
      messages: [],
      slotRunning: false,
      slotStopping: false,
      pendingTurnSlot: null,
      activeSlot: 'slot-1',
      ...over,
    },
    dashboard: { slots },
  }) as never

describe('selectContinuable', () => {
  it('is false for a brand-new chat with no messages', () => {
    // The composer's send button must stay disabled exactly as it is today —
    // there is no conversation to hand back.
    expect(selectContinuable(state())).toBe(false)
  })

  it('is true when the last conversational row is the user (nothing came back)', () => {
    // The gateway-restart-during-an-update shape: the turn's task died with the
    // process and nothing was ever appended.
    expect(selectContinuable(state({ messages: [msg('user', 'do the thing')] }))).toBe(true)
  })

  it('is true for the first turn of a chat when it produced nothing', () => {
    // A first turn that dies still deserves recovery; only a ZERO-message
    // session is excluded.
    expect(selectContinuable(state({ messages: [msg('user', 'first ever prompt')] }))).toBe(true)
  })

  it('is true after a clean completion, so the button doubles as "keep going"', () => {
    // This is the case a force-quit lands in: os._exit runs no cleanup, so no
    // error row is ever written and a KILLED turn is shape-identical to this
    // one. Refusing here is what left the user with no way back.
    expect(selectContinuable(state({
      messages: [msg('user'), msg('assistant', 'all done')],
    }))).toBe(true)
  })

  it('is true when an error row follows the assistant (streamed partway, then died)', () => {
    expect(selectContinuable(state({
      messages: [msg('user'), msg('assistant', 'starting…'), msg('error', '⟳ Connection lost — please retry.')],
    }))).toBe(true)
  })

  it('is true when tool rows ran but no assistant text landed', () => {
    expect(selectContinuable(state({
      messages: [msg('user'), msg('tool_call', 'grep'), msg('tool_result', 'hit')],
    }))).toBe(true)
  })

  it('is false when the transcript holds only a compaction notice', () => {
    // Scaffolding, not conversation: nothing for a continuation to reason from.
    // Mirrors `_has_conversation`, which skips the same row.
    expect(selectContinuable(state({
      messages: [msg('assistant', 'Auto-compacted at 80%.', { kind: 'compaction' })],
    }))).toBe(false)
  })

  it('is false when the transcript holds only non-conversational rows', () => {
    expect(selectContinuable(state({
      messages: [msg('tool_call', 'grep'), msg('tool_result', 'hit')],
    }))).toBe(false)
  })

  it('is false when a user row exists but carries no content', () => {
    expect(selectContinuable(state({ messages: [msg('user', '')] }))).toBe(false)
  })

  it('is false while a turn is running', () => {
    expect(selectContinuable(state({ messages: [msg('user')], slotRunning: true }))).toBe(false)
  })

  it('is false while a stop is in flight', () => {
    expect(selectContinuable(state({ messages: [msg('user')], slotStopping: true }))).toBe(false)
  })

  it('is false while an optimistic local turn is pending', () => {
    expect(selectContinuable(state({ messages: [msg('user')], pendingTurnSlot: 'slot-1' }))).toBe(false)
  })

  it('is false while an autopilot plan is mid-flight', () => {
    // A plan reads `running` False BETWEEN stages, so `running` alone would offer
    // Continue on a slot the server refuses with `slot_orchestrating`.
    expect(selectContinuable(state({ messages: [msg('user')] }, [{ key: 'slot-1', orchestrating: true }]))).toBe(false)
  })

  it('is false while a subagent is still running on the slot', () => {
    expect(selectContinuable(state({ messages: [msg('user')] }, [{ key: 'slot-1', subagents_running: true }]))).toBe(false)
  })

  it('is unaffected by another slot orchestrating', () => {
    expect(selectContinuable(state({ messages: [msg('user')] }, [{ key: 'other', orchestrating: true }]))).toBe(true)
  })

  it('is false when a queued message is waiting — the runner will resume on its own', () => {
    // Offering Continue here would double-fire the turn.
    expect(selectContinuable(state({
      messages: [msg('user'), msg('queued', 'next one')],
    }))).toBe(false)
  })

  it('reads past an injected recovery row to the real floor beneath it', () => {
    expect(selectContinuable(state({
      messages: [msg('user'), msg('inject', '[Continue — requested by the user]\nresume')],
    }))).toBe(true)
  })
})

describe('selectTurnInterrupted', () => {
  it('is false for a brand-new chat with no messages', () => {
    expect(selectTurnInterrupted(state())).toBe(false)
  })

  it('is true when the last conversational row is the user (nothing came back)', () => {
    expect(selectTurnInterrupted(state({ messages: [msg('user', 'do the thing')] }))).toBe(true)
  })

  it('is true when an error row follows the assistant', () => {
    // Without the trailing error this transcript is shape-identical to a clean
    // completion, so the error row is the only signal that separates them.
    expect(selectTurnInterrupted(state({
      messages: [msg('user'), msg('assistant', 'starting…'), msg('error', 'boom')],
    }))).toBe(true)
  })

  it('is FALSE after a clean completion, even though Continue is still offered', () => {
    // The distinction the two selectors exist for: the button is available, but
    // it must not claim an interruption the transcript does not show.
    const s = state({ messages: [msg('user'), msg('assistant', 'all done')] })
    expect(selectContinuable(s)).toBe(true)
    expect(selectTurnInterrupted(s)).toBe(false)
  })

  it('is false after a force-quit that left no error row', () => {
    // Honest, and the reason `selectContinuable` cannot key on this: the turn
    // WAS killed mid-flight, but nothing recorded it, so the copy stays neutral
    // rather than guessing.
    expect(selectTurnInterrupted(state({
      messages: [msg('user'), msg('assistant', 'starting…'), msg('tool_call', 'grep')],
    }))).toBe(false)
  })

  it('diverges from selectContinuable on a superseded error row', () => {
    // The ErrorCard contract: its Continue button is wired to `interrupted`, not
    // `continuable`, because `i === lastErrorIdx` only means "newest error row" —
    // never "the transcript ends badly". Here the newest error is mid-transcript
    // and a later turn completed, so the composer stays continuable while the
    // stale failure card must NOT offer to resume a request it does not describe.
    const s = state({
      messages: [msg('user', 'a'), msg('error', 'boom'), msg('user', 'b'), msg('assistant', 'done')],
    })
    expect(selectContinuable(s)).toBe(true)
    expect(selectTurnInterrupted(s)).toBe(false)
  })

  it('is true while a turn runs, so it cannot gate the ErrorCard alone', () => {
    // Why the ErrorCard needs `continuable && interrupted`, not `interrupted`
    // alone: this predicate carries NONE of the busy checks. Gating the card on
    // it by itself renders a live Continue that handleContinue early-returns on —
    // a dead control. Both halves are load-bearing and neither is sufficient.
    const s = state({ messages: [msg('user', 'a'), msg('error', 'boom')], slotRunning: true })
    expect(selectTurnInterrupted(s)).toBe(true)
    expect(selectContinuable(s)).toBe(false)
  })

  it('is true with a message queued, which also cannot gate the card alone', () => {
    // `queued` is in CONTINUE_SCAN_SKIP, so this scan walks past it rather than
    // refusing — only selectContinuable has the queued early-return.
    const s = state({ messages: [msg('user', 'a'), msg('error', 'boom'), msg('queued', 'next')] })
    expect(selectTurnInterrupted(s)).toBe(true)
    expect(selectContinuable(s)).toBe(false)
  })

  it('ignores an old error once the assistant replied after it', () => {
    // The error belongs to a superseded turn; the conversation moved on.
    expect(selectTurnInterrupted(state({
      messages: [msg('user'), msg('error', 'boom'), msg('user', 'again'), msg('assistant', 'done')],
    }))).toBe(false)
  })

  it('skips a compaction notice rather than treating it as the assistant floor', () => {
    expect(selectTurnInterrupted(state({
      messages: [msg('user'), msg('assistant', 'Auto-compacted at 80%.', { kind: 'compaction' })],
    }))).toBe(true)
  })

  it('reads past an injected recovery row to the real floor beneath it', () => {
    expect(selectTurnInterrupted(state({
      messages: [msg('user'), msg('inject', '[Continue — requested by the user]\nresume')],
    }))).toBe(true)
  })
})
