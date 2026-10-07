import type { Dispatch, SetStateAction } from 'react'
import { useRef } from 'react'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider, type QueryClientProviderProps } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AppDispatch } from '../store'
import type { ResizeInfo } from '../utils/resizeImage'

// Where a finished attachment lands, and that a capture cannot hold Send
// forever. The holds registry is the real one: these tests are about what it
// does with a file once the composer that started it is no longer showing it.

const apiSeam = vi.hoisted(() => ({
  uploadFiles: vi.fn(),
  screenshot: vi.fn(),
}))

vi.mock('../components/ChatDropOverlay', () => ({
  useChatFileDrop: () => ({ active: false, dropTargetProps: {} }),
}))
vi.mock('../components/WebPreviewPanel', () => ({ PREVIEW_SNIP_EVENT: 'kirocrew-web-preview-snip' }))
vi.mock('../utils/browserAnnotations', () => ({ PREVIEW_ANNOTATE_EVENT: 'kirocrew-preview-annotate' }))
vi.mock('../hooks/useMessageSearch', () => ({
  useMessageSearch: () => ({ isOpen: false, close: vi.fn() }),
}))
vi.mock('../hooks/panelTabRegistry', () => ({ usePanelTabDescriptors: () => [] }))
vi.mock('../hooks/usePanelTabs', () => ({
  useAnyLiveAppTab: () => false,
  usePanelTabs: () => ({ tabs: [], openView: vi.fn(), openFolder: vi.fn(), openDiff: vi.fn() }),
}))
vi.mock('../hooks/usePanelDocumentActions', () => ({
  usePanelDocumentActions: () => ({ openFile: vi.fn(), openArtifact: vi.fn(), saveFile: vi.fn() }),
}))
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ colorTheme: null }) }))
vi.mock('../hooks/useScreenSnip', () => ({
  screenSnipSupported: false,
  captureScreen: vi.fn(),
  currentTabCaptureDeps: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: vi.fn().mockResolvedValue({}),
    uploadFiles: apiSeam.uploadFiles,
    screenshot: apiSeam.screenshot,
  },
}))

import { SCREENSHOT_DEADLINE_MS, useChatPageResourcesController } from '../pages/chat/useChatPageResourcesController'
import { __resetComposerSendHoldsForTests, isComposerSendHeld } from '../utils/composerSendHolds'

function wrapper({ children }: QueryClientProviderProps) {
  return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>
}

const fileDrafts: { current: Record<string, string[]> } = { current: {} }
const saveDrafts = vi.fn()
const showActionError = vi.fn()

function useController(slot: string) {
  const activeSlotRef = useRef<string | null>(slot)
  activeSlotRef.current = slot
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return useChatPageResourcesController({
    activeSlot: slot,
    activeSlotRef,
    messages: [],
    slotLoading: false,
    dispatch: vi.fn() as unknown as AppDispatch,
    queryClient,
    showActionError,
    composer: {
      inputRef: { current: '' },
      setInput: vi.fn() as Dispatch<SetStateAction<string>>,
      drafts: { current: {} },
      fileDrafts,
      currentProjectRef: { current: undefined },
      voiceCaretRef: { current: null },
      voicePendingCaretRef: { current: null },
      saveDrafts,
    },
    capture: {
      setUploading: vi.fn() as Dispatch<SetStateAction<boolean>>,
      setUploadError: vi.fn() as Dispatch<SetStateAction<string>>,
      setUploadHint: vi.fn() as Dispatch<SetStateAction<string>>,
      setResizedInfo: vi.fn() as Dispatch<SetStateAction<Record<string, ResizeInfo>>>,
      snipSlotRef: { current: null },
      setSnipFrame: vi.fn() as Dispatch<SetStateAction<HTMLCanvasElement | null>>,
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  fileDrafts.current = {}
  __resetComposerSendHoldsForTests()
})

afterEach(() => {
  vi.useRealTimers()
  __resetComposerSendHoldsForTests()
})

describe('where a finished upload lands', () => {
  it('lands in the starting session\'s saved file draft after a switch away', async () => {
    let finish!: (value: { paths: string[] }) => void
    apiSeam.uploadFiles.mockReturnValue(new Promise(resolve => { finish = resolve }))
    const view = renderHook(({ slot }) => useController(slot), { wrapper, initialProps: { slot: 'session-a' } })

    let upload!: Promise<void>
    act(() => { upload = view.result.current.uploadFiles([new File(['x'], 'a.txt')]) })
    expect(isComposerSendHeld('session-a')).toBe(true)

    view.rerender({ slot: 'session-b' })
    await act(async () => { finish({ paths: ['/up/a.txt'] }); await upload })

    // The saved draft is what a session switch back, a reload or a reopened
    // window restores from, so nothing is left waiting in memory.
    expect(fileDrafts.current['session-a']).toEqual(['/up/a.txt'])
    expect(fileDrafts.current['session-b']).toBeUndefined()
    expect(saveDrafts).toHaveBeenCalled()
    expect(isComposerSendHeld('session-a')).toBe(false)
  })
})

describe('screenshot capture bound', () => {
  it('releases Send when the capture request never answers', async () => {
    vi.useFakeTimers()
    // A request that only ends when its signal aborts, like a stalled fetch.
    apiSeam.screenshot.mockImplementation((signal?: AbortSignal) => new Promise((_, reject) => {
      signal?.addEventListener('abort', () => reject(signal.reason))
    }))
    const view = renderHook(() => useController('shot'), { wrapper })

    await act(async () => { void view.result.current.handleCapture() })
    expect(isComposerSendHeld('shot')).toBe(true)

    await act(async () => { await vi.advanceTimersByTimeAsync(SCREENSHOT_DEADLINE_MS) })

    expect(isComposerSendHeld('shot')).toBe(false)
    expect(showActionError).toHaveBeenCalledTimes(1)
  })
})
