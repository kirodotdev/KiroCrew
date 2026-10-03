import type { Dispatch, SetStateAction } from 'react'
import { useRef } from 'react'
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider, type QueryClientProviderProps } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AppDispatch } from '../store'
import type { ResizeInfo } from '../utils/resizeImage'

const captureSeam = vi.hoisted(() => ({
  supported: false,
  captureScreen: vi.fn(),
  screenshot: vi.fn(),
  hold: vi.fn(),
}))
const panelTabs = vi.hoisted(() => ({
  tabs: [] as Array<{ kind: string }>,
  openView: vi.fn(),
  openFolder: vi.fn(),
  openDiff: vi.fn(),
}))
const closeSearch = vi.hoisted(() => vi.fn())

vi.mock('../components/ChatDropOverlay', () => ({
  useChatFileDrop: () => ({ active: false, dropTargetProps: {} }),
}))
vi.mock('../components/WebPreviewPanel', () => ({ PREVIEW_SNIP_EVENT: 'kirocrew-web-preview-snip' }))
vi.mock('../utils/browserAnnotations', () => ({ PREVIEW_ANNOTATE_EVENT: 'kirocrew-preview-annotate' }))
vi.mock('../hooks/useMessageSearch', () => ({
  useMessageSearch: () => ({ isOpen: false, close: closeSearch }),
}))
vi.mock('../hooks/panelTabRegistry', () => ({ usePanelTabDescriptors: () => [] }))
vi.mock('../hooks/usePanelTabs', () => ({
  useAnyLiveAppTab: () => false,
  usePanelTabs: () => panelTabs,
}))
vi.mock('../hooks/usePanelDocumentActions', () => ({
  usePanelDocumentActions: () => ({ openFile: vi.fn(), openArtifact: vi.fn(), saveFile: vi.fn() }),
}))
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ colorTheme: null }) }))
vi.mock('../hooks/useScreenSnip', () => ({
  get screenSnipSupported() { return captureSeam.supported },
  captureScreen: captureSeam.captureScreen,
  currentTabCaptureDeps: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: vi.fn().mockResolvedValue({}),
    screenshot: captureSeam.screenshot,
  },
}))
vi.mock('../utils/composerSendHolds', () => ({
  cancelComposerUploads: vi.fn(),
  finishComposerAttachment: vi.fn(),
  holdComposerSend: captureSeam.hold,
  registerComposerUpload: vi.fn(),
  releaseComposerSend: vi.fn(),
  unregisterComposerUpload: vi.fn(),
  useComposerUploadCancellable: () => false,
}))

import { useChatPageResourcesController } from '../pages/chat/useChatPageResourcesController'

function wrapper({ children }: QueryClientProviderProps) {
  return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>
}

function useSlotlessController() {
  const activeSlotRef = useRef<string | null>(null)
  const inputRef = useRef('')
  const drafts = useRef<Record<string, string>>({})
  const currentProjectRef = useRef<string | undefined>(undefined)
  const voiceCaretRef = useRef<{ start: number; end: number } | null>(null)
  const voicePendingCaretRef = useRef<number | null>(null)
  const snipSlotRef = useRef<string | null>(null)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return useChatPageResourcesController({
    activeSlot: null,
    activeSlotRef,
    messages: [],
    slotLoading: false,
    dispatch: vi.fn() as unknown as AppDispatch,
    queryClient,
    showActionError: vi.fn(),
    composer: {
      inputRef,
      setInput: vi.fn() as Dispatch<SetStateAction<string>>,
      drafts,
      currentProjectRef,
      voiceCaretRef,
      voicePendingCaretRef,
      saveDrafts: vi.fn(),
    },
    capture: {
      setUploading: vi.fn() as Dispatch<SetStateAction<boolean>>,
      setUploadError: vi.fn() as Dispatch<SetStateAction<string>>,
      setUploadHint: vi.fn() as Dispatch<SetStateAction<string>>,
      setResizedInfo: vi.fn() as Dispatch<SetStateAction<Record<string, ResizeInfo>>>,
      snipSlotRef,
      setSnipFrame: vi.fn() as Dispatch<SetStateAction<HTMLCanvasElement | null>>,
    },
  })
}

describe('slotless capture entry points', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    captureSeam.supported = false
    captureSeam.screenshot.mockResolvedValue({ path: '/shots/should-not-exist.png' })
    captureSeam.captureScreen.mockResolvedValue(null)
  })

  it('does not start the native screenshot path or take a hold', async () => {
    const { result } = renderHook(() => useSlotlessController(), { wrapper })

    await act(async () => { await result.current.handleCapture() })

    expect(captureSeam.screenshot).not.toHaveBeenCalled()
    expect(captureSeam.captureScreen).not.toHaveBeenCalled()
    expect(captureSeam.hold).not.toHaveBeenCalled()
  })

  it('does not start preview snip capture or take a hold', async () => {
    captureSeam.supported = true
    renderHook(() => useSlotlessController(), { wrapper })

    await act(async () => { window.dispatchEvent(new Event('kirocrew-web-preview-snip')) })

    expect(captureSeam.captureScreen).not.toHaveBeenCalled()
    expect(captureSeam.screenshot).not.toHaveBeenCalled()
    expect(captureSeam.hold).not.toHaveBeenCalled()
  })
})
