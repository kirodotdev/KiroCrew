/**
 * A split pane's upload that fails once its chat is off screen (the
 * pane rebound to another chat, or gone) is held for that chat outside the
 * transcript, and the pane's upload banner shows it when the chat is shown
 * again. A transcript row would be dropped by the next history rebuild and
 * read as the chat's last turn error.
 */
import { createElement, StrictMode, type ReactNode } from 'react'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useChatPaneUploads } from '../components/useChatPaneUploads'
import { __resetComposerSendHoldsForTests } from '../utils/composerSendHolds'
import { clearSlotUploadFailures, holdSlotUploadFailure, takeSlotUploadFailure } from '../utils/slotUploadFailures'

afterEach(() => {
  vi.restoreAllMocks()
  clearSlotUploadFailures()
  __resetComposerSendHoldsForTests()
})

const failed = i18nT('pages.chatPage.upload_failed_error', { error: 'disk full' })

/** `api.uploadFiles` answering only when the test says so. */
function heldUpload() {
  let answer!: (res: { paths: string[]; error?: string }) => void
  const spy = vi.spyOn(api, 'uploadFiles').mockImplementation(() => new Promise(resolve => { answer = resolve as never }))
  return { spy, refuse: () => answer({ paths: [], error: 'disk full' }) }
}

function mount(slot: string, { strict = false }: { strict?: boolean } = {}) {
  const slotKeyRef = { current: slot }
  const mountedRef = { current: true }
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => {
    const tree = createElement(QueryClientProvider, { client: queryClient }, children)
    return strict ? createElement(StrictMode, null, tree) : tree
  }
  const hook = renderHook(({ slotKey }: { slotKey: string }) => useChatPaneUploads({
    slotKey, slotKeyRef, mountedRef, setInput: vi.fn(), dashCfg: { upload_max_mb: 50 }, dashCfgError: undefined,
  }), { wrapper, initialProps: { slotKey: slot } })
  /** Point the pane at `next`, the way ChatPane moves its ref and prop together. */
  const showSlot = (next: string) => { slotKeyRef.current = next; hook.rerender({ slotKey: next }) }
  return { ...hook, mountedRef, showSlot }
}

const file = () => new File(['x'], 'a.txt', { type: 'text/plain' })

describe('useChatPaneUploads: a failure for a chat off screen (#34828)', () => {
  it('shows a failure that lands after the pane moved to another chat when it comes back, once', async () => {
    const { spy, refuse } = heldUpload()
    const pane = mount('A')
    act(() => { pane.result.current.uploadFiles([file()]) })
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    pane.showSlot('B')
    await act(async () => { refuse() })
    await waitFor(() => expect(pane.result.current.uploadMutation.isPending).toBe(false))
    expect(pane.result.current.uploadError).toBe('')
    pane.showSlot('A')
    expect(pane.result.current.uploadError).toBe(failed)
    act(() => { pane.result.current.setUploadError('') })
    pane.showSlot('B')
    pane.showSlot('A')
    expect(pane.result.current.uploadError).toBe('')
  })

  it('shows a failure that lands after the pane went away in the next pane on that chat', async () => {
    const { spy, refuse } = heldUpload()
    const first = mount('A')
    act(() => { first.result.current.uploadFiles([file()]) })
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1))
    first.mountedRef.current = false
    first.unmount()
    await act(async () => { refuse() })
    const next = mount('A')
    await waitFor(() => expect(next.result.current.uploadError).toBe(failed))
  })

  it('shows at once a failure held for the chat the pane already shows', () => {
    const pane = mount('A')
    act(() => { holdSlotUploadFailure('B', 'other chat') })
    expect(pane.result.current.uploadError).toBe('')
    act(() => { holdSlotUploadFailure('A', failed) })
    expect(pane.result.current.uploadError).toBe(failed)
    expect(takeSlotUploadFailure('A')).toBe('')
    expect(takeSlotUploadFailure('B')).toBe('other chat')
  })

  it('keeps a held failure in the banner through the StrictMode mount replay', () => {
    holdSlotUploadFailure('A', failed)
    const pane = mount('A', { strict: true })
    expect(pane.result.current.uploadError).toBe(failed)
  })
})
