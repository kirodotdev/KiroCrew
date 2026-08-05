import { describe, expect, it } from 'vitest'

import { selectContinuable } from '../store/chatSlice'
import type { ChatMessage } from '../types'

/**
 * `selectContinuable` decides whether the UI OFFERS to resume a turn that ended
 * without a reply. It mirrors `_is_interrupted` in
 * `src/kiro_crew/dashboard/chat_handlers.py`, which authorizes the resume under
 * the slot lock — these tests pin the predicate so the two cannot drift apart
 * silently.
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
    // there is no turn to pick up.
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

  it('is false after a clean completion (assistant has the floor)', () => {
    expect(selectContinuable(state({
      messages: [msg('user'), msg('assistant', 'all done')],
    }))).toBe(false)
  })

  it('is true when an error row follows the assistant (streamed partway, then died)', () => {
    // Without the trailing error this transcript is shape-identical to a clean
    // completion, so the error row is the only signal that separates them.
    expect(selectContinuable(state({
      messages: [msg('user'), msg('assistant', 'starting…'), msg('error', '⟳ Connection lost — please retry.')],
    }))).toBe(true)
  })

  it('is true when tool rows ran but no assistant text landed', () => {
    expect(selectContinuable(state({
      messages: [msg('user'), msg('tool_call', 'grep'), msg('tool_result', 'hit')],
    }))).toBe(true)
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

  it('skips a compaction notice rather than treating it as the assistant floor', () => {
    expect(selectContinuable(state({
      messages: [msg('user'), msg('assistant', 'Auto-compacted at 80%.', { kind: 'compaction' })],
    }))).toBe(true)
  })

  it('skips an injected recovery row and reads the real floor beneath it', () => {
    expect(selectContinuable(state({
      messages: [msg('user'), msg('inject', '[Continue — requested by the user]\nresume')],
    }))).toBe(true)
  })

  it('ignores an old error once the assistant replied after it', () => {
    // The error belongs to a superseded turn; the conversation moved on.
    expect(selectContinuable(state({
      messages: [msg('user'), msg('error', 'boom'), msg('user', 'again'), msg('assistant', 'done')],
    }))).toBe(false)
  })
})
