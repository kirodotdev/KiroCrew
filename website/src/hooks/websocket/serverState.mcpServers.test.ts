/**
 * The MCP server list (`['mcp-servers']`) is read once with `staleTime: Infinity`.
 * The gateway probes the servers in the background after a read and pushes an
 * `mcp_servers` refresh frame when that probe is done, so an open MCP Servers tab
 * shows each server's status without a reload. A frame sent while the socket was
 * down reaches the tab only through the reconnect re-read.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { QueryClient, QueryObserver } from '@tanstack/react-query'

vi.mock('../../api/client', () => ({
  api: { getCredentialRedaction: () => new Promise(() => {}) },
}))

import { invalidateRefreshQueries, refreshMayChangeRegistry, refreshServerStateAfterReconnect } from './serverState'

const KEYS = [['mcp-servers'], ['mcp-servers', 'ada'], ['cron-jobs'], ['kirocrewConfig'], ['kirocrew-agents']]

let qc: QueryClient
const unsubscribe: Array<() => void> = []
const settle = () => new Promise(resolve => setTimeout(resolve, 0))

afterEach(() => {
  for (const u of unsubscribe.splice(0)) u()
  qc.clear()
})

/** Observe every key with a counting read. */
async function observeAll(): Promise<Record<string, number>> {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const reads: Record<string, number> = {}
  for (const key of KEYS) {
    const id = key.join('/')
    reads[id] = 0
    const observer = new QueryObserver(qc, { queryKey: key, queryFn: async () => { reads[id] += 1; return [] } })
    unsubscribe.push(observer.subscribe(() => {}))
  }
  await settle()
  for (const id of Object.keys(reads)) reads[id] = 0
  return reads
}

const reread = (reads: Record<string, number>) => Object.keys(reads).filter(id => reads[id] > 0).sort()

describe('MCP server list cache', () => {
  it('re-reads the server list alone on the frame a finished probe sends', async () => {
    const reads = await observeAll()
    const cronsStale = vi.fn()
    const othersStale = vi.fn()
    invalidateRefreshQueries(qc, ['mcp_servers'], cronsStale, othersStale)
    await settle()
    // Not the chat header's per-agent list: a different read its menu re-runs on open.
    expect(reread(reads)).toEqual(['mcp-servers'])
    expect(cronsStale).not.toHaveBeenCalled()
    expect(othersStale).not.toHaveBeenCalled()
    expect(refreshMayChangeRegistry(['mcp_servers'])).toBe(false)
  })

  it('re-reads on a frame that names no kind, as every listed cache does', async () => {
    const reads = await observeAll()
    invalidateRefreshQueries(qc, [])
    await settle()
    expect(reads['mcp-servers']).toBe(1)
    expect(reads['mcp-servers/ada']).toBe(0)
  })

  it('re-reads after a reconnect, which may have missed that frame', async () => {
    const reads = await observeAll()
    refreshServerStateAfterReconnect(qc)
    await settle()
    expect(reads['mcp-servers']).toBe(1)
  })
})
