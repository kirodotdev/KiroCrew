/**
 * The ONE post-resolve check every resume entry point reads (#5925).
 *
 * `api_chat_slot_resume` succeeds whether or not the resumed session's surface
 * is one the chat page can display, so `ok` alone cannot tell a usable resume
 * from one that will bounce (#3624). PR #3640 taught the sidebar row to look at
 * the returned `surface`, but the four sibling call sites -- ChatPage's
 * "Continue a previous chat" list, the notification panel's Resume button, and
 * the `recents` / `sessions` command-palette providers -- kept resolving blind.
 * Two of those are plain modules with no component, so they cannot own a notice
 * at all.
 *
 * So the check moved into `resumeFromHistory.fulfilled`, next to the
 * short-circuit that already computes the same predicate. These tests pin that
 * contract: what gets recorded, when it is cleared, and that a late answer from
 * a superseded resume cannot narrate a row the user has moved past.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'

vi.mock('../api/client', () => ({
  api: { resumeChatSlot: vi.fn() },
}))

import chatReducer, { resumeFromHistory, clearUnresumableResume } from './chatSlice'
import { api } from '../api/client'

const resumeChatSlotMock = api.resumeChatSlot as unknown as ReturnType<typeof vi.fn>

function makeStore() {
  return configureStore({
    reducer: { chat: chatReducer },
    middleware: (getDefault) => getDefault({ serializableCheck: false, immutableCheck: false }),
  })
}

beforeEach(() => {
  resumeChatSlotMock.mockReset()
})

describe('unresumableResume — shared post-resolve check (#5925)', () => {
  it('records the resume when the wire answers with a surface the chat page cannot display', async () => {
    resumeChatSlotMock.mockResolvedValue({ ok: true, key: 'dashboard_ops', mode: 'dashboard', messages: [] })
    const store = makeStore()

    await store.dispatch(resumeFromHistory({ key: 'dashboard_ops', title: 'Ops board' }) as never)

    // Raw facts, not a sentence: the render site localizes the surface label
    // from the key, which a reducer cannot do.
    expect(store.getState().chat.unresumableResume).toEqual({
      key: 'dashboard_ops',
      title: 'Ops board',
      surface: 'dashboard',
      reason: 'surface',
    })
    // The short-circuit itself still holds: nothing else moved.
    expect(store.getState().chat.activeSlot).toBeNull()
  })

  it('records nothing when the resume lands on a surface the chat page does show', async () => {
    const store = makeStore()
    // Seed a notice first, so this asserts a displayable resume both declines to
    // narrate AND does not leave the previous row's narration standing.
    resumeChatSlotMock.mockResolvedValueOnce({ ok: true, key: 'dashboard_ops', mode: 'dashboard', messages: [] })
    await store.dispatch(resumeFromHistory({ key: 'dashboard_ops', title: 'Ops board' }) as never)
    expect(store.getState().chat.unresumableResume).not.toBeNull()

    resumeChatSlotMock.mockResolvedValueOnce({ ok: true, key: 'chat-1', mode: 'orchestrator', messages: [] })
    await store.dispatch(resumeFromHistory({ key: 'chat-1', title: 'Work' }) as never)

    expect(store.getState().chat.unresumableResume).toBeNull()
    expect(store.getState().chat.activeSlot).toBe('chat-1')
  })

  it('records a FAILED resume, so the rarer path is not the dead click it was', async () => {
    // `ok: false` is a fulfilled payload, so it never reaches the `catch` a
    // caller wrote: before this branch existed, nothing anywhere narrated it.
    resumeChatSlotMock.mockResolvedValue({ ok: false, key: 'gone', messages: [] })
    const store = makeStore()

    await store.dispatch(resumeFromHistory({ key: 'gone', title: 'Gone' }) as never)

    expect(store.getState().chat.unresumableResume).toEqual({
      key: 'gone', title: 'Gone', surface: '', reason: 'failed',
    })
    // Still no slice mutation: nothing was resumed.
    expect(store.getState().chat.activeSlot).toBeNull()
  })

  it('records a REJECTED resume, which is the likeliest failure of all', async () => {
    // `api.resumeChatSlot` throws on any non-2xx, so a 404/409/5xx lands on
    // `rejected` rather than on the `ok: false` branch.
    resumeChatSlotMock.mockRejectedValue(new Error('404 not found'))
    const store = makeStore()

    await store.dispatch(resumeFromHistory({ key: 'gone', title: 'Gone' }) as never)

    expect(store.getState().chat.unresumableResume).toEqual({
      key: 'gone', title: 'Gone', surface: '', reason: 'failed',
    })
  })

  it('clears a previous notice as soon as the next resume starts', async () => {
    resumeChatSlotMock.mockResolvedValue({ ok: true, key: 'dashboard_ops', mode: 'dashboard', messages: [] })
    const store = makeStore()
    await store.dispatch(resumeFromHistory({ key: 'dashboard_ops', title: 'Ops board' }) as never)
    expect(store.getState().chat.unresumableResume).not.toBeNull()

    // A resume that never settles: only `pending` has run, which is the moment
    // the stale notice has to go -- keeping it would attribute the old row's
    // outcome to the click the user just made.
    resumeChatSlotMock.mockReturnValue(new Promise(() => {}))
    void store.dispatch(resumeFromHistory({ key: 'chat-2', title: 'Other' }) as never)

    expect(store.getState().chat.unresumableResume).toBeNull()
  })

  it('ignores a superseded resume answering late, so the notice tracks the last click', async () => {
    // Two clicks in flight; the FIRST (undisplayable) resolves last. Before the
    // check moved into the slice this ordering was only guarded inside the
    // sidebar component, so a palette resume racing a sidebar resume was
    // unordered -- and a stale answer could narrate a row already moved past.
    let settleFirst: (v: unknown) => void = () => {}
    resumeChatSlotMock.mockImplementationOnce(() => new Promise(res => { settleFirst = res }))
    resumeChatSlotMock.mockResolvedValueOnce({ ok: true, key: 'chat-2', mode: 'orchestrator', messages: [] })
    const store = makeStore()

    const first = store.dispatch(resumeFromHistory({ key: 'dashboard_ops', title: 'Ops board' }) as never)
    await store.dispatch(resumeFromHistory({ key: 'chat-2', title: 'Other' }) as never)
    settleFirst({ ok: true, key: 'dashboard_ops', mode: 'dashboard', messages: [] })
    await first

    expect(store.getState().chat.unresumableResume).toBeNull()
  })

  it('clearUnresumableResume dismisses the notice without disarming the ordering token', async () => {
    resumeChatSlotMock.mockResolvedValue({ ok: true, key: 'dashboard_ops', mode: 'dashboard', messages: [] })
    const store = makeStore()
    await store.dispatch(resumeFromHistory({ key: 'dashboard_ops', title: 'Ops board' }) as never)

    store.dispatch(clearUnresumableResume())

    expect(store.getState().chat.unresumableResume).toBeNull()
    // The token still names the resume that produced the dismissed notice, so a
    // second late answer from it cannot re-open what the user just closed.
    expect(store.getState().chat.lastResumeRequestId).not.toBeNull()
  })
})
