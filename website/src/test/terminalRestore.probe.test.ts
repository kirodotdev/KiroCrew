/**
 * The `terminal-enabled` query: a failed probe reads as enabled until an
 * explicit `enabled: false` has been seen, after which a failed refetch keeps
 * it; and the registry flag has exactly one writer, the probe's mirror.
 */
import { describe, it, expect } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { execSync } from 'node:child_process'
import * as path from 'node:path'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { useTerminalEnabledCache, useTerminalEnabledQuery } from '../shell/boot/terminalRestore'

const wrap = (qc: QueryClient) => ({ children }: { children: ReactNode }) =>
  createElement(QueryClientProvider, { client: qc }, children)

describe('useTerminalEnabledQuery', () => {
  it('keeps a cached explicit false when a refetch fails', async () => {
    server.use(http.get('/api/terminal/sessions', () => new HttpResponse(null, { status: 500 })))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    queryClient.setQueryData(['terminal-enabled'], { enabled: false, sessions: [] })
    const { result } = renderHook(() => useTerminalEnabledQuery(), { wrapper: wrap(queryClient) })
    await waitFor(() => expect(result.current.isFetching).toBe(false))
    expect(queryClient.getQueryData(['terminal-enabled'])).toEqual({ enabled: false, sessions: [] })
    expect(result.current.data).toEqual({ enabled: false, sessions: [] })
  })

  it('reads a failed first probe as enabled (default-on)', async () => {
    server.use(http.get('/api/terminal/sessions', () => new HttpResponse(null, { status: 500 })))
    const { result } = renderHook(() => useTerminalEnabledQuery(), { wrapper: wrap(new QueryClient({ defaultOptions: { queries: { retry: false } } })) })
    await waitFor(() => expect(result.current.data).toEqual({ enabled: true }))
  })

  it('T16: a policy lock rides the answer and survives a failed refetch', async () => {
    let calls = 0
    server.use(http.get('/api/terminal/sessions', () => {
      calls += 1
      return calls === 1
        ? HttpResponse.json({ enabled: false, locked_by_policy: true, sessions: [] })
        : new HttpResponse(null, { status: 500 })
    }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useTerminalEnabledQuery(), { wrapper: wrap(qc) })
    await waitFor(() => expect(result.current.data?.locked_by_policy).toBe(true))
    await qc.invalidateQueries({ queryKey: ['terminal-enabled'] })
    expect(calls).toBe(2)
    expect(qc.getQueryData(['terminal-enabled'])).toEqual({ enabled: false, locked_by_policy: true, sessions: [] })
  })

  it('T16: the cache observer never fetches and follows a later write', async () => {
    let calls = 0
    server.use(http.get('/api/terminal/sessions', () => { calls += 1; return HttpResponse.json({ enabled: true }) }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => useTerminalEnabledCache(), { wrapper: wrap(qc) })
    expect(result.current.data).toBeUndefined()
    act(() => { qc.setQueryData(['terminal-enabled'], { enabled: false, locked_by_policy: true, sessions: [] }) })
    await waitFor(() => expect(result.current.data?.locked_by_policy).toBe(true))
    expect(calls).toBe(0)
  })

  it('the registry flag has one writer outside tests: the probe mirror', () => {
    const src = path.resolve(__dirname, '..')
    const out = execSync(
      `grep -rl 'setTerminalEnabledFlag(' "${src}" --include='*.ts' --include='*.tsx' | grep -v '/test/' | grep -v '\\.test\\.'`,
      { encoding: 'utf-8' },
    ).trim().split('\n').map(p => path.relative(src, p)).sort()
    // The registry defines it; the probe is its only caller.
    expect(out).toEqual(['shell/boot/terminalRestore.ts', 'utils/terminalRegistry.ts'])
  })
})
