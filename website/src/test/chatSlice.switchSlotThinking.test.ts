import { describe, it, expect } from 'vitest'
import reducer from '../store/chatSlice'
import type { ChatMessage } from '../types'
import './mockApiClient'

/**
 * Regression test: switching away from a slot and back must preserve
 * client-only thinking (reasoning) blocks. The backend never persists
 * thinking, so switchSlot.fulfilled must re-insert them via
 * mergePreservedThinking — otherwise they vanish on tab switch.
 */

const SLOT = 'thinking-slot'
const init = () => reducer(undefined, { type: '@@INIT' })

function makeState(messages: ChatMessage[]) {
  return { ...init(), activeSlot: SLOT, messages }
}

function switchAway(state: ReturnType<typeof makeState>) {
  return reducer(state, {
    type: 'chat/switchSlot/pending',
    meta: { arg: 'other-slot', requestId: 'r1', requestStatus: 'pending' },
  })
}

function switchBack(state: ReturnType<typeof makeState>, serverMessages: ChatMessage[]) {
  // First set activeSlot via pending (switchSlot.pending sets activeSlot)
  let s = reducer(state, {
    type: 'chat/switchSlot/pending',
    meta: { arg: SLOT, requestId: 'r2', requestStatus: 'pending' },
  })
  // Then fulfill with the server history (which lacks thinking)
  s = reducer(s, {
    type: 'chat/switchSlot/fulfilled',
    meta: { arg: SLOT, requestId: 'r2', requestStatus: 'fulfilled' },
    payload: {
      key: SLOT,
      messages: serverMessages,
      running: false,
      hasMore: false,
      queue: [],
    },
  })
  return s
}

describe('switchSlot preserves thinking blocks', () => {
  it('retains a thinking block after switching away and back', () => {
    const thinking: ChatMessage = { role: 'thinking', content: 'Let me analyze the code...', cls: '' }
    const assistant: ChatMessage = { role: 'assistant', content: 'Here is the analysis.', cls: '' }
    const user: ChatMessage = { role: 'user', content: 'Analyze this', cls: '' }

    // State has thinking + assistant (as rendered during streaming)
    const state = makeState([user, thinking, assistant])

    // Switch away
    const away = switchAway(state)

    // Switch back — server history only has user + assistant (no thinking)
    const back = switchBack(away, [user, assistant])

    // Thinking block must be preserved
    const thinkingMsgs = back.messages.filter((m) => m.role === 'thinking')
    expect(thinkingMsgs).toHaveLength(1)
    expect(thinkingMsgs[0].content).toBe('Let me analyze the code...')
  })

  it('does not preserve empty thinking placeholders', () => {
    const emptyThinking: ChatMessage = { role: 'thinking', content: '', cls: '' }
    const assistant: ChatMessage = { role: 'assistant', content: 'Done.', cls: '' }

    const state = makeState([emptyThinking, assistant])
    const away = switchAway(state)
    const back = switchBack(away, [assistant])

    // Empty thinking should NOT be preserved (it's just a placeholder)
    expect(back.messages.filter((m) => m.role === 'thinking')).toHaveLength(0)
  })

  it('preserves multiple thinking blocks across a multi-tool turn', () => {
    const user: ChatMessage = { role: 'user', content: 'Do a complex task', cls: '' }
    const think1: ChatMessage = { role: 'thinking', content: 'First I need to read the file', cls: '' }
    const tool: ChatMessage = { role: 'tool', content: 'read file.ts', cls: '' }
    const think2: ChatMessage = { role: 'thinking', content: 'Now I will apply the fix', cls: '' }
    const assistant: ChatMessage = { role: 'assistant', content: 'Fixed it.', cls: '' }

    const state = makeState([user, think1, tool, think2, assistant])
    const away = switchAway(state)
    const back = switchBack(away, [user, tool, assistant])

    const thinkingMsgs = back.messages.filter((m) => m.role === 'thinking')
    expect(thinkingMsgs).toHaveLength(2)
    expect(thinkingMsgs[0].content).toBe('First I need to read the file')
    expect(thinkingMsgs[1].content).toBe('Now I will apply the fix')
  })
})

describe('switchSlot preserves thinking position on still-running slot', () => {
  it('anchors thinking above the streaming row (not appended below)', () => {
    const user: ChatMessage = { role: 'user', content: 'Explain this', cls: '' }
    const thinking: ChatMessage = { role: 'thinking', content: 'Analyzing the code...', cls: '' }
    const streaming: ChatMessage = { role: 'streaming', content: 'Here is my partial', cls: '' }

    // Mid-stream state: user -> thinking -> streaming (still generating)
    const state = makeState([user, thinking, streaming])
    const away = switchAway(state)

    // Switch back while still running — server has user + streaming partial
    let s = reducer(away, {
      type: 'chat/switchSlot/pending',
      meta: { arg: SLOT, requestId: 'r3', requestStatus: 'pending' },
    })
    s = reducer(s, {
      type: 'chat/switchSlot/fulfilled',
      meta: { arg: SLOT, requestId: 'r3', requestStatus: 'fulfilled' },
      payload: {
        key: SLOT,
        messages: [user, streaming],
        running: true,
        hasMore: false,
        queue: [],
      },
    })

    // Thinking must appear BEFORE streaming, not appended after
    const roles = s.messages.map((m) => m.role)
    const thinkIdx = roles.indexOf('thinking')
    const streamIdx = roles.indexOf('streaming')
    expect(thinkIdx).toBeGreaterThan(-1)
    expect(streamIdx).toBeGreaterThan(-1)
    expect(thinkIdx).toBeLessThan(streamIdx)
  })
})
