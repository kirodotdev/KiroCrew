import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage, sseChatMessagePatchByTs } from './chatSlice'
import '../test/mockApiClient'

/**
 * At a tool call the gateway commits the reply's text so far as a row, but
 * holds that text's last run off it until the text after the tool shows the run
 * is not half of a credential. It then gives the run back as a content patch
 * keyed by the row's `mid`. An open tab built that row from its streaming
 * bubble, so the patch must land there, and not on the bubble that streams
 * after the tool.
 */

const SLOT = 'chat-held-run'
const init = () => ({ ...reducer(undefined, { type: '@@INIT' }), activeSlot: SLOT })

describe('a run given back after a tool call', () => {
  it('lands on the row the streaming bubble became', () => {
    let s = init()
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'Let me check the ', seq: 1 }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'assistant', content: 'Let me check the ', cls: 'msg msg-a', ts: 't1', meta: { mid: 'm1' } }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'tool', content: 'Reading a file', cls: 'msg msg-tool', ts: 't2', meta: { mid: 'm-tool', tool_call_id: 'tc-1' } }))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: ' file now.', seq: 2 }))
    s = reducer(s, sseChatMessagePatchByTs({ slot: SLOT, ts: 't1', mid: 'm1', content: 'Let me check the config' }))

    expect(s.messages.map(m => [m.role, m.content])).toEqual([
      ['assistant', 'Let me check the config'],
      ['tool', 'Reading a file'],
      ['streaming', ' file now.'],
    ])
  })
})
