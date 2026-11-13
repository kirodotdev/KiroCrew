/**
 * Prompt-stash cleanup driven by `src/store/chatSlice.ts` history thunks: only a
 * confirmed delete of the same session clears a stash. Split from
 * `ChatSliceCoverage.test.tsx` to keep that file under the line cap.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { deleteHistorySession, fetchHistory } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import { addStashEntry, loadPromptStash, makeStashEntry } from '../utils/promptStash'

const apiMock = vi.hoisted(() => ({
  chatSlots: vi.fn(),
  dashboardConfig: vi.fn(),
  deleteSession: vi.fn(),
  sessions: vi.fn(),
}))

vi.mock('../api/client', () => ({ api: apiMock }))

function makeStore() {
  return configureStore({
    reducer: {
      chat: chatReducer,
      dashboard: dashboardReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

beforeEach(() => {
  for (const fn of Object.values(apiMock)) fn.mockReset()
  apiMock.chatSlots.mockResolvedValue([])
  apiMock.dashboardConfig.mockResolvedValue({ default_memory_mode: 'persistent' })
})

describe('chatSlice history thunks and prompt stashes', () => {
  it('does not sweep prompt stashes from an incomplete history page', async () => {
    expect(addStashEntry('not-in-first-page', makeStashEntry('keep until complete', []))).toBe(true)
    apiMock.sessions.mockResolvedValueOnce({
      sessions: [{ key: 'dashboard_old-chat' }],
      has_more: true,
    })
    const store = makeStore()

    await store.dispatch(fetchHistory(false))

    expect(loadPromptStash('not-in-first-page').map(entry => entry.text)).toEqual(['keep until complete'])
  })

  it('keeps a stash for a session the filtered history list never showed', async () => {
    // A tab closed before its first send is never listed (`user_only` drops
    // never-used sessions), so absence from a complete list proves nothing.
    expect(addStashEntry('never-sent', makeStashEntry('first prompt, stashed', []))).toBe(true)
    apiMock.sessions.mockResolvedValueOnce({ sessions: [{ key: 'dashboard_old-chat' }], has_more: false })
    const store = makeStore()

    await store.dispatch(fetchHistory(false))

    expect(loadPromptStash('never-sent').map(entry => entry.text)).toEqual(['first prompt, stashed'])
  })

  it('keeps the stash of a listed session that a later complete refresh no longer lists', async () => {
    // Leaving the list does not mean deleted: another tab may have resumed the
    // session (`exclude_open` drops it) while this tab's slot list is stale.
    // Only a confirmed delete clears a stash.
    expect(addStashEntry('resumed-elsewhere', makeStashEntry('draft in a resumed session', []))).toBe(true)
    apiMock.sessions.mockResolvedValueOnce({
      sessions: [{ key: 'dashboard_resumed-elsewhere' }, { key: 'dashboard_kept-chat' }],
      has_more: false,
    })
    const store = makeStore()
    await store.dispatch(fetchHistory(false))

    apiMock.sessions.mockResolvedValueOnce({ sessions: [{ key: 'dashboard_kept-chat' }], has_more: false })
    await store.dispatch(fetchHistory(false))

    expect(loadPromptStash('resumed-elsewhere').map(entry => entry.text)).toEqual(['draft in a resumed session'])
  })

  it('keeps the stash of a session a Load more page skipped after a row delete', async () => {
    // Deleting a row leaves historyOffset alone, so the next Load more starts
    // one row late and never returns `skipped-chat`. Its stash must survive.
    expect(addStashEntry('skipped-chat', makeStashEntry('draft in a skipped session', []))).toBe(true)
    apiMock.sessions.mockResolvedValueOnce({
      sessions: [{ key: 'dashboard_first-chat' }, { key: 'dashboard_deleted-chat' }],
      has_more: true,
    })
    apiMock.deleteSession.mockResolvedValueOnce({ ok: true })
    const store = makeStore()
    await store.dispatch(fetchHistory(false))
    await store.dispatch(deleteHistorySession('dashboard_deleted-chat'))

    apiMock.sessions.mockResolvedValueOnce({ sessions: [{ key: 'dashboard_last-chat' }], has_more: false })
    await store.dispatch(fetchHistory(true))

    expect(loadPromptStash('skipped-chat').map(entry => entry.text)).toEqual(['draft in a skipped session'])
  })

  it('keeps a draft stashed in a reopened session while the delete was in flight', async () => {
    // A member thread reuses its slot key: tab B reopens it and stashes before
    // tab A reads the DELETE response. That draft is the new session's, not
    // the deleted one's, so the confirmed delete must not take it.
    const slot = 'member-code-reviewer'
    expect(addStashEntry(slot, makeStashEntry('deleted session draft', []))).toBe(true)
    apiMock.deleteSession.mockImplementationOnce(async () => {
      expect(addStashEntry(slot, makeStashEntry('reopened session draft', []))).toBe(true)
      return { ok: true }
    })
    const store = makeStore()

    await store.dispatch(deleteHistorySession(`dashboard:${slot}`))

    expect(loadPromptStash(slot).map(entry => entry.text)).toEqual(['reopened session draft'])
  })

  it('keeps a surviving channel slot\'s stash when its dashboard lookalike is deleted', async () => {
    // `dashboard_slack_<ts>` is a separate dashboard transcript; deleting it
    // leaves the channel session `slack_<ts>` and its unsent drafts in place.
    const slot = 'slack_1785370133.085469'
    expect(addStashEntry(slot, makeStashEntry('channel draft', []))).toBe(true)
    apiMock.deleteSession.mockResolvedValueOnce({ ok: true })
    const store = makeStore()

    await store.dispatch(deleteHistorySession(`dashboard_${slot}`))

    expect(apiMock.deleteSession).toHaveBeenCalledWith(`dashboard_${slot}`)
    expect(loadPromptStash(slot).map(entry => entry.text)).toEqual(['channel draft'])
  })
})
