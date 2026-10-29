/**
 * A side-panel read, diff or save names the chat it is for (`slot=`), so the
 * gateway can refuse one that runs on a remote environment instead of showing
 * it this machine's copy of the file. A request with no chat names none.
 */
import { renderHook, act } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { usePanelDocumentActions } from '../hooks/usePanelDocumentActions'
import { fileReadUrl } from '../utils/fileReadUrl'

type Ctl = Parameters<typeof usePanelDocumentActions>[0]['tabsCtl']

function recordingFetch() {
  const urls: string[] = []
  const fn = vi.fn((input: RequestInfo | URL) => {
    urls.push(String(input))
    return Promise.resolve({
      ok: true, status: 200, headers: { get: () => null },
      text: () => Promise.resolve('body'), json: () => Promise.resolve({ diff: '', original: '' }),
    } as unknown as Response)
  }) as unknown as typeof fetch
  return { fn, urls }
}

function render(slot: string | null) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tabsCtl = { openFile: vi.fn(), openWorkingTreeDiff: vi.fn(), patchTab: vi.fn(), tabs: [] } as unknown as Ctl
  const { result } = renderHook(() => usePanelDocumentActions({
    tabsCtl, slotRef: { current: slot }, queryClient, showActionError: vi.fn(), onOpened: vi.fn(),
  }))
  return result.current
}

describe('side-panel file requests name their chat', () => {
  const realFetch = globalThis.fetch
  afterEach(() => { globalThis.fetch = realFetch; vi.restoreAllMocks() })

  it('sends the chat on the read and the diff of a file it opens', async () => {
    const { fn, urls } = recordingFetch()
    globalThis.fetch = fn
    const actions = render('chat-a b')
    await act(async () => { await actions.openFile('/workspace/project/a.ts') })
    expect(urls.filter(u => u.startsWith('/api/file-read'))).toEqual(['/api/file-read?path=%2Fworkspace%2Fproject%2Fa.ts&slot=chat-a%20b'])
    expect(urls.filter(u => u.startsWith('/api/file-diff'))).toEqual(['/api/file-diff?path=%2Fworkspace%2Fproject%2Fa.ts&slot=chat-a%20b'])
  })

  it('sends the chat on a save', async () => {
    const { fn, urls } = recordingFetch()
    globalThis.fetch = fn
    const actions = render('chat-a')
    await act(async () => { await actions.saveFile('/workspace/project/a.ts', 'x') })
    expect(urls[0]).toBe('/api/file-write?slot=chat-a')
  })

  it('names no chat when there is none', async () => {
    const { fn, urls } = recordingFetch()
    globalThis.fetch = fn
    const actions = render(null)
    await act(async () => { await actions.openFile('/workspace/project/a.ts') })
    await act(async () => { await actions.saveFile('/workspace/project/a.ts', 'x') })
    expect(urls.some(u => u.includes('slot='))).toBe(false)
    expect(fileReadUrl('/workspace/project/a.ts')).toBe('/api/file-read?path=%2Fworkspace%2Fproject%2Fa.ts')
  })
})
