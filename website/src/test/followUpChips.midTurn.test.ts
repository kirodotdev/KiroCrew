/**
 * Option chips from an earlier segment hide once the running turn moves on (#15926).
 *
 * The main chat gated chips on "the newest row is streaming". After a `_segment`
 * frame finalized the text into an `assistant` row, the `tool` / `thinking` rows
 * that followed closed that gate, and the chips of the earlier segment stayed up
 * for a question the agent had already moved past.
 *
 * The chips still show mid-turn while the offering row is the newest row, so a
 * pick can be steered into the turn (pinned by ChatPage.followUpBusySend.test.tsx).
 */
import { describe, it, expect } from 'vitest'
import { renderHook } from '@testing-library/react'
import reducer, { setActiveSlot, startLocalTurn, appendMessage, sseChatMessage } from '../store/chatSlice'
import { deriveFollowUpOptions } from '../app-sdk/protocol'
import { useFollowUpChips, isTurnActivityRow } from '../pages/chat/page/followUpChips'
import type { ChatMessage } from '../types'

const S = 'chat-1'
const OFFER = 'Please log in again.\n\n[OPTIONS: Done, check now | Wait]'

/** The issue's sequence through the real reducer: a segment with options, then tool work. */
function runningTurnState(after: 'tool+thinking' | 'nothing') {
  let st = reducer(undefined, { type: '@@init' })
  st = reducer(st, setActiveSlot(S))
  st = reducer(st, appendMessage({ role: 'user', content: 'check it', cls: 'msg msg-u' }))
  st = reducer(st, startLocalTurn(S))
  st = reducer(st, sseChatMessage({ slot: S, role: 'chunk', content: OFFER, seq: 1 }))
  st = reducer(st, sseChatMessage({ slot: S, role: '_segment', content: '' }))
  if (after === 'tool+thinking') {
    st = reducer(st, sseChatMessage({ slot: S, role: 'tool', content: 'read comments', ts: '2026-10-01T00:00:00Z' }))
    st = reducer(st, sseChatMessage({ slot: S, role: 'thinking', content: '' }))
  }
  return st
}

function chipsFor(messages: ChatMessage[], turnRunning: boolean) {
  const isStreaming = messages[messages.length - 1]?.role === 'streaming'
  const { result } = renderHook(() => useFollowUpChips({
    messages,
    isStreaming,
    turnRunning,
    pendingQuestion: null,
    activeSlot: S,
    inputRef: { current: '' },
    setInput: () => {},
    prefillEdited: false,
    setPrefillEdited: () => {},
  }))
  return result.current.followUpOptions
}

describe('main chat chips while the turn is still running (#15926)', () => {
  it('hides the earlier segment\'s chips once tool and thinking rows follow it', () => {
    const st = runningTurnState('tool+thinking')
    expect(st.slotRunning).toBe(true)
    expect(st.messages.map(m => m.role).slice(-3)).toEqual(['assistant', 'tool', 'thinking'])
    expect(chipsFor(st.messages, !!st.slotRunning)).toEqual([])
  })

  it('keeps the chips while the offering row is still the newest row', () => {
    const st = runningTurnState('nothing')
    expect(st.slotRunning).toBe(true)
    expect(chipsFor(st.messages, !!st.slotRunning)).toEqual(['Done, check now', 'Wait'])
  })

  it('brings the chips back when the turn ends', () => {
    const st = runningTurnState('tool+thinking')
    expect(chipsFor(st.messages, false)).toEqual(['Done, check now', 'Wait'])
  })
})

describe('deriveFollowUpOptions movedOnRow', () => {
  const user: ChatMessage = { role: 'user', content: 'go', cls: 'msg msg-u' }
  const offer: ChatMessage = { role: 'assistant', content: OFFER, cls: 'msg msg-a' }
  const tool: ChatMessage = { role: 'tool', content: 'read comments', cls: '' }
  const thinking: ChatMessage = { role: 'thinking', content: '', cls: '' }

  it('a tool row after the offer hides it while the turn runs', () => {
    expect(deriveFollowUpOptions([user, offer, tool], false, false, isTurnActivityRow).followUpOptions).toEqual([])
  })

  it('a thinking row after the offer hides it while the turn runs', () => {
    expect(deriveFollowUpOptions([user, offer, thinking], false, false, isTurnActivityRow).followUpOptions).toEqual([])
  })

  it('callers that pass no movedOnRow keep the old scan', () => {
    expect(deriveFollowUpOptions([user, offer, tool], false).followUpOptions).toEqual(['Done, check now', 'Wait'])
  })

  it('a newer offer after the tool rows still shows mid-turn', () => {
    const later: ChatMessage = { role: 'assistant', content: 'Next?\n\n[OPTIONS: Yes | No]', cls: 'msg msg-a' }
    expect(deriveFollowUpOptions([user, offer, tool, later], false, false, isTurnActivityRow).followUpOptions).toEqual(['Yes', 'No'])
  })
})
