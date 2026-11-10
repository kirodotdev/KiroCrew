/**
 * Accepting a folder suggestion clears the card only once the gateway has
 * acknowledged the move. A refused move rolls the session back, so clearing the
 * card on the click would leave the session where it was with nothing on screen
 * saying so; the card stays and the move hook reports the refusal.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ setSlotFolder: vi.fn() }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import { store } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { setAgentSwitchNotice, setFolderSuggestion } from '../store/chatSlice'
import { ApiError } from '../api/apiError'
import { useMoveSlotToFolder } from '../hooks/useMoveSlotToFolder'
import { useFollowupCardActions } from '../pages/chat/page/followupCardActions'

const SLOT = 'chat-suggest-1'
const folderOf = () => store.getState().dashboard.slots.find(s => s.key === SLOT)?.folder_id
const cardOf = () => store.getState().chat.folderSuggestions?.[SLOT]

function renderAccept() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}><Provider store={store}>{children}</Provider></QueryClientProvider>
  )
  return renderHook(() => {
    const moveSlotToFolder = useMoveSlotToFolder()
    return useFollowupCardActions({
      dispatch: store.dispatch,
      moveSlotToFolder,
      activeSlot: SLOT,
      folderSuggestion: cardOf(),
      followupTsBySlot: {},
      inputRef: { current: '' },
    })
  }, { wrapper })
}

beforeEach(() => {
  const slot: ChatSlot = { key: SLOT, title: SLOT, messages: 0, running: false, folder_id: '' }
  store.dispatch(sseSlots([slot]))
  store.dispatch(setFolderSuggestion({ slot: SLOT, folderId: 'f1', folderName: 'Notes', breadcrumb: 'Notes', ts: 100 }))
})
afterEach(() => {
  vi.clearAllMocks()
  store.dispatch(sseSlots([]))
  store.dispatch(setAgentSwitchNotice(null))
})

describe('folderSuggestionAccept', () => {
  it('keeps the card when the gateway refuses the move', async () => {
    mocks.setSlotFolder.mockRejectedValueOnce(new ApiError(403, 'this folder belongs to a Bot and is managed for you'))
    const { result } = renderAccept()
    act(() => result.current.folderSuggestionAccept('f1'))
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice?.error).toBe(true))
    expect(folderOf()).toBeFalsy()
    expect(cardOf()?.folderId).toBe('f1')
  })

  it('clears the card when the move is acknowledged after the page unmounted', async () => {
    let ack!: (v: unknown) => void
    mocks.setSlotFolder.mockReturnValueOnce(new Promise(resolve => { ack = resolve }))
    const { result, unmount } = renderAccept()
    act(() => result.current.folderSuggestionAccept('f1'))
    await waitFor(() => expect(mocks.setSlotFolder).toHaveBeenCalledWith(SLOT, 'f1'))
    unmount()
    await act(async () => { ack({}) })
    await waitFor(() => expect(cardOf()).toBeUndefined())
  })

  it('clears the card once the move is acknowledged', async () => {
    mocks.setSlotFolder.mockResolvedValueOnce({})
    const { result } = renderAccept()
    act(() => result.current.folderSuggestionAccept('f1'))
    await waitFor(() => expect(cardOf()).toBeUndefined())
    expect(folderOf()).toBe('f1')
  })
})
