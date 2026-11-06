/**
 * The `terminal-enabled` query: a failed probe reads as enabled until an
 * explicit `enabled: false` has been seen, after which a failed refetch keeps
 * it; and the registry flag has exactly one writer, the probe's mirror.
 */
import { describe, it, expect } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ReactNode } from 'react'
import { execSync } from 'node:child_process'
import * as path from 'node:path'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { useTerminalEnabledQuery } from '../shell/boot/terminalRestore'

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
