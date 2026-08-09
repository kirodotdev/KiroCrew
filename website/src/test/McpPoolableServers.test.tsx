import { describe, it, expect } from 'vitest'
import {
  poolableRowLocked,
  poolableEligible,
  toggleAllChecked,
  toggleAllTargets,
  pooledViaAgentConfig,
} from '../pages/settings/McpPoolableServers'
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

describe('toggle all', () => {
  const on = srv({ name: 'on-mcp', poolable: true, in_allowlist: true })
  const off = srv({ name: 'off-mcp', poolable: false })
  const lockedHttp = srv({ name: 'http-mcp', transport: 'http', poolable: false })
  const lockedDeny = srv({ name: 'deny-mcp', denylisted: true, poolable: false })

  it('counts only the rows this UI can write', () => {
    expect(poolableEligible([on, off, lockedHttp, lockedDeny]).map(s => s.name)).toEqual([
      'on-mcp',
      'off-mcp',
    ])
  })

  it('reads checked when every eligible row is pooled', () => {
    expect(toggleAllChecked([on])).toBe(true)
  })

  it('ignores locked rows when deciding checked', () => {
    // Without this a single denylisted or HTTP server pins the toggle-all switch
    // off forever, and its click becomes a permanent no-op.
    expect(toggleAllChecked([on, lockedHttp, lockedDeny])).toBe(true)
  })

  it('reads unchecked when one eligible row is not pooled', () => {
    expect(toggleAllChecked([on, off])).toBe(false)
  })

  it('reads unchecked when nothing is eligible', () => {
    expect(toggleAllChecked([lockedHttp, lockedDeny])).toBe(false)
    expect(toggleAllChecked([])).toBe(false)
  })

  it('targets only the eligible rows that disagree with the next state', () => {
    expect(toggleAllTargets([on, off, lockedHttp, lockedDeny], true)).toEqual(['off-mcp'])
    expect(toggleAllTargets([on, off, lockedHttp, lockedDeny], false)).toEqual(['on-mcp'])
  })

  it('targets nothing when the eligible rows already agree', () => {
    expect(toggleAllTargets([on, lockedHttp], true)).toEqual([])
    expect(toggleAllTargets([], false)).toEqual([])
  })
})

describe('pooledViaAgentConfig', () => {
  it('counts a locked row that is pooled via the agent-JSON escape hatch', () => {
    // This row's switch reads ON but it is outside the count's denominator, so
    // the subline has to say so or it contradicts the pixels above it.
    const hatch = srv({ name: 'hatch-mcp', entry_poolable: true, in_allowlist: false, poolable: true })
    expect(pooledViaAgentConfig([hatch])).toBe(1)
  })

  it('ignores rows this UI owns, pooled or not', () => {
    const on = srv({ name: 'on-mcp', in_allowlist: true, poolable: true })
    const off = srv({ name: 'off-mcp', poolable: false })
    expect(pooledViaAgentConfig([on, off])).toBe(0)
  })

  it('ignores a locked row that is not pooled', () => {
    // Denylisted and HTTP rows are locked but never pooled — counting them
    // would inflate the reconciliation and invent servers in the pool.
    expect(pooledViaAgentConfig([srv({ transport: 'http' }), srv({ denylisted: true })])).toBe(0)
  })
})
