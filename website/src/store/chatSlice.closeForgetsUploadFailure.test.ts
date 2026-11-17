/* Closing a chat drops the upload failure held for it, since the chat is never
 * shown again. A close answered 404 closed nothing here: another close popped
 * the key and may still restore it, so its held failure is kept. */
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../lib/scrollQuiet', () => ({ whenScrollQuiet: () => Promise.resolve() }))
vi.mock('../api/client', () => ({
  api: {
    deleteChatSlot: vi.fn(() => Promise.resolve({ ok: true })),
    chatSlotDetail: vi.fn(() => Promise.resolve({ messages: [], has_more: false, total: 0, next_before: 0, running: false })),
    chatSlots: vi.fn(() => Promise.resolve([])),
  },
}))

import dashboardReducer, { sseSlots } from './dashboardSlice'
import chatReducer, { deleteSlot } from './chatSlice'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { clearSlotUploadFailures, holdSlotUploadFailure, takeSlotUploadFailure } from '../utils/slotUploadFailures'

const deleteChatSlot = vi.mocked(api.deleteChatSlot)

function storeWithChat() {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer },
    middleware: g => g({ serializableCheck: false, immutableCheck: false }),
  })
  store.dispatch(sseSlots([{ key: 'chat-a', title: 'A', messages: 1, running: false } as unknown as Parameters<typeof sseSlots>[0][number]]))
  return store
}

afterEach(() => { clearSlotUploadFailures(); deleteChatSlot.mockReset(); deleteChatSlot.mockResolvedValue({ ok: true } as never) })

describe('closing a chat with a held upload failure', () => {
  it('drops the failure once the close succeeds', async () => {
    const store = storeWithChat()
    holdSlotUploadFailure('chat-a', 'Upload failed: disk full')
    await store.dispatch(deleteSlot('chat-a'))
    expect(takeSlotUploadFailure('chat-a')).toBe('')
  })

  it('keeps the failure when the close answers 404', async () => {
    deleteChatSlot.mockRejectedValueOnce(new ApiError(404, 'not found'))
    const store = storeWithChat()
    holdSlotUploadFailure('chat-a', 'Upload failed: disk full')
    await store.dispatch(deleteSlot('chat-a'))
    expect(takeSlotUploadFailure('chat-a')).toBe('Upload failed: disk full')
  })
})
