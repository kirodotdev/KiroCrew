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

  it('tells the host which chat a file tab opened for, and only once it opened', async () => {
    // ChatPage switches the panel to a split pane's chat on this signal, so a
    // failed read (offline) must not send it.
    const { fn } = recordingFetch()
    globalThis.fetch = fn
    const onOpened = vi.fn()
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tabsCtl = { openFile: vi.fn(), openWorkingTreeDiff: vi.fn(), patchTab: vi.fn(), tabs: [] } as unknown as Ctl
    const { result } = renderHook(() => usePanelDocumentActions({
      tabsCtl, slotRef: { current: 'chat-a' }, queryClient, showActionError: vi.fn(), onOpened,
    }))
    await act(async () => { await result.current.openFile('/workspace/project/a.ts', { slot: 'pane-b' }) })
    expect(onOpened).toHaveBeenCalledWith('pane-b')
    onOpened.mockClear()
    // A slot taken from slotRef was not a pane's choice, so it names no chat.
    await act(async () => { await result.current.openFile('/workspace/project/c.ts') })
    expect(onOpened).toHaveBeenCalledTimes(1)
    expect(onOpened).toHaveBeenCalledWith(undefined)
    onOpened.mockClear()
    globalThis.fetch = vi.fn(() => Promise.reject(new Error('offline'))) as unknown as typeof fetch
    await act(async () => { await result.current.openFile('/workspace/project/b.ts', { slot: 'pane-b' }) })
    expect(onOpened).not.toHaveBeenCalled()
  })

  // The user moves to another chat while the read is in flight. ChatPage's
  // host switches the panel when `onOpened` names a chat it is not showing, so
  // a late read must not name one: that would pull the user back.
  function openDuringChatChange(override: { slot?: string | null }, movedTo: string) {
    let release!: () => void
    const gate = new Promise<void>(r => { release = r })
    const { fn } = recordingFetch()
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => { await gate; return fn(input) }) as unknown as typeof fetch
    const slotRef = { current: 'chat-a' as string | null }
    const switched: string[] = []
    // The same rule ChatPage's revealDockAfterOpen applies.
    const onOpened = vi.fn((s?: string | null) => { if (s != null && s !== slotRef.current) switched.push(s) })
    const tabsCtl = { openFile: vi.fn(), openWorkingTreeDiff: vi.fn(), patchTab: vi.fn(), tabs: [] } as unknown as Ctl
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => usePanelDocumentActions({
      tabsCtl, slotRef, queryClient, showActionError: vi.fn(), onOpened,
    }))
    return {
      tabsCtl, onOpened, switched,
      run: async () => {
        let pending!: Promise<void>
        act(() => { pending = result.current.openFile('/workspace/project/a.ts', override) })
        slotRef.current = movedTo
        release()
        await act(async () => { await pending })
      },
    }
  }

  it('a single-chat open does not switch back when the user changes chat during the read', async () => {
    const t = openDuringChatChange({}, 'chat-b')
    await t.run()
    // The tab still lands in the chat it was opened from...
    expect((t.tabsCtl.openFile as ReturnType<typeof vi.fn>).mock.calls[0][2]).toBe('chat-a')
    // ...but the host is told no chat to show, so the panel stays on chat-b.
    expect(t.onOpened).toHaveBeenCalledTimes(1)
    expect(t.onOpened).toHaveBeenCalledWith(undefined)
    expect(t.switched).toEqual([])
  })

  it('a pane open does not switch when the user goes to a third chat during the read', async () => {
    const t = openDuringChatChange({ slot: 'pane-b' }, 'chat-c')
    await t.run()
    expect((t.tabsCtl.openFile as ReturnType<typeof vi.fn>).mock.calls[0][2]).toBe('pane-b')
    expect(t.onOpened).toHaveBeenCalledTimes(1)
    expect(t.onOpened).toHaveBeenCalledWith(undefined)
    expect(t.switched).toEqual([])
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
