// The applied folder card's Undo: the card is cleared only once the server
// acknowledged the move back to the top level, and a failed move rejects to the
// card (which keeps itself and shows the error) without clearing it.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import type { ReactNode } from 'react'
import { store } from '../store'
import { api } from '../api/client'
import { useFollowupCardActions } from '../pages/chat/page/followupCardActions'
import { clearFolderSuggestion } from '../store/chatSlice'

function renderActions() {
  const dispatch = vi.fn()
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <Provider store={store}><QueryClientProvider client={queryClient}>{children}</QueryClientProvider></Provider>
  )
  const { result } = renderHook(() => useFollowupCardActions({
    dispatch,
    moveSlotToFolder: vi.fn(),
    activeSlot: 'chat-1',
    folderSuggestion: { folderId: 'f-1', folderName: 'feature', breadcrumb: 'feature', ts: 7, turns: 0 },
    followupTsBySlot: {},
    inputRef: { current: '' },
  }), { wrapper })
  return { result, dispatch }
}

afterEach(() => { vi.restoreAllMocks() })

describe('folder suggestion Undo', () => {
  it('moves the session to the top level, then clears the card it rendered', async () => {
    const move = vi.spyOn(api, 'setSlotFolder').mockResolvedValue({ ok: true } as never)
    const { result, dispatch } = renderActions()
    await act(async () => { await result.current.folderSuggestionUndo() })
    expect(move).toHaveBeenCalledWith('chat-1', '')
    expect(dispatch).toHaveBeenCalledWith(clearFolderSuggestion({ slot: 'chat-1', ts: 7 }))
  })

  it('rejects and keeps the card when the move fails', async () => {
    vi.spyOn(api, 'setSlotFolder').mockRejectedValue(new Error('gateway unreachable'))
    const { result, dispatch } = renderActions()
    await act(async () => {
      await expect(result.current.folderSuggestionUndo()).rejects.toThrow('gateway unreachable')
    })
    expect(dispatch).not.toHaveBeenCalledWith(clearFolderSuggestion({ slot: 'chat-1', ts: 7 }))
  })
})
