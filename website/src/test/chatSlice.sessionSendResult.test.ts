/**
 * A delivered `session_send` draws as the outgoing card from its own success
 * result (`meta.output`). The live `tool_result` frame lands that result on the
 * tool row, so the card appears when the call returns, not only after the next
 * transcript fetch. Only the gateway's own, bounded `session_send` row gets it.
 */
import { describe, it, expect } from 'vitest'
import reducer, { sseToolResult } from '../store/chatSlice'
import { MAX_TOOL_RESULT_CHARS } from '../utils/askQuestionTool'

const initial = reducer(undefined, { type: '@@INIT' })
const OK = '📨 Delivered to `chat-9` — it started a turn on your message.'

function withTool(meta: Record<string, unknown>) {
  return {
    ...initial,
    activeSlot: 'chat-2',
    messages: [{ role: 'tool', content: '🔧 Session send', cls: '', ts: 't1', meta: { tool_call_id: 'tc-1', input: '{"target":"chat-9","message":"go"}', ...meta } }],
  }
}

const GATEWAY = { tool_name: 'session_send', mcp_server: 'kirocrew-dashboard' }

describe('sseToolResult — a delivered session_send result reaches its tool row', () => {
  it('lands the success result on the gateway session_send row', () => {
    const state = reducer(withTool(GATEWAY), sseToolResult({ slot: 'chat-2', output: OK, tool_call_id: 'tc-1' }))
    expect(state.messages[0].meta?.output).toBe(OK)
  })

  it('leaves another server’s session_send row alone', () => {
    const state = reducer(withTool({ tool_name: 'session_send', mcp_server: 'evil-server' }), sseToolResult({ slot: 'chat-2', output: OK, tool_call_id: 'tc-1' }))
    expect(state.messages[0].meta?.output).toBeUndefined()
  })

  it('copies no refusal and no oversized result', () => {
    const refused = reducer(withTool(GATEWAY), sseToolResult({ slot: 'chat-2', output: 'Error: could not send to that session: session_closed', tool_call_id: 'tc-1' }))
    expect(refused.messages[0].meta?.output).toBeUndefined()
    const big = reducer(withTool(GATEWAY), sseToolResult({ slot: 'chat-2', output: OK + 'x'.repeat(MAX_TOOL_RESULT_CHARS), tool_call_id: 'tc-1' }))
    expect(big.messages[0].meta?.output).toBeUndefined()
  })
})
