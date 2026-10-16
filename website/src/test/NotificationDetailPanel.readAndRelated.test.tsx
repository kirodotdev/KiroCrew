/**
 * The detail panel's read toggle and its related-chat match.
 *
 * The toggle sends the read state the panel shows the opposite of: an unread
 * note offers "Mark read", a read one offers "Mark unread", and a retired
 * approval offers neither (it is read and asks for nothing). A note with no
 * slot of its own links to the chat whose title appears in its body as a
 * whole word, or whose key appears in its title.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders, createTestStore } from './helpers'
import type { RootState } from '../store'
import dashboardReducer from '../store/dashboardSlice'
import NotificationDetailPanel from '../components/notifications/NotificationDetailPanel'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import type { Notification } from '../types'
import { ThemeBootProbe, themeBootSettled } from './themeBootSettled'

vi.mock('../api/client', () => ({
  api: { ackNotification: vi.fn().mockResolvedValue({}), unackNotification: vi.fn().mockResolvedValue({}) },
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))
vi.mock('../pages/chat', () => ({ CronAckBar: () => null }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const note: Notification = {
  kind: 'info', ts: '2026-10-06T07:00:00Z', title: 'Nightly report', body: 'The Release notes chat finished its draft.',
}

async function renderPanel(n: Notification, slots: Array<{ key: string; title?: string }> = []) {
  const dashboard = { ...dashboardReducer(undefined, { type: '@@init' }), slots } as unknown as RootState['dashboard']
  const store = createTestStore({ dashboard, notifications: { items: [n] } as RootState['notifications'] })
  renderWithProviders(<><NotificationDetailPanel n={n} onClose={vi.fn()} /><ThemeBootProbe /></>, { store })
  await themeBootSettled()
  return store
}

beforeEach(() => {
  vi.mocked(api.ackNotification).mockClear()
  vi.mocked(api.unackNotification).mockClear()
})

describe('NotificationDetailPanel read toggle', () => {
  it('an unread note offers Mark read, which acks it', async () => {
    await renderPanel({ ...note, acked: false })
    await userEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('components.notifications.notificationDetailPanel.mark_read')) }))
    await waitFor(() => { expect(api.ackNotification).toHaveBeenCalledWith(note.ts) })
    expect(api.unackNotification).not.toHaveBeenCalled()
  })

  it('a read note offers Mark unread, which un-acks it', async () => {
    await renderPanel({ ...note, acked: true })
    await userEvent.click(screen.getByRole('button', { name: new RegExp(i18nT('components.notifications.notificationDetailPanel.mark_unread')) }))
    await waitFor(() => { expect(api.unackNotification).toHaveBeenCalledWith(note.ts) })
    expect(api.ackNotification).not.toHaveBeenCalled()
  })
})

describe('NotificationDetailPanel related chat', () => {
  const goToChat = () => screen.queryByRole('button', { name: new RegExp(i18nT('components.notifications.notificationDetailPanel.go_to_chat')) })

  it('links the chat whose title appears in the body as a whole word', async () => {
    await renderPanel({ ...note, acked: true }, [{ key: 'chat-a', title: 'Release notes' }])
    expect(goToChat()).not.toBeNull()
  })

  it('links the chat whose key appears in the title', async () => {
    await renderPanel({ ...note, acked: true, title: 'Report for chat-b' }, [{ key: 'chat-b', title: 'xy' }])
    expect(goToChat()).not.toBeNull()
  })

  it('links nothing when no chat matches', async () => {
    await renderPanel({ ...note, acked: true }, [{ key: 'chat-c', title: 'Releases' }])
    expect(goToChat()).toBeNull()
  })
})
