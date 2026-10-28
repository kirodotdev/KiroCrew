/**
 * A delivered `session_send` draws as the outgoing card from the receipt the
 * gateway stamps on its row (`meta.sessionSend`). Live, that receipt arrives as
 * a `chat_message_update` patch addressed to the row, so the card appears when
 * the call returns; the call's own result text is never what decides it.
 */
import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessagePatchByTs, sseToolResult } from '../store/chatSlice'
import { parseSessionSend } from '../pages/chat/sessionRelay'

const initial = reducer(undefined, { type: '@@INIT' })

function withTool() {
  return {
    ...initial,
    activeSlot: 'chat-2',
    messages: [{
      role: 'tool',
      content: '🔧 Session send',
      cls: '',
      ts: 't1',
      meta: { mid: 'm1', tool_call_id: 'tc-1', tool_name: 'session_send', mcp_server: 'kirocrew-dashboard', input: '{"target":"Planner","message":"go"}' },
    }],
  }
}

describe('a session_send row gets its card from the gateway receipt', () => {
  it('the receipt patch makes the row the outgoing card, linking the resolved key', () => {
    const state = reducer(withTool(), sseChatMessagePatchByTs({ slot: 'chat-2', ts: 't1', mid: 'm1', meta: { sessionSend: { outcome: 'queued', target: 'chat-9' } } }))
    expect(parseSessionSend(state.messages[0])).toEqual({ target: 'Planner', message: 'go', delivered: 'chat-9' })
  })

  it('the success text of the live tool result does not', () => {
    const state = reducer(withTool(), sseToolResult({ slot: 'chat-2', output: '📨 Delivered to `chat-9` — it started a turn on your message.', tool_call_id: 'tc-1' }))
    expect(parseSessionSend(state.messages[0])).toBeNull()
  })
})
