// @vitest-environment jsdom
/**
 * Where a link on a Bot's Dashboard tab opens: an artifact or a workspace file in
 * the Bot's side panel, anything else outside the app, and a path that leaves the
 * workspace nowhere.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import { classifyDashboardLink, useDashboardPanelLinkOpener } from '../pages/members/dashboardPanelLinks'

const ORIGIN = 'https://dash.example'
const WS = '/home/me/.kiro/crew/workspace-member-helper'
const at = (path: string) => ORIGIN + path

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('classifyDashboardLink', () => {
  it('reads a same-origin artifact route as that artifact', () => {
    expect(classifyDashboardLink(at('/artifacts/task-receipt'), ORIGIN, WS)).toEqual({ kind: 'artifact', slug: 'task-receipt' })
    expect(classifyDashboardLink(at('/artifacts/a%20b/'), ORIGIN, WS)).toEqual({ kind: 'artifact', slug: 'a b' })
  })

  it('leaves other origins, other dashboard routes and deeper artifact routes opening outside', () => {
    expect(classifyDashboardLink('https://example.com/artifacts/x', ORIGIN, WS)).toEqual({ kind: 'external' })
    expect(classifyDashboardLink(at('/chat?sid=chat-1'), ORIGIN, WS)).toEqual({ kind: 'external' })
    expect(classifyDashboardLink(at('/artifacts/x/versions'), ORIGIN, WS)).toEqual({ kind: 'external' })
    expect(classifyDashboardLink(at('/artifacts'), ORIGIN, WS)).toEqual({ kind: 'external' })
  })

  it('refuses an artifact slug carrying a control character or a backslash', () => {
    expect(classifyDashboardLink(at('/artifacts/a%0Ab'), ORIGIN, WS).kind).toBe('refused')
    expect(classifyDashboardLink(at('/artifacts/a%5Cb'), ORIGIN, WS).kind).toBe('refused')
  })

  it('reads an absolute path inside the workspace as a file', () => {
    expect(classifyDashboardLink(at(`${WS}/drafts/receipt.md`), ORIGIN, WS)).toEqual({ kind: 'file', path: `${WS}/drafts/receipt.md` })
    expect(classifyDashboardLink(at(`${WS}/drafts/my%20notes.md`), ORIGIN, `${WS}/`)).toEqual({ kind: 'file', path: `${WS}/drafts/my notes.md` })
  })

  it('refuses a path that leaves the workspace, by .., by an encoded slash or by being elsewhere', () => {
    // The URL parser folds a literal `..` before the router sees it.
    expect(classifyDashboardLink(at(`${WS}/../workspace-member-other/x.md`), ORIGIN, WS).kind).toBe('refused')
    expect(classifyDashboardLink(at(`${WS}/..%2F..%2F.ssh%2Fid_rsa`), ORIGIN, WS).kind).toBe('refused')
    expect(classifyDashboardLink(at(`${WS}/%2E%2E/x`), ORIGIN, WS).kind).toBe('refused')
    expect(classifyDashboardLink(at('/home/me/.aws/credentials'), ORIGIN, WS).kind).toBe('refused')
    expect(classifyDashboardLink(at(`${WS}-old/x.md`), ORIGIN, WS).kind).toBe('refused')
    expect(classifyDashboardLink(at(WS), ORIGIN, WS).kind).toBe('refused')
    expect(classifyDashboardLink(at(`${WS}//x.md`), ORIGIN, WS).kind).toBe('refused')
    expect(classifyDashboardLink(at(`${WS}/a%5C..%5Cx`), ORIGIN, WS).kind).toBe('refused')
    expect(classifyDashboardLink(at(`${WS}/x.md?line=3`), ORIGIN, WS).kind).toBe('refused')
  })

  it('routes no file link without a usable workspace', () => {
    expect(classifyDashboardLink(at(`${WS}/x.md`), ORIGIN, undefined)).toEqual({ kind: 'external' })
    expect(classifyDashboardLink(at('/x.md'), ORIGIN, '/')).toEqual({ kind: 'external' })
    expect(classifyDashboardLink(at('/C:/ws/x.md'), ORIGIN, 'C:\\ws')).toEqual({ kind: 'external' })
  })
})

describe('useDashboardPanelLinkOpener', () => {
  afterEach(() => { vi.restoreAllMocks() })

  function setup(initialProps = { slot: 'member-helper', botId: 'helper' }) {
    const openArtifact = vi.fn()
    const openFile = vi.fn()
    const showRefusal = vi.fn()
    const opened = vi.spyOn(window, 'open').mockReturnValue(null)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    const { result, rerender } = renderHook(({ slot, botId }: { slot: string; botId: string }) => useDashboardPanelLinkOpener({
      workspaceDir: WS, slot, botId, botName: 'Helper', openArtifact, openFile, showRefusal,
    }), { wrapper, initialProps })
    return { open: result.current, rerender, openArtifact, openFile, showRefusal, opened }
  }
  const here = (path: string) => window.location.origin + path

  it('opens a known artifact in the panel and no window', async () => {
    vi.spyOn(api, 'artifact').mockResolvedValue({ slug: 'task-receipt', kind: 'markdown', content: '' } as never)
    const s = setup()
    s.open(here('/artifacts/task-receipt'))
    await vi.waitFor(() => expect(s.openArtifact).toHaveBeenCalledWith('task-receipt'))
    expect(s.opened).not.toHaveBeenCalled()
  })

  it('drops a pending artifact open when the active slot changes', async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.artifact>>>()
    vi.spyOn(api, 'artifact').mockReturnValue(pending.promise)
    const s = setup()
    s.open(here('/artifacts/task-receipt'))
    await vi.waitFor(() => expect(api.artifact).toHaveBeenCalledWith('task-receipt'))

    s.rerender({ slot: 'member-other', botId: 'other' })
    await act(async () => {
      pending.resolve({ slug: 'task-receipt', kind: 'markdown', content: '' } as never)
      await pending.promise
    })

    expect(s.openArtifact).not.toHaveBeenCalled()
    expect(s.showRefusal).not.toHaveBeenCalled()
  })

  it('drops a pending artifact open when only the active Bot changes', async () => {
    const pending = deferred<Awaited<ReturnType<typeof api.artifact>>>()
    vi.spyOn(api, 'artifact').mockReturnValue(pending.promise)
    const s = setup({ slot: '', botId: 'helper' })
    s.open(here('/artifacts/task-receipt'))
    await vi.waitFor(() => expect(api.artifact).toHaveBeenCalledWith('task-receipt'))

    s.rerender({ slot: '', botId: 'other' })
    await act(async () => {
      pending.resolve({ slug: 'task-receipt', kind: 'markdown', content: '' } as never)
      await pending.promise
    })

    expect(s.openArtifact).not.toHaveBeenCalled()
    expect(s.showRefusal).not.toHaveBeenCalled()
  })

  it('opens nothing for an artifact the library does not hold, and says so', async () => {
    vi.spyOn(api, 'artifact').mockRejectedValue(new Error('404'))
    const s = setup()
    s.open(here('/artifacts/nope'))
    await vi.waitFor(() => expect(s.showRefusal).toHaveBeenCalledTimes(1))
    expect(s.openArtifact).not.toHaveBeenCalled()
    expect(s.opened).not.toHaveBeenCalled()
  })

  it('opens a file inside the workspace in the panel, as a chat file link does', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    const s = setup()
    expect(s.open(here(`${WS}/drafts/receipt.md`))).toBe(true)
    expect(s.openFile).toHaveBeenCalledTimes(1)
    expect(s.openFile.mock.calls[0]).toEqual([`${WS}/drafts/receipt.md`])
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(s.showRefusal).not.toHaveBeenCalled()
    expect(s.opened).not.toHaveBeenCalled()
  })

  it('opens nothing for a path outside the workspace or one with a .. segment, and says so', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    const s = setup()
    s.open(here('/home/me/.ssh/id_rsa'))
    s.open(here(`${WS}/..%2F..%2F.ssh%2Fid_rsa`))
    expect(s.showRefusal).toHaveBeenCalledTimes(2)
    expect(fetchSpy).not.toHaveBeenCalled()
    expect(s.openFile).not.toHaveBeenCalled()
    expect(s.opened).not.toHaveBeenCalled()
  })

  it('claims the links it handles, and leaves every other link to the bridge', async () => {
    vi.spyOn(api, 'artifact').mockResolvedValue({ slug: 'task-receipt', kind: 'markdown', content: '' } as never)
    const s = setup()
    expect(s.open('https://example.com/docs')).toBe(false)
    expect(s.open(here('/schedule'))).toBe(false)
    expect(s.open(here('/artifacts/task-receipt'))).toBe(true)
    expect(s.open(here('/home/me/.ssh/id_rsa'))).toBe(true)
    expect(s.opened).not.toHaveBeenCalled()
    expect(s.openArtifact).not.toHaveBeenCalled()
    expect(s.openFile).not.toHaveBeenCalled()
  })
})
