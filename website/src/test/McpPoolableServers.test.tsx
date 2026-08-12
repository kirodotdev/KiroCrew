import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, it, expect, vi } from 'vitest'
import {
  McpPoolableServers,
  poolableRowLocked,
  poolableEligible,
  toggleAllChecked,
  toggleAllNextValue,
  toggleAllState,
  toggleAllTargets,
  pooledViaAgentConfig,
} from '../pages/settings/McpPoolableServers'
import { api, type McpPoolableServer } from '../api/client'

afterEach(() => {
  vi.restoreAllMocks()
})

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

  it('represents a partially pooled list as mixed', () => {
    expect(toggleAllState([on, off, lockedHttp, lockedDeny])).toBe('mixed')
  })

  it('clears a mixed list instead of briefly pooling every server', () => {
    expect(toggleAllNextValue('mixed')).toBe(false)
  })

  it('moves uniform lists to their opposite state', () => {
    expect(toggleAllNextValue('off')).toBe(true)
    expect(toggleAllNextValue('on')).toBe(false)
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

  it.each([
    { state: 'off', servers: [off], name: 'Pool all', checked: false },
    { state: 'on', servers: [on], name: 'Clear all', checked: true },
  ])('renders the $state checkbox with an explicit $name action', async ({ servers, name, checked }) => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue({
      enabled: true,
      apps_enabled: false,
      running: true,
      ping_ok: true,
      supported: true,
    })
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers })
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <McpPoolableServers />
      </QueryClientProvider>,
    )

    const master = await screen.findByRole('checkbox', { name })
    expect(master).toHaveProperty('checked', checked)
    expect((master as HTMLInputElement).indeterminate).toBe(false)
    expect(screen.queryByRole('button', { name: 'Pool all' })).not.toBeInTheDocument()
  })

  it('renders mixed state with explicit actions in both directions', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue({
      enabled: true,
      apps_enabled: false,
      running: true,
      ping_ok: true,
      supported: true,
    })
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [on, off] })
    const setPoolableMany = vi.spyOn(api, 'mcpGatewaySetPoolableMany').mockResolvedValue({
      ok: true,
      names: ['on-mcp'],
      poolable: false,
    })
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <McpPoolableServers />
      </QueryClientProvider>,
    )

    const master = await screen.findByRole('checkbox', { name: 'Clear all' })
    expect(master).toHaveAttribute('aria-checked', 'mixed')
    expect((master as HTMLInputElement).indeterminate).toBe(true)
    expect(screen.getByRole('button', { name: 'Pool all' })).toBeEnabled()

    fireEvent.click(master)

    await waitFor(() => {
      expect(setPoolableMany).toHaveBeenCalledWith(['on-mcp'], false)
    })

    fireEvent.click(screen.getByRole('button', { name: 'Pool all' }))

    await waitFor(() => {
      expect(setPoolableMany).toHaveBeenCalledWith(['off-mcp'], true)
    })
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
