import type { Dispatch, SetStateAction } from 'react'
import { useRef } from 'react'
import { renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider, type QueryClientProviderProps } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { store, type AppDispatch } from '../store'
import { sseConnected, sseDisconnected } from '../store/dashboardSlice'
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
// A recognisable plain action in place of the thunk, so a test can see the
// switch reach `dispatch`.
const switchSlot = vi.hoisted(() => vi.fn((arg: { key: string }) => ({ type: 'test/switchSlot', payload: arg })))

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
const openFileTabMock = vi.hoisted(() => vi.fn())
// The host's `onOpened`, as the document actions would call it once a tab opened.
const docHooks = vi.hoisted(() => ({ onOpened: undefined as undefined | ((slot?: string | null) => void) }))
vi.mock('../hooks/usePanelDocumentActions', () => ({
  usePanelDocumentActions: (o: { onOpened?: (slot?: string | null) => void }) => {
    docHooks.onOpened = o.onOpened
    return { openFile: openFileTabMock, openArtifact: vi.fn(), saveFile: vi.fn() }
  },
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
vi.mock('../store/chatSlice', async (importOriginal) => ({
  ...await importOriginal<typeof import('../store/chatSlice')>(),
  switchSlot,
}))

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

// The folder twin of handleFileOpen's split-pane override (#9921, #35014),
// pinned here because this file already builds the controller.
describe('handleFolderOpen (split-pane slot override)', () => {
  beforeEach(() => { vi.clearAllMocks(); store.dispatch(sseConnected()) })

  it('stamps the folder tab with the pane slot it is given and switches the panel to that chat', () => {
    // The panel is bound to activeSlot, so without the switch the folder tab
    // sits in pane-chat's strip while the panel keeps showing active-chat's.
    const { result } = renderHook(() => useController('active-chat'), { wrapper })
    result.current.handleFolderOpen('/repo/evidence/', { slot: 'pane-chat' })
    const openFolder = vi.mocked(result.current.tabsCtl.openFolder)
    expect(openFolder).toHaveBeenCalledTimes(1)
    expect(openFolder).toHaveBeenCalledWith('/repo/evidence/', 'pane-chat', { hold: true })
    expect(switchSlot).toHaveBeenCalledTimes(1)
    expect(switchSlot).toHaveBeenCalledWith({ key: 'pane-chat', announceOnMissing: true, gesture: true })
    const switchCall = vi.mocked(dispatch).mock.calls.findIndex(([a]) => (a as { type?: string }).type === 'test/switchSlot')
    expect(switchCall).not.toBe(-1)
    // Held first, then switched (the order `openCrewSession` uses).
    expect(openFolder.mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(dispatch).mock.invocationCallOrder[switchCall])
    expect(closeSearch).toHaveBeenCalledTimes(1)
  })

  it('a file opened from a split pane switches the panel to that pane\'s chat and opens held', () => {
    // A file chip or a diff's Open button in split pane B: the tab lands in
    // B's strip, so the panel must show B, or the click seems to do nothing.
    const { result } = renderHook(() => useController('active-chat'), { wrapper })
    void result.current.handleFileOpen('/repo/a.md', { slot: 'pane-chat', line: 3 })
    expect(openFileTabMock).toHaveBeenCalledWith('/repo/a.md', { slot: 'pane-chat', line: 3, hold: true })
    // No switch until the tab is open: a failed read must not blank the pane.
    expect(switchSlot).not.toHaveBeenCalled()
    docHooks.onOpened?.('pane-chat')
    expect(switchSlot).toHaveBeenCalledTimes(1)
    expect(switchSlot).toHaveBeenCalledWith({ key: 'pane-chat', announceOnMissing: true, gesture: true })
    expect(dispatch).toHaveBeenCalledWith({ type: 'test/switchSlot', payload: { key: 'pane-chat', announceOnMissing: true, gesture: true } })
  })

  it('a pane file that opens from cache while offline does not switch the panel', () => {
    // Offline the switch's fetch is rejected and blanks the pane, so a read
    // served from the query cache must not trigger it. The dock still opens.
    store.dispatch(sseDisconnected(null))
    const { result } = renderHook(() => useController('active-chat'), { wrapper })
    void result.current.handleFileOpen('/repo/a.md', { slot: 'pane-chat' })
    docHooks.onOpened?.('pane-chat')
    expect(switchSlot).not.toHaveBeenCalled()
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: expect.stringContaining('openActivityPanel') }))
  })

  it('a file opened for the chat the panel already shows does not switch', () => {
    const { result } = renderHook(() => useController('active-chat'), { wrapper })
    void result.current.handleFileOpen('/repo/a.md', { slot: 'active-chat' })
    void result.current.handleFileOpen('/repo/b.md')
    expect(openFileTabMock).toHaveBeenNthCalledWith(1, '/repo/a.md', { slot: 'active-chat' })
    expect(openFileTabMock).toHaveBeenNthCalledWith(2, '/repo/b.md', undefined)
    docHooks.onOpened?.('active-chat')
    docHooks.onOpened?.(null)
    docHooks.onOpened?.()
    expect(switchSlot).not.toHaveBeenCalled()
  })

  it('without an override stamps the active slot', () => {
    const { result } = renderHook(() => useController('active-chat'), { wrapper })
    result.current.handleFolderOpen('/repo/evidence/')
    expect(result.current.tabsCtl.openFolder).toHaveBeenCalledWith('/repo/evidence/', 'active-chat')
    expect(switchSlot).not.toHaveBeenCalled()
  })
})
