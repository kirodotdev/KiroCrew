// A cron run's bell note is marked read once its chat is on screen (#23564),
// and only then: another session, another note kind, a non-chat page, an
// unfocused window or a hidden Crew all leave it unread.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { api } from '../api/client'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
import notificationsReducer, { addNotification } from '../store/notificationsSlice'
import type { Notification } from '../types'
import { unreadCronNotesFor, useAckCronNotesInView } from '../hooks/useAckCronNotesInView'
import { createTestStore, renderHookWithProviders } from './helpers'

const note = (over: Partial<Notification>): Notification => ({
  kind: 'cron', title: 'Nightly digest', body: 'done', ts: '2026-10-24T01:00:00Z', slot: 'cron-a', job_id: 'job-a',
  ...over,
})

function storeWith(items: Notification[], activeSlot: string | null) {
  const chat = chatReducer(undefined, setActiveSlot(activeSlot as string))
  const notifications = { ...notificationsReducer(undefined, { type: '@@test/init' }), items }
  return createTestStore({ chat, notifications } as never)
}

const ackedTs = (store: ReturnType<typeof createTestStore>) =>
  store.getState().notifications.items.filter(n => n.acked).map(n => n.ts)

let ack: ReturnType<typeof vi.spyOn>
let focused = true
beforeEach(() => {
  focused = true
  ack = vi.spyOn(api, 'ackNotification').mockResolvedValue(new Response('{}'))
  vi.spyOn(document, 'hasFocus').mockImplementation(() => focused)
})
afterEach(() => vi.restoreAllMocks())

describe('unreadCronNotesFor', () => {
  it('picks unread cron notes of the slot and nothing else', () => {
    const notes = [
      note({ ts: '1' }),
      note({ ts: '2', acked: true }),
      note({ ts: '3', slot: 'cron-b' }),
      note({ ts: '4', kind: 'approval' }),
      note({ ts: '5', kind: 'agent' }),
    ]
    expect(unreadCronNotesFor(notes, 'cron-a')).toEqual(['1'])
    expect(unreadCronNotesFor(notes, null)).toEqual([])
  })
})

describe('useAckCronNotesInView', () => {
  it('marks the open cron chat\'s notes read, leaving every other note alone', async () => {
    const store = storeWith([
      note({ ts: 'run-1' }),
      note({ ts: 'run-2' }),
      note({ ts: 'other-session', slot: 'cron-b' }),
      note({ ts: 'approval', kind: 'approval' }),
    ], 'cron-a')
    renderHookWithProviders(() => useAckCronNotesInView(), { store, route: '/chat' })
    await waitFor(() => expect(ackedTs(store).sort()).toEqual(['run-1', 'run-2']))
    expect(ack.mock.calls.map(c => c[0]).sort()).toEqual(['run-1', 'run-2'])
  })

  it('does nothing away from a chat route', async () => {
    const store = storeWith([note({ ts: 'run-1' })], 'cron-a')
    renderHookWithProviders(() => useAckCronNotesInView(), { store, route: '/notifications' })
    await act(async () => { await Promise.resolve() })
    expect(ack).not.toHaveBeenCalled()
    expect(ackedTs(store)).toEqual([])
  })

  it('does nothing while the local Crew is hidden', async () => {
    const store = storeWith([note({ ts: 'run-1' })], 'cron-a')
    renderHookWithProviders(() => useAckCronNotesInView({ hidden: true }), { store, route: '/chat' })
    await act(async () => { await Promise.resolve() })
    expect(ack).not.toHaveBeenCalled()
  })

  it('waits for the window to come back into focus', async () => {
    focused = false
    const store = storeWith([note({ ts: 'run-1' })], 'cron-a')
    renderHookWithProviders(() => useAckCronNotesInView(), { store, route: '/chat' })
    await act(async () => { await Promise.resolve() })
    expect(ack).not.toHaveBeenCalled()

    focused = true
    act(() => { window.dispatchEvent(new Event('focus')) })
    await waitFor(() => expect(ackedTs(store)).toEqual(['run-1']))
  })

  it('reads a note that arrives while its chat is open, and one for a chat switched to', async () => {
    const store = storeWith([note({ ts: 'b-1', slot: 'cron-b' })], 'cron-a')
    renderHookWithProviders(() => useAckCronNotesInView(), { store, route: '/chat' })
    await act(async () => { await Promise.resolve() })
    expect(ack).not.toHaveBeenCalled()

    act(() => { store.dispatch(addNotification(note({ ts: 'a-1' }))) })
    await waitFor(() => expect(ackedTs(store)).toEqual(['a-1']))

    act(() => { store.dispatch(setActiveSlot('cron-b')) })
    await waitFor(() => expect(ackedTs(store).sort()).toEqual(['a-1', 'b-1']))
  })

  it('asks once per note when the server refuses, instead of looping', async () => {
    ack.mockRejectedValue(new Error('500'))
    const store = storeWith([note({ ts: 'run-1' })], 'cron-a')
    renderHookWithProviders(() => useAckCronNotesInView(), { store, route: '/chat' })
    await waitFor(() => expect(ack).toHaveBeenCalledTimes(1))
    // The refusal rolls the note back to unread; a re-render must not re-ask.
    await waitFor(() => expect(ackedTs(store)).toEqual([]))
    act(() => { window.dispatchEvent(new Event('focus')) })
    await act(async () => { await Promise.resolve() })
    expect(ack).toHaveBeenCalledTimes(1)
  })
})
