import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import NotificationFeed from '../components/notifications/NotificationFeed'
import { addNotification } from '../store/notificationsSlice'
import type { RootState } from '../store'
import type { Notification } from '../types'

const api = vi.hoisted(() => ({
  notifications: vi.fn(),
  ackNotification: vi.fn(),
  deleteNotification: vi.fn(),
  clearNotifications: vi.fn(),
  updateNotificationChannelSettings: vi.fn(),
}))
vi.mock('../api/client', () => ({ api }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const note = (ts: string, title: string): Notification => ({ kind: 'cron', ts, title, body: 'body', acked: false })

function renderFeed(items: Notification[]) {
  const store = createTestStore({ notifications: { items } as RootState['notifications'] })
  const { unmount } = renderWithProviders(<NotificationFeed selectedTs={null} onSelect={() => {}} />, { store })
  return Object.assign(store, { unmount })
}

const openClearAll = async () => {
  fireEvent.click(screen.getByRole('button', { name: /^Clear/ }))
  return screen.findByRole('dialog', { name: 'Clear all notifications?' })
}

beforeEach(() => {
  Object.values(api).forEach(m => m.mockReset())
  api.notifications.mockResolvedValue({ notifications: [] })
  api.deleteNotification.mockResolvedValue({})
  api.clearNotifications.mockResolvedValue({})
})

describe('NotificationFeed Clear all', () => {
  it('clears in one request when nothing arrived while it asked', async () => {
    renderFeed([note('1700000001', 'First'), note('1700000002', 'Second')])
    const dialog = await openClearAll()
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Clear' })) })
    await waitFor(() => expect(api.clearNotifications).toHaveBeenCalledTimes(1))
    expect(api.deleteNotification).not.toHaveBeenCalled()
  })

  it('keeps a note delivered while the question was open', async () => {
    const store = renderFeed([note('1700000001', 'First')])
    const dialog = await openClearAll()
    act(() => { store.dispatch(addNotification(note('1700000009', 'Arrived later'))) })
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Clear' })) })
    await waitFor(() => expect(api.deleteNotification).toHaveBeenCalledWith('1700000001'))
    expect(api.clearNotifications).not.toHaveBeenCalled()
    expect(api.deleteNotification).not.toHaveBeenCalledWith('1700000009')
    await waitFor(() => expect(store.getState().notifications.items.map(n => n.ts)).toEqual(['1700000009']))
  })

  it('reports a failed clear, and a successful retry removes the report', async () => {
    api.clearNotifications.mockRejectedValueOnce(new Error('offline'))
    renderFeed([note('1700000001', 'First')])
    const dialog = await openClearAll()
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Clear' })) })
    expect(await screen.findByTestId('notification-feed-clear-failed')).toHaveTextContent('Couldn\'t clear the notifications. Try again.')
    expect(screen.getByText('First')).toBeTruthy()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    const retry = await openClearAll()
    await act(async () => { fireEvent.click(within(retry).getByRole('button', { name: 'Clear' })) })
    await waitFor(() => expect(api.clearNotifications).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('notification-feed-clear-failed')).toBeNull())
  })

  it('shows no failure once another clear has emptied the inbox, in either settle order', async () => {
    for (const order of ['older-fails-last', 'newer-fails-last'] as const) {
      const settle: { ok: () => void; fail: (e: Error) => void }[] = []
      api.clearNotifications.mockReset()
      api.clearNotifications.mockImplementation(() => new Promise<void>((ok, fail) => { settle.push({ ok, fail }) }))
      const { unmount } = renderFeed([note('1700000001', 'First')])
      for (let i = 0; i < 2; i++) {
        const dialog = await openClearAll()
        await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Clear' })) })
        await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
      }
      const [first, second] = settle
      if (order === 'older-fails-last') {
        await act(async () => { second.ok() })
        await act(async () => { first.fail(new Error('offline')) })
      } else {
        await act(async () => { first.ok() })
        await act(async () => { second.fail(new Error('offline')) })
      }
      expect(screen.queryByTestId('notification-feed-clear-failed')).toBeNull()
      unmount()
    }
  })

  it('a clear that failed in one feed is cleared by a successful clear in the other', async () => {
    api.clearNotifications.mockRejectedValueOnce(new Error('offline'))
    const store = createTestStore({ notifications: { items: [note('1700000001', 'First')] } as RootState['notifications'] })
    renderWithProviders(<><NotificationFeed selectedTs={null} onSelect={() => {}} /><NotificationFeed variant="mac" selectedTs={null} onSelect={() => {}} /></>, { store })
    const [panelClear, macClear] = screen.getAllByRole('button', { name: /^Clear/ })
    fireEvent.click(panelClear)
    await act(async () => { fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Clear' })) })
    expect(await screen.findByTestId('notification-feed-clear-failed')).toBeTruthy()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    fireEvent.click(macClear)
    const macDialog = await screen.findByRole('dialog', { name: 'Clear all notifications?' })
    await act(async () => { fireEvent.click(within(macDialog).getByRole('button', { name: 'Clear' })) })
    await waitFor(() => expect(screen.queryByTestId('notification-feed-clear-failed')).toBeNull())
  })

  it('reports a failed delete when it deletes only what it showed', async () => {
    api.deleteNotification.mockRejectedValueOnce(new Error('offline'))
    const store = renderFeed([note('1700000001', 'First')])
    const dialog = await openClearAll()
    act(() => { store.dispatch(addNotification(note('1700000009', 'Arrived later'))) })
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Clear' })) })
    expect(await screen.findByTestId('notification-feed-clear-failed')).toBeTruthy()
    expect(store.getState().notifications.items.map(n => n.ts)).toContain('1700000001')
  })
})

describe('NotificationFeed stored errors', () => {
  it('lists a past error note without mounting an alert', async () => {
    const items: Notification[] = [{ kind: 'error', ts: '1700000003', title: 'Couldn\'t reload this session.', body: '', acked: true }]
    const store = createTestStore({ notifications: { items } as RootState['notifications'] })
    renderWithProviders(<NotificationFeed variant="mac" selectedTs={null} onSelect={() => {}} />, { store })
    expect(await screen.findByText('Couldn\'t reload this session.')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
