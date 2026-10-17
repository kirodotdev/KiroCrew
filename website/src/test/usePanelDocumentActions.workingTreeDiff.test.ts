/**
 * `openWorkingTreeDiff` (#9695) opens a file's working-tree diff as its own
 * read-only `wtdiff:<path>` tab. It is the diff twin of `openFile`: same seed
 * read (body + binary/partial verdict) plus a diff prefetch, then hands the tab
 * to the controller. These cover its branches — a clean read, a 404 placeholder,
 * a non-404 read error, the IDE file-bridge early return, and a thrown fetch —
 * so the hook stays above the per-file coverage floor without the editable-tab
 * paths the old design carried.
 */
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { usePanelDocumentActions } from '../hooks/usePanelDocumentActions'

type Ctl = Parameters<typeof usePanelDocumentActions>[0]['tabsCtl']

function fetchReturning(fileRead: Partial<Response> & { text?: () => Promise<string> }) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/file-read')) return Promise.resolve(fileRead as unknown as Response)
    // the diff prefetch (api.fileDiff goes through fetch too in this harness)
    return Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, json: () => Promise.resolve({ diff: '', original: '' }) } as unknown as Response)
  }) as unknown as typeof fetch
}

function render(tabsCtl: Partial<Ctl>, showActionError = vi.fn(), onOpened = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const { result } = renderHook(() => usePanelDocumentActions({
    tabsCtl: tabsCtl as Ctl, slotRef: { current: 'slot' }, queryClient, showActionError, onOpened,
  }))
  return { openWorkingTreeDiff: result.current.openWorkingTreeDiff, showActionError, onOpened }
}

describe('openWorkingTreeDiff (#9695)', () => {
  const realFetch = globalThis.fetch
  afterEach(() => {
    globalThis.fetch = realFetch
    delete (window as unknown as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles
    vi.restoreAllMocks()
  })

  it('opens a read-only wtdiff tab from a clean read, carrying the body and verdicts', async () => {
    globalThis.fetch = fetchReturning({ ok: true, status: 200, headers: { get: () => null } as unknown as Headers, text: () => Promise.resolve('working tree body') })
    const openWorkingTreeDiffSpy = vi.fn()
    const { openWorkingTreeDiff, onOpened } = render({ openWorkingTreeDiff: openWorkingTreeDiffSpy })
    await act(async () => { await openWorkingTreeDiff('/tmp/a.ts') })
    expect(openWorkingTreeDiffSpy).toHaveBeenCalledTimes(1)
    const [path, body, slot, opts] = openWorkingTreeDiffSpy.mock.calls[0]
    expect(path).toBe('/tmp/a.ts')
    expect(body).toBe('working tree body')
    expect(slot).toBe('slot')
    expect(opts).toMatchObject({ binary: false, partial: false })
    expect(onOpened).toHaveBeenCalled()
  })

  it('opens a not-found placeholder tab on a 404 (no error surfaced)', async () => {
    globalThis.fetch = fetchReturning({ ok: false, status: 404, headers: { get: () => null } as unknown as Headers, text: () => Promise.resolve('') })
    const openWorkingTreeDiffSpy = vi.fn()
    const { openWorkingTreeDiff, showActionError } = render({ openWorkingTreeDiff: openWorkingTreeDiffSpy })
    await act(async () => { await openWorkingTreeDiff('/tmp/gone.ts') })
    expect(openWorkingTreeDiffSpy).toHaveBeenCalledTimes(1)
    // A 404 still opens a tab (a placeholder body), and does NOT raise an error.
    expect(showActionError).not.toHaveBeenCalled()
  })

  it('surfaces a non-404 read error and opens no tab', async () => {
    globalThis.fetch = fetchReturning({ ok: false, status: 500, headers: { get: () => null } as unknown as Headers, text: () => Promise.resolve('') })
    const openWorkingTreeDiffSpy = vi.fn()
    const { openWorkingTreeDiff, showActionError } = render({ openWorkingTreeDiff: openWorkingTreeDiffSpy })
    await act(async () => { await openWorkingTreeDiff('/tmp/boom.ts') })
    expect(openWorkingTreeDiffSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(showActionError).toHaveBeenCalledTimes(1))
  })

  it('defers to the IDE file-bridge and opens no panel tab when a plugin handles files', async () => {
    ;(window as unknown as { __kirocrewPluginHandlesFiles?: boolean }).__kirocrewPluginHandlesFiles = true
    const openWorkingTreeDiffSpy = vi.fn()
    const { openWorkingTreeDiff } = render({ openWorkingTreeDiff: openWorkingTreeDiffSpy })
    await act(async () => { await openWorkingTreeDiff('/tmp/a.ts') })
    expect(openWorkingTreeDiffSpy).not.toHaveBeenCalled()
  })

  it('surfaces a thrown fetch as an action error', async () => {
    globalThis.fetch = vi.fn(() => Promise.reject(new Error('network down'))) as unknown as typeof fetch
    const openWorkingTreeDiffSpy = vi.fn()
    const { openWorkingTreeDiff, showActionError } = render({ openWorkingTreeDiff: openWorkingTreeDiffSpy })
    await act(async () => { await openWorkingTreeDiff('/tmp/a.ts') })
    expect(openWorkingTreeDiffSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(showActionError).toHaveBeenCalledTimes(1))
  })
})
