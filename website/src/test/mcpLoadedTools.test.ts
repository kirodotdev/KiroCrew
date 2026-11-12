import { describe, it, expect } from 'vitest'
import { deriveLoadedMcpTools, mcpToolStatus } from '../lib/mcpLoadedTools'
import type { ChatMessage } from '../types'

const toolMsg = (output: string): ChatMessage => ({ role: 'tool', content: '', cls: '', meta: { output } })

describe('deriveLoadedMcpTools', () => {
  it('collects server::tool ids from a tool_search result', () => {
    const out = JSON.stringify({
      tools: [
        { tool_name: 'post_message', server_name: 'slack-mcp', score: 14 },
        { tool_name: 'read_file', server_name: 'fs-mcp', score: 9 },
      ],
    })
    const set = deriveLoadedMcpTools([toolMsg(out)])
    expect(set.has('slack-mcp::post_message')).toBe(true)
    expect(set.has('fs-mcp::read_file')).toBe(true)
    expect(set.size).toBe(2)
  })

  it('ignores non-tool messages and non-tool_search output', () => {
    const set = deriveLoadedMcpTools([
      // Right shape but wrong role — must not count.
      { role: 'assistant', content: '', cls: '', meta: { output: '{"tools":[{"tool_name":"x","server_name":"y"}]}' } },
      toolMsg('plain text output'),
      toolMsg('{"not":"a_tool_search"}'),
      toolMsg('{bad json but has server_name'),
    ])
    expect(set.size).toBe(0)
  })

  it('unions activations across multiple tool_search calls', () => {
    const a = JSON.stringify({ tools: [{ tool_name: 't1', server_name: 's' }] })
    const b = JSON.stringify({ tools: [{ tool_name: 't2', server_name: 's' }] })
    const set = deriveLoadedMcpTools([toolMsg(a), toolMsg(b)])
    expect([...set].sort()).toEqual(['s::t1', 's::t2'])
  })

  it('counts an MCP tool a tool message shows was called, with no tool_search output', () => {
    const set = deriveLoadedMcpTools([
      // The default backend: the server is stamped, the tool is named only by the title.
      { role: 'tool', content: '🔧 @kirocrew-core/learn_list', cls: '', meta: { mcp_server: 'kirocrew-core' } },
      // Both halves stamped.
      { role: 'tool', content: '✅ Ping', cls: '', meta: { mcp_server: 'p223rs', tool_name: 'p223rs_ping' } },
      // A built-in names a tool but no server: not an MCP tool.
      { role: 'tool', content: '🔧 Run Command', cls: '', meta: { tool_name: 'execute_bash' } },
      // A shell call's title is the model's own text; without the stamped server it names nothing.
      { role: 'tool', content: '🔧 @kirocrew-core/session_send', cls: '', meta: { kind: 'execute' } },
    ])
    expect([...set].sort()).toEqual(['kirocrew-core::learn_list', 'p223rs::p223rs_ping'])
  })
})

describe('mcpToolStatus', () => {
  const loaded = new Set(['s::loaded'])
  it('disabled takes precedence over everything', () => {
    expect(mcpToolStatus('s', 'x', { loaded, disabledTools: ['x'], toolSearchOn: true })).toBe('disabled')
    expect(mcpToolStatus('s', 'x', { loaded, disabledTools: ['x'], toolSearchOn: false })).toBe('disabled')
  })
  it('active for every non-disabled tool when tool search is off', () => {
    expect(mcpToolStatus('s', 'anything', { loaded, toolSearchOn: false })).toBe('active')
  })
  it('active when the tool has been loaded this session', () => {
    expect(mcpToolStatus('s', 'loaded', { loaded, toolSearchOn: true })).toBe('active')
  })
  it('deferred when tool search is on and the tool is not loaded', () => {
    expect(mcpToolStatus('s', 'other', { loaded, toolSearchOn: true })).toBe('deferred')
  })
})
