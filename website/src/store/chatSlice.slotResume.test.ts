/**
 * `selectContinuable` / `selectTurnInterrupted` name the session they answer
 * about (#20692). A pane shows a slot that is usually not the active one (a
 * crewmate's chat on the Members page never is), so the two selectors cannot
 * read the active mirror for it: its rows are `slotMessages[slot]`, its run
 * state is `slotRun[slot]` plus the slots frame, and the active slot's own turn
 * says nothing about it. Called with no slot they still read the active one,
 * which is what `useAppSelector(selectContinuable)` in ChatPage does.
 */
import { describe, it, expect } from 'vitest'
import chatReducer, { selectContinuable, selectTurnInterrupted } from './chatSlice'
import type { ChatMessage, ChatSlot } from '../types'
import type { RootState } from './index'

const ACTIVE = 'front'
const PANE = 'member-radar'
const THROTTLED = 'Bedrock is throttling requests. Try: (1) wait a few seconds and retry, or (2) switch to a different model in the picker (e.g. sonnet).'

const msg = (role: string, content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', ts: '2026-10-14T09:00:00Z', ...(meta ? { meta } : {}) }) as ChatMessage

const CLEAN = [msg('user', 'hi'), msg('assistant', 'hello')]
const THROTTLED_TURN = [msg('user', 'status?'), msg('tool', '🔧 gh run list'), msg('error', THROTTLED)]

interface Setup {
  active?: ChatMessage[]
  pane?: ChatMessage[]
  paneRun?: 'streaming' | 'stopping'
  slots?: Partial<ChatSlot>[]
  pending?: string | null
  activeRunning?: boolean
}

function stateOf(opts: Setup): RootState {
  const base = chatReducer(undefined, { type: '@@INIT' })
  return {
    chat: {
      ...base,
      activeSlot: ACTIVE,
      messages: opts.active ?? [],
      slotRunning: !!opts.activeRunning,
      slotMessages: opts.pane ? { [PANE]: opts.pane } : {},
      slotRun: opts.paneRun ? { [PANE]: { state: opts.paneRun } } : {},
      pendingTurnSlot: opts.pending ?? null,
    },
    dashboard: { slots: (opts.slots ?? []).map((s) => ({ key: PANE, ...s })) },
  } as unknown as RootState
}

describe('Resume selectors for a slot other than the active one', () => {
  it('read the named slot, not the active mirror', () => {
    const s = stateOf({ active: CLEAN, pane: THROTTLED_TURN })

    expect(selectTurnInterrupted(s)).toBe(false)
    expect(selectTurnInterrupted(s, ACTIVE)).toBe(false)
    expect(selectTurnInterrupted(s, PANE)).toBe(true)
    expect(selectContinuable(s, PANE)).toBe(true)
  })

  it('give the active slot the same answer whether it is named or not', () => {
    const s = stateOf({ active: THROTTLED_TURN })

    expect(selectTurnInterrupted(s)).toBe(true)
    expect(selectTurnInterrupted(s, ACTIVE)).toBe(true)
    expect(selectTurnInterrupted(s, null)).toBe(true)
    expect(selectContinuable(s)).toBe(true)
    expect(selectContinuable(s, ACTIVE)).toBe(true)
    expect(selectContinuable(s, null)).toBe(true)
  })

  it('see a turn that ended cleanly as finished', () => {
    const s = stateOf({ active: THROTTLED_TURN, pane: CLEAN })

    expect(selectTurnInterrupted(s, PANE)).toBe(false)
    // Resume is offered on a settled conversation only where the caller also
    // asks `selectTurnInterrupted`, as ChatPage and the pane do.
    expect(selectContinuable(s, PANE)).toBe(true)
  })

  it('read an unanswered patrol wake as the interrupted turn', () => {
    const s = stateOf({ pane: [msg('user', 'watch the queue'), msg('assistant', 'ok'), msg('nudge', '[auto-nudge cycle 2]\nPatrol.'), msg('tool', '🔧 gh issue list'), msg('error', THROTTLED)] })

    expect(selectTurnInterrupted(s, PANE)).toBe(true)
    expect(selectContinuable(s, PANE)).toBe(true)
  })

  it.each<[string, Setup]>([
    ['its stream state says a turn is running', { paneRun: 'streaming' }],
    ['its stream state says it is stopping', { paneRun: 'stopping' }],
    ['the slots frame says it is running', { slots: [{ running: true }] }],
    ['the slots frame says it is stopping', { slots: [{ stopping: true }] }],
    ['a turn started here is waiting for its receipt', { pending: PANE }],
    ['sub-agents are still running for it', { slots: [{ subagents_running: true }] }],
    ['it is a relay archive of a turn that ran elsewhere', { slots: [{ executor: 'remote' }] }],
  ])('withhold Continue from the named slot when %s', (_why, opts) => {
    const s = stateOf({ pane: THROTTLED_TURN, ...opts })

    expect(selectContinuable(s, PANE)).toBe(false)
  })

  it('do not borrow the active slot run state or its receipt wait', () => {
    // The active slot is mid-turn and another slot is the one waiting on a
    // receipt; neither says anything about the pane's own idle slot.
    const s = stateOf({ active: CLEAN, pane: THROTTLED_TURN, activeRunning: true, pending: ACTIVE })

    expect(selectContinuable(s, PANE)).toBe(true)
    expect(selectContinuable(s)).toBe(false)
  })

  it('withhold Continue while a message is queued behind the slot, and from a slot with no rows', () => {
    expect(selectContinuable(stateOf({ pane: [...THROTTLED_TURN, msg('queued', 'and one more thing')] }), PANE)).toBe(false)
    expect(selectContinuable(stateOf({ pane: [] }), PANE)).toBe(false)
    expect(selectContinuable(stateOf({}), 'no-such-slot')).toBe(false)
    expect(selectTurnInterrupted(stateOf({}), 'no-such-slot')).toBe(false)
  })
})
