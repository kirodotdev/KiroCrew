/**
 * /notifications on mobile: when the open note is gone, focus lands in the feed.
 *
 * The detail replaces the feed in place, so pressing Approve or Reject (or a
 * delete from another tab) unmounts the detail while it holds focus. Back
 * returns focus to the opened row, but that row is gone too, so focus fell
 * to <body>. The first row takes it instead.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { deleteNotification } from '../store/notificationsSlice'
import NotificationsPage from '../pages/NotificationsPage'
import type { RootState } from '../store'
import type { Notification } from '../types'

let mobile = false
vi.mock('../hooks/useIsMobile', () => ({
  useIsMobile: () => mobile,
}))

vi.mock('../api/client', () => ({
  api: {
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    taskRunToChat: vi.fn().mockResolvedValue({}),
    resolveApproval: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const first: Notification = {
  kind: 'cron', ts: '2026-05-29T10:00:00Z', title: 'Cron Result', body: 'cron body text', acked: true,
}
const second: Notification = {
  kind: 'hook', ts: '2026-05-29T09:00:00Z', title: 'Hook Fired', body: 'hook body text', acked: true,
}

function renderPage() {
  const store = createTestStore({ notifications: { items: [first, second] } as RootState['notifications'] } as Partial<RootState>)
  renderWithProviders(<NotificationsPage />, { store })
  return store
}

/** Focus a row's open control the way a keyboard user would, then press it. */
function openRow(title: string): HTMLElement {
  const row = screen.getByRole('button', { name: `Open notification: ${title}` })
  row.focus()
  fireEvent.click(row)
  return row
}

beforeEach(() => {
  localStorage.clear()
  mobile = true
})

describe('NotificationsPage mobile focus when the open note is gone', () => {
  it('moves focus to the first row', () => {
    const store = renderPage()
    openRow('Hook Fired')
    screen.getByRole('button', { name: 'Back' }).focus()

    act(() => { store.dispatch(deleteNotification.fulfilled(second.ts, '', second.ts)) })

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open notification: Cron Result' }))
  })

  it('moves focus on to the first row when the delete lands after Back', () => {
    const store = renderPage()
    openRow('Hook Fired')
    const back = screen.getByRole('button', { name: 'Back' })
    back.focus()
    fireEvent.click(back)

    act(() => { store.dispatch(deleteNotification.fulfilled(second.ts, '', second.ts)) })

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open notification: Cron Result' }))
  })

  it('leaves focus on a live control outside the feed when the open note is deleted', () => {
    const store = renderPage()
    openRow('Hook Fired')
    const elsewhere = document.createElement('button')
    document.body.appendChild(elsewhere)
    elsewhere.focus()

    act(() => { store.dispatch(deleteNotification.fulfilled(second.ts, '', second.ts)) })

    expect(document.activeElement).toBe(elsewhere)
    elsewhere.remove()
  })

  it('still returns to the opened row when it is there', () => {
    renderPage()
    openRow('Hook Fired')
    const back = screen.getByRole('button', { name: 'Back' })
    back.focus()
    fireEvent.click(back)

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open notification: Hook Fired' }))
  })
})
