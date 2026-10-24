/**
 * Opening a cron job's chat marks that job's bell notes read (#23564).
 *
 * The hook reads only `chat.activeSlot` and the notifications slice, so the
 * store here carries the real notifications reducer beside a one-field chat
 * stand-in: the chat slice's own switch thunk fetches the transcript, which is
 * not what these tests are about. happy-dom reports the window focused by
 * default (hidden=false, hasFocus()=true), so "away" cases set it explicitly.
 */
import { createElement, type ReactNode } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore, type UnknownAction } from '@reduxjs/toolkit'
import notificationsReducer, { addNotification, unackNotificationByTs } from '../store/notificationsSlice'
import { cronNotesSeenInChat, useMarkOpenCronChatRead } from '../hooks/useMarkOpenCronChatRead'
import type { Notification } from '../types'

const ackNotification = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    ackNotification: (ts: string) => ackNotification(ts),
  },
}))

const cronNote = (ts: string, slot: string | undefined, over: Partial<Notification> = {}): Notification => ({
  kind: 'cron', title: 'Cron: nightly', body: 'done', ts, job_id: 'j1', slot, ...over,
})

const OPEN_SLOT = 'test/openSlot'
const chatStandIn = (state = { activeSlot: null as string | null }, action: UnknownAction) =>
  action.type === OPEN_SLOT ? { activeSlot: action.payload as string } : state

function setup(items: Notification[], activeSlot: string | null, route = '/chat') {
  const notifications = notificationsReducer(undefined, { type: '@@test/init' })
  const store = configureStore({
    reducer: { chat: chatStandIn, notifications: notificationsReducer },
    preloadedState: { chat: { activeSlot }, notifications: { ...notifications, items } },
  })
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(Provider, { store, children: createElement(MemoryRouter, { initialEntries: [route] }, children) })
  renderHook(() => useMarkOpenCronChatRead(), { wrapper })
  const acked = (ts: string) => store.getState().notifications.items.find(n => n.ts === ts)?.acked
  return { store, acked }
}

describe('cronNotesSeenInChat', () => {
  const ctx = { pathname: '/chat', activeSlot: 'cron-j1', windowAway: false }

  it('selects only unread cron notes for the chat on screen', () => {
    const items = [
      cronNote('1', 'cron-j1'),
      cronNote('2', 'cron-j1', { acked: true }),
      cronNote('3', 'cron-j2'),
      cronNote('4', undefined),
      cronNote('5', 'cron-j1', { kind: 'approval' }),
    ]
    expect(cronNotesSeenInChat(items, ctx)).toEqual(['1'])
  })

  it('selects nothing off a chat route, with no active chat, or while away', () => {
    const items = [cronNote('1', 'cron-j1')]
    expect(cronNotesSeenInChat(items, { ...ctx, pathname: '/schedule' })).toEqual([])
    expect(cronNotesSeenInChat(items, { ...ctx, activeSlot: null })).toEqual([])
    expect(cronNotesSeenInChat(items, { ...ctx, windowAway: true })).toEqual([])
  })
})

describe('useMarkOpenCronChatRead', () => {
  beforeEach(() => {
    ackNotification.mockReset()
    ackNotification.mockResolvedValue({ ok: true })
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("marks the open cron chat's notes read and leaves other notes alone", async () => {
    const { acked } = setup([cronNote('1', 'cron-j1'), cronNote('2', 'cron-j2')], 'cron-j1')
    await waitFor(() => expect(acked('1')).toBe(true))
    expect(ackNotification).toHaveBeenCalledWith('1')
    expect(ackNotification).toHaveBeenCalledTimes(1)
    expect(acked('2')).toBeFalsy()
  })

  it('marks a note when its chat is opened later', async () => {
    const { store } = setup([cronNote('1', 'cron-j1')], 'other')
    expect(ackNotification).not.toHaveBeenCalled()
    act(() => { store.dispatch({ type: OPEN_SLOT, payload: 'cron-j1' }) })
    await waitFor(() => expect(ackNotification).toHaveBeenCalledWith('1'))
  })

  it('marks a run that lands while its chat is on screen', async () => {
    const { store } = setup([], 'cron-j1')
    act(() => { store.dispatch(addNotification(cronNote('9', 'cron-j1'))) })
    await waitFor(() => expect(ackNotification).toHaveBeenCalledWith('9'))
  })

  it('waits while the window is away and marks once the user returns', async () => {
    const hasFocus = vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    setup([cronNote('1', 'cron-j1')], 'cron-j1')
    expect(ackNotification).not.toHaveBeenCalled()
    hasFocus.mockReturnValue(true)
    act(() => { window.dispatchEvent(new Event('focus')) })
    await waitFor(() => expect(ackNotification).toHaveBeenCalledWith('1'))
  })

  it('leaves a note unread when the user marks it unread with the chat open', async () => {
    const { store, acked } = setup([cronNote('1', 'cron-j1')], 'cron-j1')
    await waitFor(() => expect(acked('1')).toBe(true))
    act(() => { store.dispatch(unackNotificationByTs('1')) })
    act(() => { window.dispatchEvent(new Event('focus')) })
    expect(acked('1')).toBe(false)
    expect(ackNotification).toHaveBeenCalledTimes(1)
  })

  it('does not retry a mark-read the server refused', async () => {
    ackNotification.mockRejectedValue(new Error('refused'))
    const { acked } = setup([cronNote('1', 'cron-j1')], 'cron-j1')
    await waitFor(() => expect(ackNotification).toHaveBeenCalledWith('1'))
    await waitFor(() => expect(acked('1')).toBe(false))
    act(() => { window.dispatchEvent(new Event('focus')) })
    expect(ackNotification).toHaveBeenCalledTimes(1)
  })

  it('does nothing off a chat route', () => {
    setup([cronNote('1', 'cron-j1')], 'cron-j1', '/schedule')
    expect(ackNotification).not.toHaveBeenCalled()
  })
})
