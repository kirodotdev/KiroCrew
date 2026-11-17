/**
 * An attachment failure held for a chat that was off screen is shown in the
 * upload banner when that chat comes on screen, once, and never on another chat.
 */
import { StrictMode } from 'react'
import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useComposerDraftLifecycle, useComposerDraftStores } from '../pages/chat/page/composerDrafts'
import { clearSlotUploadFailures, holdSlotUploadFailure } from '../utils/slotUploadFailures'

afterEach(() => { clearSlotUploadFailures() })

const ref = <T,>(current: T) => ({ current })

function renderLifecycle(initial: string, wrapper?: typeof StrictMode) {
  const setUploadError = vi.fn()
  const staging = {
    stagedNowRef: ref({ files: [], pastes: [], sessions: [] }),
    pendingFilesRef: ref([]), pasteBlocksRef: ref([]), pendingSessionsRef: ref([]),
    setPendingFiles: vi.fn(), setPasteBlocks: vi.fn(), setPendingSessions: vi.fn(),
    setUploadError, setUploadHint: vi.fn(),
    pickedFileTokens: ref({}),
  }
  const hook = renderHook(({ activeSlot }) => {
    const stores = useComposerDraftStores()
    useComposerDraftLifecycle({
      activeSlot, stores, staging, prevSlot: ref<string | null>(null), composerSlotRef: ref(activeSlot),
      inputRef: ref(''), setInput: vi.fn(), consumedPrefillRef: ref(null), foregroundCreateId: null,
      foregroundCreateIdRef: ref(null), lastCreatedActivationRef: ref(null),
      knowledgeFetchRef: ref({ clearResults: vi.fn() }), raisePrefillHint: vi.fn(), setPrefillHint: vi.fn(),
      setActionError: vi.fn(), updateHistoryQuery: vi.fn(),
    } as never)
  }, { initialProps: { activeSlot: initial }, wrapper })
  return { ...hook, setUploadError }
}

describe('a held upload failure', () => {
  it('shows in the banner when its chat comes on screen, once', () => {
    const { rerender, setUploadError } = renderLifecycle('slot-b')
    expect(setUploadError).toHaveBeenLastCalledWith('')
    holdSlotUploadFailure('slot-a', 'Upload failed: disk full')
    rerender({ activeSlot: 'slot-a' })
    expect(setUploadError).toHaveBeenLastCalledWith('Upload failed: disk full')
    rerender({ activeSlot: 'slot-b' })
    expect(setUploadError).toHaveBeenLastCalledWith('')
    rerender({ activeSlot: 'slot-a' })
    expect(setUploadError).toHaveBeenLastCalledWith('')
  })

  it('shows when the page mounts on its chat', () => {
    holdSlotUploadFailure('slot-a', 'Upload failed: disk full')
    const { setUploadError } = renderLifecycle('slot-a')
    expect(setUploadError).toHaveBeenLastCalledWith('Upload failed: disk full')
  })

  it('survives the StrictMode replay of the mount effect', () => {
    holdSlotUploadFailure('slot-a', 'Upload failed: disk full')
    const { setUploadError } = renderLifecycle('slot-a', StrictMode)
    expect(setUploadError).toHaveBeenLastCalledWith('Upload failed: disk full')
  })

  it('shows at once when held for the chat already on screen', () => {
    // An earlier page instance started the upload; it fails after this page
    // mounted on the same chat and drained the table.
    const { setUploadError } = renderLifecycle('slot-a')
    act(() => { holdSlotUploadFailure('slot-a', 'Upload failed: disk full') })
    expect(setUploadError).toHaveBeenLastCalledWith('Upload failed: disk full')
    act(() => { holdSlotUploadFailure('slot-b', 'Upload failed: other') })
    expect(setUploadError).toHaveBeenLastCalledWith('Upload failed: disk full')
  })
})
