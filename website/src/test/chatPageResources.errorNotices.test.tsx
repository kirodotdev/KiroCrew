/**
 * #9186: two resource-region failures that used to be silent now reach the
 * user through `showActionError`, each exactly once.
 *  - a failed source-host config read notifies once per failure, not per render;
 *  - a non-403 artifact-reference failure notifies; the incognito 403 does not.
 */
import { createElement, type ReactNode } from 'react'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { usePanelDocumentActions } from '../hooks/usePanelDocumentActions'
import { ThemeProvider } from '../hooks/useTheme'
import { useChatPageResourcesController } from '../pages/chat/useChatPageResourcesController'

afterEach(() => { vi.restoreAllMocks() })

const ref = <T,>(current: T) => ({ current })

describe('source-host config read failure', () => {
  it('notifies once and does not re-notify on re-render', async () => {
    vi.spyOn(api, 'dashboardConfig').mockRejectedValue(new Error('boom'))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, createElement(ThemeProvider, null, children))
    const showActionError = vi.fn()
    const noop = vi.fn()
    const props = {
      activeSlot: 'slot', activeSlotRef: ref<string | null>('slot'), messages: [], slotLoading: false,
      dispatch: noop as never, queryClient, showActionError,
      composer: {
        inputRef: ref(null), setInput: noop, drafts: ref({}), fileDrafts: ref({}), setPendingFiles: noop,
        currentProjectRef: ref(undefined), voiceCaretRef: ref(null), voicePendingCaretRef: ref(null), saveDrafts: noop,
      },
      capture: {
        setUploading: noop, setUploadError: noop, setUploadHint: noop, setResizedInfo: noop,
        snipSlotRef: ref(null), setSnipFrame: noop,
      },
    } as unknown as Parameters<typeof useChatPageResourcesController>[0]
    const { rerender } = renderHook(() => useChatPageResourcesController(props), { wrapper })
    await waitFor(() => expect(showActionError).toHaveBeenCalledTimes(1))
    expect(showActionError.mock.calls[0][0]).toContain('boom')
    rerender()
    rerender()
    expect(showActionError).toHaveBeenCalledTimes(1)
  })
})

describe('artifact-reference breadcrumb failure', () => {
  async function openWith(rejection: unknown) {
    vi.spyOn(api, 'recordArtifactReference').mockRejectedValue(rejection)
    vi.spyOn(api, 'artifact').mockResolvedValue({ kind: 'markdown', content: '' } as never)
    const showActionError = vi.fn()
    const tabsCtl = { openArtifact: vi.fn() } as unknown as Parameters<typeof usePanelDocumentActions>[0]['tabsCtl']
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { result } = renderHook(() => usePanelDocumentActions({
      tabsCtl, slotRef: { current: 'slot' }, queryClient, showActionError,
    }))
    await act(async () => { await result.current.openArtifact('a-slug') })
    // Let the fire-and-forget rejection settle.
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    return showActionError
  }

  it('reports a non-403 failure with its reason', async () => {
    const showActionError = await openWith(new ApiError(500, 'server exploded'))
    expect(showActionError).toHaveBeenCalledTimes(1)
    expect(showActionError.mock.calls[0][0]).toContain('server exploded')
  })

  it('reports an auth-required 403', async () => {
    const showActionError = await openWith(new ApiError(403, 'session expired', '', true))
    expect(showActionError).toHaveBeenCalledTimes(1)
    expect(showActionError.mock.calls[0][0]).toContain('session expired')
  })

  it('stays silent on the expected incognito 403', async () => {
    const showActionError = await openWith(new ApiError(403, 'incognito'))
    expect(showActionError).not.toHaveBeenCalled()
  })
})
