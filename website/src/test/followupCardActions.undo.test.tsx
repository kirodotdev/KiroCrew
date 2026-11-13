// The applied folder card's Undo runs through the real fire-and-forget move hook:
// the card is cleared only once the server acknowledged the move back to the top
// level, and a failed move keeps the card and lands on the shell banner, which
// does not depend on the card still being mounted.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'
import type { ChatSlot } from '../types'
import { store } from '../store'
import { api } from '../api/client'
import { sseSlots } from '../store/dashboardSlice'
import { useMoveSlotToFolder } from '../hooks/useMoveSlotToFolder'
import { useFollowupCardActions } from '../pages/chat/page/followupCardActions'
import { clearFolderSuggestion, setAgentSwitchNotice } from '../store/chatSlice'

function renderActions() {
  // The card clear goes through this `dispatch`; the move hook itself dispatches
  // its optimistic write, rollback and banner through the Provider's store.
  const dispatch = vi.fn()
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <Provider store={store}><QueryClientProvider client={queryClient}>{children}</QueryClientProvider></Provider>
  )
  const { result } = renderHook(() => {
    const moveSlotToFolder = useMoveSlotToFolder()
    return useFollowupCardActions({
      dispatch,
      moveSlotToFolder,
      activeSlot: 'chat-1',
      folderSuggestion: { folderId: 'f-1', folderName: 'feature', breadcrumb: 'feature', ts: 7, turns: 0 },
      followupTsBySlot: {},
      inputRef: { current: '' },
    })
  }, { wrapper })
  return { result, dispatch }
}

beforeEach(() => {
  // Auto-applied: the session already sits in the suggested folder.
  const slot: ChatSlot = { key: 'chat-1', title: 'chat-1', messages: 0, running: false, folder_id: 'f-1' }
  store.dispatch(sseSlots([slot]))
})
afterEach(() => {
  vi.restoreAllMocks()
  store.dispatch(sseSlots([]))
  store.dispatch(setAgentSwitchNotice(null))
})

describe('folder suggestion Undo', () => {
  it('moves the session to the top level, then clears the card it rendered', async () => {
    const move = vi.spyOn(api, 'setSlotFolder').mockResolvedValue({ ok: true } as never)
    const { result, dispatch } = renderActions()
    act(() => { result.current.folderSuggestionUndo() })
    await waitFor(() => expect(dispatch).toHaveBeenCalledWith(clearFolderSuggestion({ slot: 'chat-1', ts: 7 })))
    expect(move).toHaveBeenCalledWith('chat-1', '')
  })

  it('keeps the card and reports the failure on the shell banner when the move fails', async () => {
    vi.spyOn(api, 'setSlotFolder').mockRejectedValue(new Error('gateway unreachable'))
    const { result, dispatch } = renderActions()
    act(() => { result.current.folderSuggestionUndo() })
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).toMatchObject({ message: 'gateway unreachable', error: true }))
    expect(store.getState().dashboard.slots.find(s => s.key === 'chat-1')?.folder_id).toBe('f-1')
    expect(dispatch).not.toHaveBeenCalledWith(clearFolderSuggestion({ slot: 'chat-1', ts: 7 }))
  })
})
