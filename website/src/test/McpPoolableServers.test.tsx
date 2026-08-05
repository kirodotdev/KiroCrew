import { describe, it, expect } from 'vitest'
import { poolableRowLocked } from '../pages/settings/McpPoolableServers'
import type { McpPoolableServer } from '../api/client'

function srv(partial: Partial<McpPoolableServer>): McpPoolableServer {
  return {
    name: 'x-mcp',
    poolable: false,
    in_allowlist: false,
    entry_poolable: false,
    agents: [],
    transport: 'stdio',
    denylisted: false,
    ...partial,
  }
}

describe('poolableRowLocked', () => {
  it('allows toggling a plain stdio server', () => {
    expect(poolableRowLocked(srv({ transport: 'stdio' }))).toBe(false)
  })

  it('allows toggling an allowlisted server', () => {
    expect(poolableRowLocked(srv({ in_allowlist: true, poolable: true }))).toBe(false)
  })

  it('locks denylisted servers (can never be pooled)', () => {
    expect(poolableRowLocked(srv({ denylisted: true }))).toBe(true)
  })

  it('locks HTTP/SSE servers (shared by nature, not process-pooled)', () => {
    expect(poolableRowLocked(srv({ transport: 'http' }))).toBe(true)
  })

  it('locks a server poolable only via the agent-JSON escape hatch', () => {
    // poolable:true in the agent file but NOT in the allowlist → not managed here.
    expect(poolableRowLocked(srv({ entry_poolable: true, in_allowlist: false }))).toBe(true)
  })

  it('does not lock a server that is both entry-poolable and allowlisted', () => {
    expect(poolableRowLocked(srv({ entry_poolable: true, in_allowlist: true, poolable: true }))).toBe(false)
  })
})
