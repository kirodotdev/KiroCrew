import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import type { Notification } from '../types'
import { LocalOverlayRoot } from '../components/overlayRoot'
import { NotificationSheet, useNotificationSheet } from '../shell/notifications/notificationSheet'

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

let sheetApi: ReturnType<typeof useNotificationSheet>
function Harness() {
  sheetApi = useNotificationSheet()
  return <NotificationSheet sheet={sheetApi} />
}

function mountOpenSheet() {
  const store = createTestStore({ notifications: { items: [note('1700000001', 'First')] } as RootState['notifications'] })
  render(
    <Provider store={store}>
      <MemoryRouter><LocalOverlayRoot hidden={false}><Harness /></LocalOverlayRoot></MemoryRouter>
    </Provider>,
  )
  act(() => { sheetApi.toggle() })
  expect(sheetApi.phase).toBe('open')
}

const openClearAll = async () => {
  fireEvent.click(screen.getByRole('button', { name: /^Clear/ }))
  return screen.findByRole('dialog', { name: 'Clear all notifications?' })
}

beforeEach(() => {
  Object.values(api).forEach(m => m.mockReset())
  api.notifications.mockResolvedValue({ notifications: [] })
  api.clearNotifications.mockResolvedValue({})
  api.deleteNotification.mockResolvedValue({})
})

describe('Clear all from the bell sheet', () => {
  it('clears and keeps the sheet open when the question is confirmed', async () => {
    mountOpenSheet()
    const dialog = await openClearAll()
    const confirm = within(dialog).getByRole('button', { name: 'Clear' })
    // A real press: the sheet's outside-pointer dismissal sees the pointerdown first.
    await act(async () => {
      fireEvent.pointerDown(confirm)
      fireEvent.pointerUp(confirm)
      fireEvent.click(confirm)
    })
    await waitFor(() => expect(api.clearNotifications).toHaveBeenCalledTimes(1))
    expect(sheetApi.phase).toBe('open')
  })

  it('Escape answers only the question and leaves the sheet open', async () => {
    mountOpenSheet()
    await openClearAll()
    await act(async () => { fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' }) })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Clear all notifications?' })).toBeNull())
    expect(api.clearNotifications).not.toHaveBeenCalled()
    expect(sheetApi.phase).toBe('open')
  })
})
