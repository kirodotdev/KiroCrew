/**
 * #9186: two resource-region failures that used to be silent now reach the
 * user through `showActionError`, each exactly once.
 *  - a failed source-host config read notifies once per failure, not per render,
 *    and a re-read the gateway never answered stays silent while config is cached;
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
import { i18nT } from '../i18n/t'
import { forgetSlotSuccession, recordSlotSuccession } from '../utils/slotSuccession'
import { clearSlotUploadFailures, takeSlotUploadFailure } from '../utils/slotUploadFailures'

afterEach(() => { vi.restoreAllMocks() })

const ref = <T,>(current: T) => ({ current })

describe('source-host config read failure', () => {
  function renderWithRejection(rejection: unknown, mock = true) {
    if (mock) vi.spyOn(api, 'dashboardConfig').mockRejectedValue(rejection)
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
    return { showActionError, rerender, queryClient }
  }

  it('notifies once and does not re-notify on re-render', async () => {
    const { showActionError, rerender } = renderWithRejection(new Error('boom'))
    await waitFor(() => expect(showActionError).toHaveBeenCalledTimes(1))
    expect(showActionError.mock.calls[0][0]).toContain('boom')
    rerender()
    rerender()
    expect(showActionError).toHaveBeenCalledTimes(1)
  })

  it('names the inert links when the gateway answers with an HTTP error', async () => {
    const { showActionError } = renderWithRejection(new ApiError(500, 'config unreadable'))
    await waitFor(() => expect(showActionError).toHaveBeenCalledTimes(1))
    expect(showActionError.mock.calls[0][0]).toBe(
      i18nT('pages.chatPage.source_hosts_failed_reason', { reason: 'config unreadable' }),
    )
  })

  it.each([
    ['a fetch rejection', new TypeError('Failed to fetch')],
    ['a fronting proxy 502', new ApiError(502, 'Bad Gateway')],
  ])('stays silent on %s the gateway never answered while config is cached', async (_label, rejection) => {
    vi.spyOn(api, 'dashboardConfig')
      .mockResolvedValueOnce({ gitlab_hosts: ['gitlab.example.com'] } as never)
      .mockRejectedValue(rejection)
    const { showActionError, queryClient } = renderWithRejection(undefined, false)
    await waitFor(() => expect(queryClient.getQueryState(['dashboardConfig'])?.status).toBe('success'))
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['dashboardConfig'] }) })
    await waitFor(() => expect(queryClient.getQueryState(['dashboardConfig'])?.errorUpdatedAt).toBeGreaterThan(0))
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(showActionError).not.toHaveBeenCalled()
  })

  it('still notifies when a first read the gateway never answered leaves nothing cached', async () => {
    const { showActionError } = renderWithRejection(new TypeError('Failed to fetch'))
    await waitFor(() => expect(showActionError).toHaveBeenCalledTimes(1))
  })
})

describe('dropping a space-named folder (#30416)', () => {
  /** A drop payload: a directory item whose resolved path is `dirPath`, plus
   *  the given regular `files`, to exercise mixed drops. The bridge resolves
   *  each item to the path its File name carries. */
  function dirDrop(dirPath: string, files: File[] = []): DataTransfer {
    const w = window as { kirocrew?: { getPathForFile?: (f: File) => string } }
    w.kirocrew = { getPathForFile: f => f.name }
    const items: unknown[] = [
      { kind: 'file', webkitGetAsEntry: () => ({ isDirectory: true }), getAsFile: () => new File(['x'], dirPath, { type: '' }) },
    ]
    for (const f of files) {
      items.push({ kind: 'file', webkitGetAsEntry: () => ({ isDirectory: false }), getAsFile: () => f })
    }
    return { items, files: [] } as unknown as DataTransfer
  }
  const plainFile = (name = 'a.txt') => new File(['x'], name, { type: 'text/plain' })

  function renderController(setUploadError: (v: string) => void, setUploadHint: (v: string) => void, setInput: (v: unknown) => void) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, createElement(ThemeProvider, null, children))
    const noop = vi.fn()
    const props = {
      activeSlot: 'slot', activeSlotRef: ref<string | null>('slot'), messages: [], slotLoading: false,
      dispatch: noop as never, queryClient, showActionError: noop,
      composer: {
        inputRef: ref(''), setInput, drafts: ref({}), fileDrafts: ref({}), setPendingFiles: noop,
        currentProjectRef: ref(undefined), voiceCaretRef: ref(null), voicePendingCaretRef: ref(null), saveDrafts: noop,
        composerLockedRef: ref(false),
      },
      capture: {
        setUploading: noop, setUploadError, setUploadHint, setResizedInfo: noop,
        snipSlotRef: ref(null), setSnipFrame: noop,
      },
    } as unknown as Parameters<typeof useChatPageResourcesController>[0]
    return renderHook(() => useChatPageResourcesController(props), { wrapper })
  }

  afterEach(() => {
    const w = window as { kirocrew?: unknown }
    delete w.kirocrew
  })

  function dropOn(result: ReturnType<typeof renderController>['result'], dataTransfer: DataTransfer) {
    const event = { dataTransfer, preventDefault: vi.fn(), stopPropagation: vi.fn() } as unknown as Parameters<typeof result.current.dropTargetProps.onDrop>[0]
    act(() => { result.current.dropTargetProps.onDrop(event) })
  }

  it('shows the folder-refused hint (not the danger error banner) and uploads nothing', async () => {
    const uploadSpy = vi.spyOn(api, 'uploadFiles')
    vi.spyOn(api, 'dashboardConfig').mockRejectedValue(new Error('ignored'))
    const setUploadError = vi.fn()
    const setUploadHint = vi.fn()
    const setInput = vi.fn()
    const { result } = renderController(setUploadError, setUploadHint, setInput)
    dropOn(result, dirDrop('/Users/me/p251 folder'))
    // The refusal is a client-side validation refusal (nothing sent): it goes
    // to the hint surface, not the danger error banner.
    expect(setUploadHint).toHaveBeenCalledWith(i18nT('components.chatInput.tree_drop_folder_refused'))
    expect(setUploadError).not.toHaveBeenCalledWith(i18nT('components.chatInput.tree_drop_folder_refused'))
    expect(setUploadError).not.toHaveBeenCalledWith(
      i18nT('pages.chatPage.upload_failed_check_file_type_and_size_max_50_mb'),
    )
    // Nothing added: no upload started, no composer text inserted.
    expect(uploadSpy).not.toHaveBeenCalled()
    expect(setInput).not.toHaveBeenCalled()
  })

  it('shows the folder-refused hint SYNCHRONOUSLY on a mixed drop with an uploadable file', async () => {
    // The local refusal is decided synchronously (not from the upload's async
    // callback), so a mixed file+folder drop uploads the file AND sets the
    // folder hint in the same tick -- it cannot land in another session or over
    // a newer refusal if the user switches session while the upload runs.
    const uploadSpy = vi.spyOn(api, 'uploadFiles').mockResolvedValue({ paths: ['/tmp/a.txt'] } as never)
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ upload_max_mb: 50 } as never)
    const setUploadError = vi.fn()
    const setUploadHint = vi.fn()
    const setInput = vi.fn()
    const { result } = renderController(setUploadError, setUploadHint, setInput)
    dropOn(result, dirDrop('/Users/me/p251 folder', [plainFile()]))
    // Set within the drop handler's synchronous run, before any upload await.
    expect(setUploadHint).toHaveBeenLastCalledWith(i18nT('components.chatInput.tree_drop_folder_refused'))
    expect(uploadSpy).toHaveBeenCalledTimes(1)
  })

  it('clears a stale upload-error banner on a refused-folder-only drop', async () => {
    // A folder-only drop skips uploadFiles (which is what clears the error
    // otherwise), so the handler must clear a prior upload failure itself --
    // the refusal is not that error.
    vi.spyOn(api, 'uploadFiles')
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ upload_max_mb: 50 } as never)
    const setUploadError = vi.fn()
    const setUploadHint = vi.fn()
    const setInput = vi.fn()
    const { result } = renderController(setUploadError, setUploadHint, setInput)
    dropOn(result, dirDrop('/Users/me/p251 folder'))
    expect(setUploadError).toHaveBeenCalledWith('')
    expect(setUploadHint).toHaveBeenCalledWith(i18nT('components.chatInput.tree_drop_folder_refused'))
  })

  it('does NOT overwrite the files-refused hint with the folder hint on a mixed drop (#30416)', async () => {
    // Files refused by the local >20 pre-check: uploadFiles sets its own hint
    // and returns true, so the folder hint must NOT replace it -- otherwise the
    // user is never told the files were refused too.
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ upload_max_mb: 50 } as never)
    const uploadSpy = vi.spyOn(api, 'uploadFiles')
    const setUploadError = vi.fn()
    const setUploadHint = vi.fn()
    const setInput = vi.fn()
    const { result } = renderController(setUploadError, setUploadHint, setInput)
    const manyFiles = Array.from({ length: 21 }, (_, i) => plainFile(`f${i}.txt`))
    dropOn(result, dirDrop('/Users/me/p251 folder', manyFiles))
    // Flush any microtasks so a mistaken folder-hint set would also have run.
    await act(async () => { await Promise.resolve() })
    expect(setUploadHint).toHaveBeenCalledWith(i18nT('pages.chatPage.too_many_files_max_20'))
    expect(setUploadHint).not.toHaveBeenCalledWith(i18nT('components.chatInput.tree_drop_folder_refused'))
    // Local pre-check refused the files: nothing was sent.
    expect(uploadSpy).not.toHaveBeenCalled()
  })
})

describe('a failed upload reports to the chat it started in', () => {
  afterEach(() => { clearSlotUploadFailures() })

  function render(activeSlotRef: { current: string | null }, setUploadError: (v: string) => void, dispatch: (a: unknown) => void) {
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ upload_max_mb: 50 } as never)
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, createElement(ThemeProvider, null, children))
    const noop = vi.fn()
    const props = {
      activeSlot: 'slot-a', activeSlotRef, messages: [], slotLoading: false,
      dispatch: dispatch as never, queryClient, showActionError: noop,
      composer: {
        inputRef: ref(''), setInput: noop, drafts: ref({}), fileDrafts: ref({}), setPendingFiles: noop,
        currentProjectRef: ref(undefined), voiceCaretRef: ref(null), voicePendingCaretRef: ref(null), saveDrafts: noop,
        composerLockedRef: ref(false),
      },
      capture: {
        setUploading: noop, setUploadError, setUploadHint: noop, setResizedInfo: noop,
        snipSlotRef: ref(null), setSnipFrame: noop,
      },
    } as unknown as Parameters<typeof useChatPageResourcesController>[0]
    return renderHook(() => useChatPageResourcesController(props), { wrapper })
  }

  it('holds the failure for the started chat when another chat is on screen', async () => {
    let settle!: (v: unknown) => void
    vi.spyOn(api, 'uploadFiles').mockReturnValue(new Promise(r => { settle = r }) as never)
    const activeSlotRef = ref<string | null>('slot-a')
    const setUploadError = vi.fn()
    const dispatch = vi.fn()
    const { result } = render(activeSlotRef, setUploadError, dispatch)
    let pending: Promise<boolean> | undefined
    act(() => { pending = result.current.uploadFiles([new File(['x'], 'a.txt', { type: 'text/plain' })]) })
    activeSlotRef.current = 'slot-b'
    setUploadError.mockClear()
    await act(async () => { settle({ error: 'disk full' }); await pending })
    expect(setUploadError).not.toHaveBeenCalledWith(expect.stringContaining('disk full'))
    expect(takeSlotUploadFailure('slot-b')).toBe('')
    expect(takeSlotUploadFailure('slot-a')).toContain('disk full')
    // Shown once: taking it again finds nothing.
    expect(takeSlotUploadFailure('slot-a')).toBe('')
    expect(dispatch).not.toHaveBeenCalled()
  })

  it('holds a failed native screenshot for the chat it was taken for', async () => {
    let fail!: (e: unknown) => void
    vi.spyOn(api, 'screenshot').mockReturnValue(new Promise((_r, rej) => { fail = rej }) as never)
    const activeSlotRef = ref<string | null>('slot-a')
    const dispatch = vi.fn()
    const { result } = render(activeSlotRef, vi.fn(), dispatch)
    let pending: Promise<void> | undefined
    act(() => { pending = result.current.handleCapture() })
    activeSlotRef.current = 'slot-b'
    await act(async () => { fail(new Error('capture timed out')); await pending })
    expect(takeSlotUploadFailure('slot-a')).toContain('capture timed out')
  })

  it('holds the failure when the chat page has unmounted before the upload fails', async () => {
    let settle!: (v: unknown) => void
    vi.spyOn(api, 'uploadFiles').mockReturnValue(new Promise(r => { settle = r }) as never)
    const setUploadError = vi.fn()
    const dispatch = vi.fn()
    const { result, unmount } = render(ref<string | null>('slot-a'), setUploadError, dispatch)
    let pending: Promise<boolean> | undefined
    act(() => { pending = result.current.uploadFiles([new File(['x'], 'a.txt', { type: 'text/plain' })]) })
    unmount()
    setUploadError.mockClear()
    await act(async () => { settle({ error: 'disk full' }); await pending })
    expect(setUploadError).not.toHaveBeenCalledWith(expect.stringContaining('disk full'))
    expect(takeSlotUploadFailure('slot-a')).toContain('disk full')
  })

  it('holds a failed screenshot for the slot that replaced the one it started in', async () => {
    let fail!: (e: unknown) => void
    vi.spyOn(api, 'screenshot').mockReturnValue(new Promise((_r, rej) => { fail = rej }) as never)
    const activeSlotRef = ref<string | null>('slot-a')
    const dispatch = vi.fn()
    const { result } = render(activeSlotRef, vi.fn(), dispatch)
    let pending: Promise<void> | undefined
    act(() => { pending = result.current.handleCapture() })
    // A memory-mode switch replaces slot-a with slot-a2 while the capture runs,
    // and the person is on a third chat when it fails.
    recordSlotSuccession('slot-a', 'slot-a2')
    activeSlotRef.current = 'slot-b'
    await act(async () => { fail(new Error('capture timed out')); await pending })
    expect(takeSlotUploadFailure('slot-a')).toBe('')
    expect(takeSlotUploadFailure('slot-a2')).toContain('capture timed out')
    forgetSlotSuccession('slot-a')
  })

  it('follows the successor when the page unmounted with the replaced chat still in its frozen ref', async () => {
    let fail!: (e: unknown) => void
    vi.spyOn(api, 'screenshot').mockReturnValue(new Promise((_r, rej) => { fail = rej }) as never)
    const { result, unmount } = render(ref<string | null>('slot-a'), vi.fn(), vi.fn())
    let pending: Promise<void> | undefined
    act(() => { pending = result.current.handleCapture() })
    // The person left /chat; a remounted page then replaced slot-a with slot-a2.
    // This instance's ref still names slot-a.
    unmount()
    recordSlotSuccession('slot-a', 'slot-a2')
    await act(async () => { fail(new Error('capture timed out')); await pending })
    expect(takeSlotUploadFailure('slot-a')).toBe('')
    expect(takeSlotUploadFailure('slot-a2')).toContain('capture timed out')
    forgetSlotSuccession('slot-a')
  })

  it('shows the banner when a memory-mode switch put the upload\'s successor on screen', async () => {
    let settle!: (v: unknown) => void
    vi.spyOn(api, 'uploadFiles').mockReturnValue(new Promise(r => { settle = r }) as never)
    const activeSlotRef = ref<string | null>('slot-a')
    const setUploadError = vi.fn()
    const { result } = render(activeSlotRef, setUploadError, vi.fn())
    let pending: Promise<boolean> | undefined
    act(() => { pending = result.current.uploadFiles([new File(['x'], 'a.txt', { type: 'text/plain' })]) })
    // The switch made slot-a2 active but has not handed the upload over yet.
    recordSlotSuccession('slot-a', 'slot-a2')
    activeSlotRef.current = 'slot-a2'
    await act(async () => { settle({ error: 'disk full' }); await pending })
    expect(setUploadError).toHaveBeenCalledWith(expect.stringContaining('disk full'))
    expect(takeSlotUploadFailure('slot-a')).toBe('')
    forgetSlotSuccession('slot-a')
  })

  it('keeps the banner when the started chat is still on screen', async () => {
    vi.spyOn(api, 'uploadFiles').mockResolvedValue({ error: 'disk full' } as never)
    const setUploadError = vi.fn()
    const dispatch = vi.fn()
    const { result } = render(ref<string | null>('slot-a'), setUploadError, dispatch)
    await act(async () => { await result.current.uploadFiles([new File(['x'], 'a.txt', { type: 'text/plain' })]) })
    expect(setUploadError).toHaveBeenCalledWith(expect.stringContaining('disk full'))
    expect(takeSlotUploadFailure('slot-a')).toBe('')
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
