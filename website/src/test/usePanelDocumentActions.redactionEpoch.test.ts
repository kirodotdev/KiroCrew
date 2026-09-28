/**
 * A file read that STARTS while the owner's credential-redaction switch is off
 * and FINISHES after it is back on carries raw bytes under a pass that no
 * longer allows them. The chip-click read goes through react-query's
 * `fetchQuery`, and the purge the switch triggers REMOVES the query, which
 * cancels the in-flight fetch: the straddling read rejects and its raw body is
 * never written into a tab. (`MarkdownPanel`'s direct read is the one path that
 * bypasses react-query; it carries the `documentBodyEpochNow` guard instead.)
 */
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { usePanelDocumentActions } from '../hooks/usePanelDocumentActions'
import { purgeDocumentBodiesForRedactionChange } from '../hooks/usePanelTabs'

describe('openFile across a document-body purge', () => {
  const realFetch = globalThis.fetch
  afterEach(() => { globalThis.fetch = realFetch; vi.restoreAllMocks() })

  it('never writes the raw body of a read that straddled the purge', async () => {
    // First read: hangs until released, answers RAW. Second read: answers REDACTED.
    let release: () => void = () => {}
    const bodies = ['AKIA-raw-while-off', '[REDACTED]']
    let reads = 0
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/file-read')) {
        const body = bodies[Math.min(reads, bodies.length - 1)]
        const n = reads++
        const resp = {
          ok: true, status: 200,
          headers: { get: () => null },
          text: () => Promise.resolve(body),
        } as unknown as Response
        return n === 0 ? new Promise<Response>(r => { release = () => r(resp) }) : Promise.resolve(resp)
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) } as unknown as Response)
    }) as unknown as typeof fetch

    const openFileSpy = vi.fn()
    const tabsCtl = { openFile: openFileSpy } as unknown as Parameters<typeof usePanelDocumentActions>[0]['tabsCtl']
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => usePanelDocumentActions({
      tabsCtl, slotRef: { current: 'slot' }, queryClient, showActionError: vi.fn(),
    }))

    let opening: Promise<void> = Promise.resolve()
    act(() => { opening = result.current.openFile('/tmp/secret.txt') })
    // The switch flips while the first read is in flight: the purge removes the
    // query, cancelling the fetch.
    act(() => { purgeDocumentBodiesForRedactionChange(queryClient) })
    act(() => { release() })
    await act(async () => { await opening })
    await waitFor(() => expect(reads).toBe(1))
    // Nothing raw reached a tab; the next open reads fresh under the pass in force.
    expect(openFileSpy.mock.calls.map(c => c[1])).not.toContain('AKIA-raw-while-off')
    await act(async () => { await result.current.openFile('/tmp/secret.txt') })
    expect(openFileSpy.mock.calls.map(c => c[1])).toEqual(['[REDACTED]'])
  })
})

describe('openFile carries the read\'s partial verdict into the tab', () => {
  const realFetch = globalThis.fetch
  afterEach(() => { globalThis.fetch = realFetch; vi.restoreAllMocks() })

  it('marks the tab partial from the X-Redacted / X-Truncated / X-Lossy-Decode headers of the opening read, and whole without them', async () => {
    // This is the seed read that fills a tab before any panel mounts, so a
    // verdict left behind here would be lost for good: a panel that later finds
    // the file gone would offer a redacted prefix -- or a Latin-1 file decoded
    // with replacement characters -- for download under the file's own name,
    // as if it were the file as written.
    const headers = new Map<string, Record<string, string>>([
      ['/tmp/redacted.txt', { 'X-Redacted': 'true' }],
      ['/tmp/cut.txt', { 'X-Truncated': 'true' }],
      ['/tmp/latin1.txt', { 'X-Lossy-Decode': 'true' }],
      ['/tmp/whole.txt', {}],
    ])
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/file-read')) {
        const path = decodeURIComponent(new URL(url, 'http://gateway').searchParams.get('path') ?? '')
        const h = headers.get(path) ?? {}
        return Promise.resolve({
          ok: true, status: 200,
          headers: { get: (k: string) => h[k] ?? null },
          text: () => Promise.resolve(`body of ${path}`),
        } as unknown as Response)
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) } as unknown as Response)
    }) as unknown as typeof fetch

    const openFileSpy = vi.fn()
    const tabsCtl = { openFile: openFileSpy } as unknown as Parameters<typeof usePanelDocumentActions>[0]['tabsCtl']
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => usePanelDocumentActions({
      tabsCtl, slotRef: { current: 'slot' }, queryClient, showActionError: vi.fn(),
    }))
    for (const path of headers.keys()) await act(async () => { await result.current.openFile(path) })
    const verdicts = Object.fromEntries(openFileSpy.mock.calls.map(c => [c[0], c[3].partial]))
    expect(verdicts).toEqual({ '/tmp/redacted.txt': true, '/tmp/cut.txt': true, '/tmp/latin1.txt': true, '/tmp/whole.txt': false })
  })
})
