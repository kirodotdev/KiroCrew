import type { Dispatch, SetStateAction } from 'react'
import { useRef } from 'react'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider, type QueryClientProviderProps } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AppDispatch } from '../store'
import type { ResizeInfo } from '../utils/resizeImage'

// `handleOpenCrewTab` is the ONE host reveal the Crew Mode dispatch-notice
// button calls, on the single-chat surface and (threaded down) in a split pane
// alike. It reuses the shared `openCrewSession` reveal — switch to the card's
// slot so the opened panel is THAT chat's (split view never moves activeSlot
// with pane focus), select Workers, open the dock, keepPanel=true because the
// button is a deliberate "open" — and adds the one thing openCrewSession does
// not: closing the find pane (which keeps the dock unmounted while open). These
// mock openCrewSession + the search controller and assert handleOpenCrewTab
// drives exactly that single path.

const closeSearch = vi.hoisted(() => vi.fn())
const openCrewSession = vi.hoisted(() => vi.fn())

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
  usePanelTabs: () => ({ tabs: [], openView: vi.fn(), openFolder: vi.fn(), openDiff: vi.fn() }),
}))
vi.mock('../hooks/usePanelDocumentActions', () => ({
  usePanelDocumentActions: () => ({ openFile: vi.fn(), openArtifact: vi.fn(), saveFile: vi.fn() }),
}))
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ colorTheme: null }) }))
vi.mock('../hooks/useScreenSnip', () => ({
  get screenSnipSupported() { return false },
  captureScreen: vi.fn(),
  currentTabCaptureDeps: vi.fn(),
  ScreenSnipRefusedError: class extends Error {},
}))
vi.mock('../api/client', () => ({
  api: { dashboardConfig: vi.fn().mockResolvedValue({}), screenshot: vi.fn() },
}))
vi.mock('../utils/composerSendHolds', () => ({
  cancelComposerUploads: vi.fn(),
  finishComposerAttachment: vi.fn(),
  holdComposerSend: vi.fn(),
  registerComposerUpload: vi.fn(),
  releaseComposerSend: vi.fn(),
  unregisterComposerUpload: vi.fn(),
  useComposerUploadCount: () => 0,
}))
// The single shared reveal `handleOpenCrewTab` reuses — mock it so the test
// asserts delegation rather than re-spelling switchSlot+openPanelView+open.
vi.mock('../pages/chat/command-center/CrewWorkerList', () => ({ openCrewSession }))

import { useChatPageResourcesController } from '../pages/chat/useChatPageResourcesController'

function wrapper({ children }: QueryClientProviderProps) {
  return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>
}

const dispatch = vi.fn() as unknown as AppDispatch

function useController(activeSlot: string | null) {
  const activeSlotRef = useRef<string | null>(activeSlot)
  const composerLockedRef = useRef(false)
  const inputRef = useRef('')
  const drafts = useRef<Record<string, string>>({})
  const currentProjectRef = useRef<string | undefined>(undefined)
  const voiceCaretRef = useRef<{ start: number; end: number } | null>(null)
  const voicePendingCaretRef = useRef<number | null>(null)
  const snipSlotRef = useRef<string | null>(null)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return useChatPageResourcesController({
    activeSlot,
    activeSlotRef,
    messages: [],
    slotLoading: false,
    dispatch,
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
      composerLockedRef,
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

describe('handleOpenCrewTab (Crew Mode dispatch-notice reveal)', () => {
  beforeEach(() => vi.clearAllMocks())

  it('reveals via the shared openCrewSession (keepPanel) and closes Find', () => {
    const { result } = renderHook(() => useController('conductor-1'), { wrapper })
    result.current.handleOpenCrewTab('conductor-1')

    // The one shared reveal — switch slot + select Workers + open the dock —
    // with keepPanel=true because the button is a deliberate open.
    expect(openCrewSession).toHaveBeenCalledTimes(1)
    expect(openCrewSession).toHaveBeenCalledWith(dispatch, 'conductor-1', true)
    // And the one thing openCrewSession does not do: close Find, else the dock
    // stays unmounted (shouldMountSidePanel) while search is open.
    expect(closeSearch).toHaveBeenCalledTimes(1)
  })

  it('in a split pane (card slot ≠ activeSlot) reveals the CARD slot, not the active one', () => {
    const { result } = renderHook(() => useController('other-chat'), { wrapper })
    result.current.handleOpenCrewTab('conductor-1')

    // openCrewSession switches to the card's slot first, so the panel it opens
    // belongs to the card's chat, not the active pane's.
    expect(openCrewSession).toHaveBeenCalledWith(dispatch, 'conductor-1', true)
    expect(closeSearch).toHaveBeenCalledTimes(1)
  })
})
