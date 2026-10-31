/**
 * /notifications mobile focus contract.
 *
 * On mobile the feed and the detail swap in place: opening a row hides the
 * feed, and Back unmounts the detail. Whatever held focus is gone either way,
 * so without help keyboard and screen-reader users land on <body>. Opening a
 * row moves focus to the detail's Back button; Back returns it to the row
 * that was opened.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
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
  mobile = false
})

describe('NotificationsPage mobile focus', () => {
  it('opening a row moves focus to the detail Back button', () => {
    mobile = true
    renderPage()

    openRow('Hook Fired')

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Back' }))
  })

  it('Back returns focus to the row that was opened', () => {
    mobile = true
    renderPage()

    openRow('Hook Fired')
    // A keyboard user presses Back from Back itself; happy-dom does no layout,
    // so it would otherwise keep focus on the (display:none) row.
    const back = screen.getByRole('button', { name: 'Back' })
    back.focus()
    fireEvent.click(back)

    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open notification: Hook Fired' }))
  })

  it('desktop: opening a row leaves focus on the row', () => {
    renderPage()

    const row = openRow('Cron Result')

    expect(document.activeElement).toBe(row)
  })
})
